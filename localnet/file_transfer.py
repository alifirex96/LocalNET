"""Передача файлов по прямому TCP-соединению.

Почему не ZeroMQ/HTTP/JSON: файл идёт «сырым» потоком байт по отдельному TCP-
соединению, без кодирования и копирования в пользовательском пространстве.
На Linux/macOS используется ``socket.sendfile()`` (системный ``sendfile`` —
zero-copy из page cache в сокет). Где его нет (Windows), работает chunked-режим
с переиспользуемым буфером 1 МБ (``readinto`` + ``sendall``). Файл никогда не
читается в память целиком — расход RAM не зависит от размера файла.

Протокол (порт ``file_port``, все управляющие кадры — 4 байта длины big-endian + UTF-8 JSON)::

    отправитель                                   получатель
    -----------                                   ----------
    b"LNF1"                                ->
    {"type":"offer", id, from, nick, name,
     size, sha256, sig}                    ->
                                           <-     {"type":"accept","offset":N}
                                                  | {"type":"decline","reason":...}
    <сырые байты файла с offset до size>   ->
                                           <-     {"type":"result","ok":true|false,"error":...}

* ``offset`` > 0 — докачка: у получателя уже есть ``.part`` с тем же хешем;
* целостность: получатель считает SHA-256 «на лету» и сверяет с ``sha256`` из offer;
* отмена: любая сторона просто закрывает соединение; ``.part`` при сетевой ошибке
  сохраняется для докачки, при отмене получателем — удаляется.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import PROTOCOL_VERSION
from .models import FileOffer, Peer, Transfer, TransferDirection, TransferState, new_id
from .utils import (
    IO_CHUNK,
    Cancelled,
    canonical_json,
    safe_filename,
    sha256_file,
    sign,
    tune_tcp_socket,
    unique_path,
    verify,
)

log = logging.getLogger(__name__)

MAGIC = b"LNF1"
MAX_FRAME = 64 * 1024
CONNECT_TIMEOUT = 5.0
IO_TIMEOUT = 30.0          # таймаут на каждую операцию чтения/записи сокета
ACCEPT_TIMEOUT = 120.0     # сколько отправитель ждёт решения получателя
RESULT_TIMEOUT = 120.0     # ожидание результата проверки хеша
SENDFILE_CHUNK = 8 * 1024 * 1024  # порция sendfile между проверками отмены/прогресса
PROGRESS_INTERVAL = 0.25   # не чаще, чем раз в N секунд сообщаем о прогрессе
USE_SENDFILE = hasattr(os, "sendfile")  # zero-copy доступен (Linux, macOS, BSD)


class ProtocolError(Exception):
    pass


class Declined(Exception):
    pass


# --------------------------------------------------------------------------- #
# Кадрирование
# --------------------------------------------------------------------------- #

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Читает ровно ``n`` байт или бросает ``ConnectionError`` при обрыве."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionError("Соединение закрыто удалённой стороной")
        got += r
    return bytes(buf)


def send_frame(sock: socket.socket, obj: dict) -> None:
    data = canonical_json(obj)
    if len(data) > MAX_FRAME:
        raise ProtocolError("Слишком большой управляющий кадр")
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_frame(sock: socket.socket) -> dict:
    (length,) = struct.unpack(">I", recv_exact(sock, 4))
    if length > MAX_FRAME:
        raise ProtocolError(f"Слишком большой управляющий кадр: {length}")
    try:
        obj = json.loads(recv_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"Некорректный кадр: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("Кадр должен быть JSON-объектом")
    return obj


def validate_offer(obj: dict, secret: str) -> None:
    if obj.get("type") != "offer" or obj.get("proto") != PROTOCOL_VERSION:
        raise ProtocolError("Ожидался offer")
    for key in ("id", "from", "nick", "name", "sha256"):
        if not isinstance(obj.get(key), str) or not obj[key]:
            raise ProtocolError(f"Поле {key} отсутствует или некорректно")
    size = obj.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProtocolError("Некорректный размер файла")
    sha = obj["sha256"]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ProtocolError("Некорректный SHA-256")
    if not verify(obj, secret):
        raise ProtocolError("Неверная подпись (общий секрет не совпадает)")


# --------------------------------------------------------------------------- #
# Отправка тела файла
# --------------------------------------------------------------------------- #

def send_file_body(
    sock: socket.socket,
    f,
    offset: int,
    size: int,
    on_progress: Callable[[int], None],
    cancel: threading.Event,
    use_sendfile: bool = USE_SENDFILE,
) -> int:
    """Отправляет байты ``[offset, size)`` файла. Возвращает число отправленных байт."""
    pos = offset
    if use_sendfile:
        while pos < size:
            if cancel.is_set():
                raise Cancelled()
            count = min(SENDFILE_CHUNK, size - pos)
            sent = sock.sendfile(f, pos, count)
            if sent == 0:
                raise ConnectionError("sendfile отправил 0 байт")
            pos += sent
            on_progress(pos)
    else:
        buf = bytearray(IO_CHUNK)
        view = memoryview(buf)
        f.seek(pos)
        while pos < size:
            if cancel.is_set():
                raise Cancelled()
            n = f.readinto(view[: min(IO_CHUNK, size - pos)])
            if not n:
                raise IOError("Файл укоротился во время отправки")
            sock.sendall(view[:n])
            pos += n
            on_progress(pos)
    return pos - offset


def receive_file_body(
    sock: socket.socket,
    f,
    hasher,
    start: int,
    size: int,
    on_progress: Callable[[int], None],
    cancel: threading.Event,
    chunk_size: int = IO_CHUNK,
) -> int:
    """Принимает ``size - start`` байт в файл, обновляя хеш. Память — один буфер."""
    pos = start
    buf = bytearray(chunk_size)
    view = memoryview(buf)
    while pos < size:
        if cancel.is_set():
            raise Cancelled()
        n = sock.recv_into(view, min(chunk_size, size - pos))
        if n == 0:
            raise ConnectionError(f"Соединение оборвано: получено {pos} из {size} байт")
        chunk = view[:n]
        f.write(chunk)
        hasher.update(chunk)
        pos += n
        on_progress(pos)
    return pos - start


class _ProgressTracker:
    """Ограничивает частоту уведомлений о прогрессе и считает скорость."""

    def __init__(self, transfer: Transfer, notify: Callable[[Transfer], None]) -> None:
        self.t = transfer
        self.notify = notify
        self._last = 0.0
        self._t0 = time.monotonic()
        self._base = transfer.offset

    def __call__(self, done: int) -> None:
        self.t.done = done
        now = time.monotonic()
        if now - self._last >= PROGRESS_INTERVAL or done >= self.t.size:
            elapsed = max(1e-6, now - self._t0)
            self.t.speed = (done - self._base) / elapsed
            self._last = now
            self.notify(self.t)


# --------------------------------------------------------------------------- #
# Менеджер передач
# --------------------------------------------------------------------------- #

class FileTransferManager:
    """TCP-сервер приёма файлов + запуск исходящих передач в отдельных потоках."""

    def __init__(
        self,
        node_id: str,
        get_nickname: Callable[[], str],
        port: int,
        get_download_dir: Callable[[], Path],
        secret: str = "",
        on_update: Optional[Callable[[Transfer], None]] = None,
        on_offer: Optional[Callable[[FileOffer], bool]] = None,
        resolve_peer: Optional[Callable[[str, str], tuple[str, str]]] = None,
    ) -> None:
        """
        :param on_offer: вызывается в потоке приёма; должен вернуть True (принять)
            или False. Может блокироваться (ожидание решения пользователя).
        :param resolve_peer: (node_id, ip) -> (ник, ip) для отображения.
        """
        self.node_id = node_id
        self.get_nickname = get_nickname
        self.requested_port = port
        self.port = port
        self.get_download_dir = get_download_dir
        self.secret = secret
        self.on_update = on_update
        self.on_offer = on_offer
        self.resolve_peer = resolve_peer

        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._transfers: dict[str, Transfer] = {}
        self._sockets: dict[str, socket.socket] = {}
        self._workers: set[threading.Thread] = set()

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # На Windows SO_REUSEADDR позволяет «украсть» занятый порт — там не включаем.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tune_tcp_socket(srv)
        try:
            srv.bind(("", self.requested_port))
        except OSError as exc:
            log.warning("Порт файлов %d занят (%s), выбираю свободный", self.requested_port, exc)
            srv.bind(("", 0))
        srv.listen(16)
        srv.settimeout(0.5)
        self.port = srv.getsockname()[1]
        self._server = srv
        self._thread = threading.Thread(target=self._accept_loop, name="file-server", daemon=True)
        self._thread.start()
        log.info("Файлы: TCP %d (sendfile: %s)", self.port, "да" if USE_SENDFILE else "нет, chunked")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            transfers = list(self._transfers.values())
        for t in transfers:
            if not t.state.finished:
                self.cancel(t.transfer_id)
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        for w in list(self._workers):
            w.join(timeout=3)

    def get(self, transfer_id: str) -> Optional[Transfer]:
        with self._lock:
            return self._transfers.get(transfer_id)

    def all(self) -> list[Transfer]:
        with self._lock:
            return list(self._transfers.values())

    def cancel(self, transfer_id: str) -> bool:
        """Отмена передачи. Закрытие сокета мгновенно прерывает блокирующий I/O."""
        with self._lock:
            t = self._transfers.get(transfer_id)
            sock = self._sockets.get(transfer_id)
        if t is None or t.state.finished:
            return False
        t.cancel_event.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return True

    def send_file(self, peer: Peer, path: str | os.PathLike) -> Transfer:
        """Запускает отправку файла пиру в фоновом потоке."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Файл не найден: {p}")
        t = Transfer(
            transfer_id=new_id(),
            direction=TransferDirection.SEND,
            peer_id=peer.node_id,
            peer_nick=peer.nickname,
            peer_ip=peer.ip,
            name=p.name,
            size=p.stat().st_size,
            path=str(p),
        )
        with self._lock:
            self._transfers[t.transfer_id] = t
        self._notify(t)
        self._spawn(self._send_worker, t, peer.ip, peer.file_port, name=f"send-{t.transfer_id[:8]}")
        return t

    # ------------------------------------------------------------------ #

    def _spawn(self, target, *args, name: str) -> None:
        th = threading.Thread(target=self._worker_wrapper, args=(target, *args), name=name, daemon=True)
        self._workers.add(th)
        th.start()

    def _worker_wrapper(self, target, *args) -> None:
        try:
            target(*args)
        finally:
            self._workers.discard(threading.current_thread())

    def _notify(self, t: Transfer) -> None:
        if self.on_update is not None:
            try:
                self.on_update(t.snapshot())
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработчика прогресса")

    def _finish(self, t: Transfer, state: TransferState, error: str = "") -> None:
        t.state = state
        t.error = error
        t.finished_at = time.time()
        with self._lock:
            sock = self._sockets.pop(t.transfer_id, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if state == TransferState.DONE:
            log.info("Передача %s «%s» завершена (%s)", t.direction.value, t.name, t.peer_nick)
        else:
            log.info("Передача %s «%s»: %s %s", t.direction.value, t.name, state.value, error)
        self._notify(t)

    # ---------------------------- отправка ----------------------------- #

    def _send_worker(self, t: Transfer, ip: str, port: int) -> None:
        sock: Optional[socket.socket] = None
        try:
            t.state = TransferState.HASHING
            self._notify(t)
            hash_progress = _ProgressTracker(t, self._notify)
            t.sha256 = sha256_file(t.path, progress=lambda d, _tot: hash_progress(d), cancel=t.cancel_event)
            t.done = 0

            sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
            tune_tcp_socket(sock)
            with self._lock:
                self._sockets[t.transfer_id] = sock
            if t.cancel_event.is_set():
                raise Cancelled()
            sock.settimeout(IO_TIMEOUT)

            offer = {
                "proto": PROTOCOL_VERSION,
                "type": "offer",
                "id": t.transfer_id,
                "from": self.node_id,
                "nick": self.get_nickname(),
                "name": t.name,
                "size": t.size,
                "sha256": t.sha256,
            }
            offer["sig"] = sign(offer, self.secret)
            sock.sendall(MAGIC)
            send_frame(sock, offer)

            t.state = TransferState.WAITING_ACCEPT
            self._notify(t)
            sock.settimeout(ACCEPT_TIMEOUT)
            reply = recv_frame(sock)
            sock.settimeout(IO_TIMEOUT)
            if reply.get("type") == "decline":
                raise Declined(str(reply.get("reason") or "получатель отказался"))
            offset = reply.get("offset", 0)
            if reply.get("type") != "accept" or not isinstance(offset, int) or not (0 <= offset <= t.size):
                raise ProtocolError("Некорректный ответ на offer")

            t.offset = offset
            t.done = offset
            t.state = TransferState.TRANSFERRING
            self._notify(t)
            tracker = _ProgressTracker(t, self._notify)
            with open(t.path, "rb") as f:
                if os.fstat(f.fileno()).st_size != t.size:
                    raise IOError("Файл изменился после расчёта хеша")
                send_file_body(sock, f, offset, t.size, tracker, t.cancel_event)

            t.state = TransferState.VERIFYING
            self._notify(t)
            sock.settimeout(RESULT_TIMEOUT)
            result = recv_frame(sock)
            if result.get("type") == "result" and result.get("ok") is True:
                self._finish(t, TransferState.DONE)
            else:
                self._finish(t, TransferState.FAILED, str(result.get("error") or "получатель сообщил об ошибке"))
        except Declined as exc:
            self._finish(t, TransferState.DECLINED, str(exc))
        except Cancelled:
            self._finish(t, TransferState.CANCELLED)
        except (OSError, ProtocolError) as exc:
            if t.cancel_event.is_set():
                self._finish(t, TransferState.CANCELLED)
            else:
                self._finish(t, TransferState.FAILED, _describe_error(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Неожиданная ошибка отправки")
            self._finish(t, TransferState.FAILED, str(exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ----------------------------- приём ------------------------------ #

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                log.warning("Ошибка accept: %s", exc)
                time.sleep(0.2)
                continue
            self._spawn(self._receive_worker, conn, addr[0], name=f"recv-{addr[0]}")

    def _receive_worker(self, conn: socket.socket, ip: str) -> None:
        t: Optional[Transfer] = None
        part_path: Optional[Path] = None
        try:
            conn.settimeout(IO_TIMEOUT)
            tune_tcp_socket(conn)
            if recv_exact(conn, len(MAGIC)) != MAGIC:
                raise ProtocolError("Неверная сигнатура протокола")
            offer = recv_frame(conn)
            validate_offer(offer, self.secret)

            nick, peer_ip = offer["nick"][:64], ip
            if self.resolve_peer is not None:
                nick, peer_ip = self.resolve_peer(offer["from"], ip) or (nick, ip)
            t = Transfer(
                transfer_id=offer["id"][:64],
                direction=TransferDirection.RECEIVE,
                peer_id=offer["from"],
                peer_nick=nick,
                peer_ip=peer_ip,
                name=safe_filename(offer["name"]),
                size=offer["size"],
                sha256=offer["sha256"],
                state=TransferState.WAITING_ACCEPT,
            )
            with self._lock:
                if t.transfer_id in self._transfers:
                    t.transfer_id = new_id()
                self._transfers[t.transfer_id] = t
                self._sockets[t.transfer_id] = conn
            self._notify(t)

            accepted = self.on_offer(FileOffer(t)) if self.on_offer is not None else True
            if t.cancel_event.is_set():
                raise Cancelled()
            if not accepted:
                try:
                    send_frame(conn, {"type": "decline", "reason": "получатель отказался"})
                except OSError:
                    pass
                self._finish(t, TransferState.DECLINED)
                return

            download_dir = self.get_download_dir()
            download_dir.mkdir(parents=True, exist_ok=True)
            # Имя .part включает хеш — докачка возможна только того же самого файла.
            part_path = download_dir / f".{t.name}.{t.sha256[:16]}.part"
            hasher = hashlib.sha256()
            offset = 0
            if part_path.exists():
                existing = part_path.stat().st_size
                if 0 < existing <= t.size:
                    # Докачка: досчитываем хеш уже полученной части.
                    hasher = _hash_prefix(part_path, existing, t.cancel_event)
                    offset = existing
                else:
                    part_path.unlink()
            t.offset = offset
            t.done = offset

            send_frame(conn, {"type": "accept", "offset": offset})
            t.state = TransferState.TRANSFERRING
            self._notify(t)
            tracker = _ProgressTracker(t, self._notify)
            with open(part_path, "r+b" if offset else "wb") as f:
                f.seek(offset)
                receive_file_body(conn, f, hasher, offset, t.size, tracker, t.cancel_event)
                f.flush()
                os.fsync(f.fileno())

            t.state = TransferState.VERIFYING
            self._notify(t)
            if hasher.hexdigest() != t.sha256:
                part_path.unlink(missing_ok=True)
                _safe_send(conn, {"type": "result", "ok": False, "error": "хеш не совпал"})
                self._finish(t, TransferState.FAILED, "Контрольная сумма не совпала — файл удалён")
                return

            final = unique_path(download_dir, t.name)
            os.replace(part_path, final)
            part_path = None
            t.path = str(final)
            _safe_send(conn, {"type": "result", "ok": True})
            self._finish(t, TransferState.DONE)
        except Cancelled:
            if t is not None:
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                self._finish(t, TransferState.CANCELLED)
        except (OSError, ProtocolError) as exc:
            if t is None:
                log.warning("Отклонено входящее соединение от %s: %s", ip, _describe_error(exc))
            elif t.cancel_event.is_set():
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                self._finish(t, TransferState.CANCELLED)
            else:
                # .part сохраняется — повторная отправка того же файла докачает его.
                self._finish(t, TransferState.FAILED, _describe_error(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Неожиданная ошибка приёма")
            if t is not None:
                self._finish(t, TransferState.FAILED, str(exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _hash_prefix(path: Path, length: int, cancel: threading.Event):
    h = hashlib.sha256()
    buf = bytearray(IO_CHUNK)
    view = memoryview(buf)
    left = length
    with open(path, "rb") as f:
        while left > 0:
            if cancel.is_set():
                raise Cancelled()
            n = f.readinto(view[: min(IO_CHUNK, left)])
            if not n:
                break
            h.update(view[:n])
            left -= n
    return h


def _safe_send(sock: socket.socket, obj: dict) -> None:
    try:
        send_frame(sock, obj)
    except OSError as exc:
        log.debug("Не удалось отправить итог: %s", exc)


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, socket.timeout):
        return "таймаут сети"
    if isinstance(exc, ConnectionRefusedError):
        return "соединение отклонено (пир недоступен или порт закрыт брандмауэром)"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return "соединение разорвано"
    return str(exc) or exc.__class__.__name__
