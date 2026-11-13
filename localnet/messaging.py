"""Текстовые сообщения по протоколу ZeroMQ (ROUTER/DEALER).

Схема:

* каждый узел держит один сокет **ROUTER**, слушающий ``tcp://*:msg_port``,
  — это его «почтовый ящик»;
* для отправки пиру открывается **DEALER** с identity = свой ``node_id``,
  подключённый к ROUTER пира; DEALER'ы кешируются по ``node_id``;
* получатель отвечает на каждое сообщение подтверждением (``ack``) через свой
  ROUTER — оно приходит обратно в DEALER отправителя. Так отправитель узнаёт,
  доставлено ли сообщение (✓) или нет (по таймауту ``ACK_TIMEOUT``);
* «общий чат» — рассылка одного и того же сообщения каждому online-пиру
  (fan-out). PUB/SUB не выбран: он не даёт подтверждений, теряет первые
  сообщения после подключения («slow joiner») и требует второй сокет/порт.

Два взаимозаменяемых транспорта с одинаковым протоколом «по проводу»:

* ``zmq`` — библиотека pyzmq (libzmq), используется, если установлена;
* ``python`` — собственная реализация ZMTP 3.0 на чистом Python (:mod:`localnet.zmtp`).
  Нужна там, где бинарные модули запрещены или не установлены (Astra Linux с ЗПС,
  запуск на системном python3 без pip). Узлы с разными транспортами совместимы.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import PROTOCOL_VERSION
from .discovery import PeerRegistry
from .models import REACTIONS, ChatMessage, Peer, new_id
from .utils import canonical_json, sign, verify
from .zmtp import ZmtpConnection, ZmtpError

try:  # pyzmq необязателен — без него работает транспорт на чистом Python
    import zmq
except Exception:  # noqa: BLE001 - ImportError или ошибка загрузки .so (ЗПС, glibc)
    zmq = None

log = logging.getLogger(__name__)

MAX_TEXT = 16 * 1024            # максимальная длина текста сообщения (символов)
MAX_READ_IDS = 500              # максимум id в одной отметке «прочитано»
MAX_WIRE_MESSAGE = 256 * 1024   # жёсткий лимит размера кадра (защита памяти)
ACK_TIMEOUT = 8.0               # сек. ожидания подтверждения доставки
POLL_MS = 50
CONNECT_TIMEOUT = 3.0
RECONNECT_DELAY = 1.0

TRANSPORTS = ("auto", "zmq", "python")


def zmq_available() -> bool:
    return zmq is not None


@dataclass
class _Pending:
    message: ChatMessage
    peer_id: str
    deadline: float


def encode_chat(msg: ChatMessage, secret: str) -> bytes:
    obj = {
        "proto": PROTOCOL_VERSION,
        "type": "chat",
        "id": msg.msg_id,
        "from": msg.sender_id,
        "nick": msg.sender_nick,
        "to": msg.to,
        "text": msg.text,
        "ts": msg.ts,
    }
    obj["sig"] = sign(obj, secret)
    return canonical_json(obj)


def decode_frame(data: bytes, secret: str) -> Optional[dict]:
    """Разбор кадра чата/ack. ``None`` — мусор или неверная подпись."""
    if len(data) > MAX_WIRE_MESSAGE:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("proto") != PROTOCOL_VERSION:
        return None
    if obj.get("type") not in ("chat", "ack", "read", "react"):
        return None
    if not isinstance(obj.get("id"), str) or not isinstance(obj.get("from"), str):
        return None
    if obj["type"] == "chat":
        if not isinstance(obj.get("text"), str) or not isinstance(obj.get("nick"), str):
            return None
        if obj.get("to") is not None and not isinstance(obj.get("to"), str):
            return None
        if not isinstance(obj.get("ts"), (int, float)):
            return None
    elif obj["type"] == "read":
        ids = obj.get("ids")
        if (not isinstance(ids, list) or not 0 < len(ids) <= MAX_READ_IDS
                or not all(isinstance(i, str) and 0 < len(i) <= 64 for i in ids)):
            return None
    elif obj["type"] == "react":
        code = obj.get("code")
        msg = obj.get("msg")
        if not isinstance(msg, str) or not 0 < len(msg) <= 64:
            return None
        if not isinstance(code, str) or (code and code not in REACTIONS):
            return None
    if not verify(obj, secret):
        return None
    return obj


def Messenger(*args, transport: str = "auto", **kwargs) -> "BaseMessenger":
    """Создаёт мессенджер с подходящим транспортом.

    ``auto`` — pyzmq, если доступен, иначе чистый Python.
    """
    if transport not in TRANSPORTS:
        log.warning("Неизвестный транспорт %r, используется auto", transport)
        transport = "auto"
    if transport == "zmq" and zmq is None:
        log.warning("pyzmq недоступен — используется транспорт на чистом Python")
        transport = "python"
    if transport == "auto":
        transport = "zmq" if zmq is not None else "python"
    cls = ZmqMessenger if transport == "zmq" else PyMessenger
    return cls(*args, **kwargs)


class BaseMessenger:
    """Общая логика: очередь отправки, подтверждения, дедупликация, таймауты."""

    transport_name = "?"

    def __init__(
        self,
        node_id: str,
        get_nickname: Callable[[], str],
        registry: PeerRegistry,
        port: int,
        secret: str = "",
        on_message: Optional[Callable[[ChatMessage], None]] = None,
        on_delivered: Optional[Callable[[str, str], None]] = None,
        on_failed: Optional[Callable[[str, str], None]] = None,
        on_read: Optional[Callable[[str, list], None]] = None,
        on_reaction: Optional[Callable[[str, str, str], None]] = None,
    ) -> None:
        self.node_id = node_id
        self.get_nickname = get_nickname
        self.registry = registry
        self.requested_port = port
        self.port = port
        self.secret = secret
        self.on_message = on_message
        self.on_delivered = on_delivered  # (msg_id, peer_id)
        self.on_failed = on_failed        # (msg_id, peer_id)
        self.on_read = on_read            # (peer_id, [msg_id])
        self.on_reaction = on_reaction    # (msg_id, peer_id, code)

        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._seen: dict[str, float] = {}  # защита от дублей при повторной доставке
        self._stop = threading.Event()

    # ---------------------------------------------------- публичный API

    def start(self) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def forget_peer(self, node_id: str) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def send(self, text: str, to: Optional[str] = None) -> ChatMessage:
        """Ставит сообщение в очередь. ``to=None`` — всем online-пирам."""
        text = text.strip()
        if not text:
            raise ValueError("Пустое сообщение")
        if len(text) > MAX_TEXT:
            raise ValueError(f"Сообщение длиннее {MAX_TEXT} символов")
        msg = ChatMessage(
            msg_id=new_id(),
            sender_id=self.node_id,
            sender_nick=self.get_nickname(),
            text=text,
            to=to,
            outgoing=True,
        )
        self._submit(msg)
        return msg

    def _submit(self, msg: ChatMessage) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def _submit_raw(self, peer_ids: list[str], payload: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def send_read(self, peer_id: str, msg_ids: list[str]) -> None:
        """Отметка «прочитано» отправителю сообщений (без подтверждения доставки)."""
        for i in range(0, len(msg_ids), MAX_READ_IDS):
            obj = {"proto": PROTOCOL_VERSION, "type": "read", "id": new_id(), "from": self.node_id,
                   "ids": list(msg_ids[i:i + MAX_READ_IDS])}
            obj["sig"] = sign(obj, self.secret)
            self._submit_raw([peer_id], canonical_json(obj))

    def send_reaction(self, msg_id: str, code: str, peer_ids: list[str]) -> None:
        """Реакция на сообщение (``code=""`` — снять реакцию) — всем участникам чата."""
        if code and code not in REACTIONS:
            raise ValueError(f"Неизвестная реакция {code!r}")
        obj = {"proto": PROTOCOL_VERSION, "type": "react", "id": new_id(), "from": self.node_id,
               "msg": msg_id, "code": code}
        obj["sig"] = sign(obj, self.secret)
        self._submit_raw(list(peer_ids), canonical_json(obj))

    def _send_raw_now(self, peer_ids: list[str], payload: bytes) -> None:
        for pid in peer_ids:
            peer = self.registry.get(pid)
            if peer is not None and peer.online:
                self._transport_send(peer, payload)

    def _transport_send(self, peer: Peer, payload: bytes) -> bool:  # pragma: no cover
        raise NotImplementedError

    # ---------------------------------------------------- общая логика

    def _do_send(self, msg: ChatMessage) -> None:
        if msg.to is None:
            targets = self.registry.online()
        else:
            peer = self.registry.get(msg.to)
            targets = [peer] if peer is not None else []
        if not targets:
            if msg.to is not None:
                self._emit(self.on_failed, msg.msg_id, msg.to)
            return
        payload = encode_chat(msg, self.secret)
        for peer in targets:
            if not peer.online:
                self._emit(self.on_failed, msg.msg_id, peer.node_id)
                continue
            # Регистрируем ожидание до отправки: ack может прийти очень быстро.
            with self._lock:
                self._pending[(msg.msg_id, peer.node_id)] = _Pending(
                    msg, peer.node_id, time.monotonic() + ACK_TIMEOUT)
            if not self._transport_send(peer, payload):
                with self._lock:
                    self._pending.pop((msg.msg_id, peer.node_id), None)
                self._emit(self.on_failed, msg.msg_id, peer.node_id)

    def _handle_incoming(self, identity: bytes, data: bytes) -> Optional[bytes]:
        """Обрабатывает входящий кадр; возвращает ack для отправки или None."""
        obj = decode_frame(data, self.secret)
        if obj is None or obj["type"] == "ack":
            log.debug("Отброшен некорректный кадр от %r", identity[:32])
            return None
        # identity DEALER'а = node_id отправителя: сверяем, чтобы нельзя было подделать поле from.
        if identity.decode("ascii", "replace") != obj["from"]:
            log.debug("identity не совпадает с полем from — отброшено")
            return None
        if obj["type"] == "read":
            self._call(self.on_read, obj["from"], obj["ids"])
            return None
        if obj["type"] == "react":
            self._call(self.on_reaction, obj["msg"], obj["from"], obj["code"])
            return None
        ack = {"proto": PROTOCOL_VERSION, "type": "ack", "id": obj["id"], "from": self.node_id}
        ack["sig"] = sign(ack, self.secret)

        now = time.monotonic()
        with self._lock:
            duplicate = obj["id"] in self._seen
            self._seen[obj["id"]] = now
            if len(self._seen) > 5000:
                cutoff = now - 600
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if duplicate:
            return canonical_json(ack)  # подтверждаем повторно, но не показываем

        to = obj.get("to")
        if to is None or to == self.node_id:
            peer = self.registry.get(obj["from"])
            msg = ChatMessage(
                msg_id=obj["id"],
                sender_id=obj["from"],
                sender_nick=obj["nick"][:64],
                text=obj["text"][:MAX_TEXT],
                to=to,
                ts=float(obj["ts"]),
                sender_ip=peer.ip if peer else "",
            )
            if self.on_message is not None:
                try:
                    self.on_message(msg)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработчика сообщения")
        return canonical_json(ack)

    def _handle_ack(self, data: bytes) -> None:
        obj = decode_frame(data, self.secret)
        if obj is None or obj["type"] != "ack":
            return
        with self._lock:
            pending = self._pending.pop((obj["id"], obj["from"]), None)
        if pending is not None:
            self._emit(self.on_delivered, obj["id"], obj["from"])

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._pending:
                return
            expired = [k for k, p in self._pending.items() if now > p.deadline]
            for key in expired:
                del self._pending[key]
        for msg_id, peer_id in expired:
            log.info("Сообщение %s не подтверждено пиром %s", msg_id[:8], peer_id[:8])
            self._emit(self.on_failed, msg_id, peer_id)

    @staticmethod
    def _call(cb: Optional[Callable], *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработчика")

    @staticmethod
    def _emit(cb: Optional[Callable[[str, str], None]], msg_id: str, peer_id: str) -> None:
        if cb is None:
            return
        try:
            cb(msg_id, peer_id)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработчика статуса сообщения")


# =========================================================================== #
# Транспорт pyzmq
# =========================================================================== #

class ZmqMessenger(BaseMessenger):
    """ZeroMQ-сокеты не потокобезопасны: ими владеет один поток ``messaging``."""

    transport_name = "zmq"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        assert zmq is not None
        self._ctx = zmq.Context.instance()
        self._router = None
        self._poller = None
        self._dealers: dict = {}  # node_id -> (endpoint, socket)
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Привязывает ROUTER. Если порт занят — берёт свободный (он анонсируется)."""
        router = self._ctx.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        router.setsockopt(zmq.MAXMSGSIZE, MAX_WIRE_MESSAGE)
        router.setsockopt(zmq.ROUTER_HANDOVER, 1)  # переподключение пира с тем же identity
        router.setsockopt(zmq.RCVHWM, 10_000)
        try:
            if self.requested_port:
                router.bind(f"tcp://*:{self.requested_port}")
                self.port = self.requested_port
            else:
                self.port = router.bind_to_random_port("tcp://*", 49152, 65535)
        except zmq.ZMQError as exc:
            log.warning("Порт сообщений %d занят (%s), выбираю свободный", self.requested_port, exc)
            self.port = router.bind_to_random_port("tcp://*", 49152, 65535)
        self._router = router
        self._thread = threading.Thread(target=self._run, name="messaging", daemon=True)
        self._thread.start()
        log.info("Сообщения: ZeroMQ (pyzmq) ROUTER на TCP %d", self.port)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3)
        self._thread = None

    def forget_peer(self, node_id: str) -> None:
        self._commands.put(("forget", node_id))

    def _submit(self, msg: ChatMessage) -> None:
        self._commands.put(("send", msg))

    def _submit_raw(self, peer_ids: list[str], payload: bytes) -> None:
        self._commands.put(("raw", peer_ids, payload))

    def _run(self) -> None:
        self._poller = zmq.Poller()
        self._poller.register(self._router, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                self._process_commands()
                try:
                    events = dict(self._poller.poll(POLL_MS))
                except zmq.ZMQError as exc:
                    log.warning("Ошибка poll: %s", exc)
                    time.sleep(0.1)
                    continue
                for sock in events:
                    self._drain(sock)
                self._check_timeouts()
        except Exception:  # noqa: BLE001
            log.exception("Поток сообщений аварийно завершён")
        finally:
            self._close_all()

    def _process_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd[0] == "send":
                    self._do_send(cmd[1])
                elif cmd[0] == "raw":
                    self._send_raw_now(cmd[1], cmd[2])
                elif cmd[0] == "forget":
                    self._close_dealer(cmd[1])
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки команды %s", cmd[0])

    def _transport_send(self, peer: Peer, payload: bytes) -> bool:
        dealer = self._get_dealer(peer)
        try:
            dealer.send(payload, zmq.NOBLOCK)
            return True
        except zmq.ZMQError as exc:
            log.warning("Не удалось отправить сообщение %s: %s", peer.display_name(), exc)
            return False

    def _get_dealer(self, peer: Peer):
        endpoint = peer.msg_endpoint
        cached = self._dealers.get(peer.node_id)
        if cached is not None:
            if cached[0] == endpoint:
                return cached[1]
            self._close_dealer(peer.node_id)
        sock = self._ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.IDENTITY, self.node_id.encode("ascii"))
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SNDHWM, 1000)
        sock.setsockopt(zmq.MAXMSGSIZE, MAX_WIRE_MESSAGE)
        sock.setsockopt(zmq.RECONNECT_IVL, 500)
        sock.setsockopt(zmq.RECONNECT_IVL_MAX, 5000)
        # Heartbeat ZMTP — обнаружение «мёртвых» TCP-соединений.
        sock.setsockopt(zmq.HEARTBEAT_IVL, 5000)
        sock.setsockopt(zmq.HEARTBEAT_TIMEOUT, 15000)
        sock.connect(endpoint)
        self._poller.register(sock, zmq.POLLIN)
        self._dealers[peer.node_id] = (endpoint, sock)
        return sock

    def _close_dealer(self, node_id: str) -> None:
        cached = self._dealers.pop(node_id, None)
        if cached is None:
            return
        try:
            self._poller.unregister(cached[1])
        except KeyError:
            pass
        cached[1].close(linger=0)

    def _drain(self, sock) -> None:
        for _ in range(1000):
            try:
                frames = sock.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return
            except zmq.ZMQError as exc:
                log.debug("Ошибка приёма: %s", exc)
                return
            if sock is self._router:
                if len(frames) != 2:
                    continue
                identity, data = frames
                ack = self._handle_incoming(identity, data)
                if ack is not None:
                    try:
                        self._router.send_multipart([identity, ack], zmq.NOBLOCK)
                    except zmq.ZMQError as exc:
                        log.debug("Не удалось отправить ack: %s", exc)
            elif len(frames) == 1:
                self._handle_ack(frames[0])

    def _close_all(self) -> None:
        for _endpoint, sock in self._dealers.values():
            sock.close(linger=0)
        self._dealers.clear()
        if self._router is not None:
            self._router.close(linger=0)
            self._router = None


# =========================================================================== #
# Транспорт на чистом Python (ZMTP 3.0)
# =========================================================================== #

class _OutgoingLink:
    """Исходящее соединение DEALER к одному пиру: очередь + поток отправки + поток чтения ack."""

    def __init__(self, owner: "PyMessenger", node_id: str, ip: str, port: int) -> None:
        self.owner = owner
        self.node_id = node_id
        self.addr = (ip, port)
        self.queue: "queue.Queue[bytes]" = queue.Queue(maxsize=1000)
        self.closed = threading.Event()
        self.conn: Optional[ZmtpConnection] = None
        self.thread = threading.Thread(target=self._run, name=f"msg-out-{node_id[:8]}", daemon=True)
        self.thread.start()

    def enqueue(self, payload: bytes) -> bool:
        try:
            self.queue.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self.closed.set()
        conn = self.conn
        if conn is not None:
            conn.close()

    def _connect(self) -> ZmtpConnection:
        sock = socket.create_connection(self.addr, timeout=CONNECT_TIMEOUT)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn = ZmtpConnection(sock, MAX_WIRE_MESSAGE)
        conn.handshake("DEALER", identity=self.owner.node_id.encode("ascii"))
        sock.settimeout(None)
        reader = threading.Thread(target=self._read_acks, args=(conn,), name=f"msg-ack-{self.node_id[:8]}",
                                  daemon=True)
        reader.start()
        return conn

    def _run(self) -> None:
        payload: Optional[bytes] = None
        while not self.closed.is_set() and not self.owner._stop.is_set():
            if payload is None:
                try:
                    payload = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
            try:
                if self.conn is None:
                    self.conn = self._connect()
                self.conn.send_message([payload])
                payload = None
            except (OSError, ZmtpError) as exc:
                # Как и libzmq: сообщение остаётся в очереди, пробуем переподключиться.
                # Если пир так и не ответит — сработает таймаут подтверждения.
                log.debug("Соединение с %s:%d: %s", *self.addr, exc)
                if self.conn is not None:
                    self.conn.close()
                    self.conn = None
                self.closed.wait(RECONNECT_DELAY)
        if self.conn is not None:
            self.conn.close()

    def _read_acks(self, conn: ZmtpConnection) -> None:
        try:
            while not self.closed.is_set():
                frames = conn.recv_message()
                if len(frames) == 1:
                    self.owner._handle_ack(frames[0])
        except (OSError, ZmtpError):
            pass
        finally:
            if self.conn is conn:
                conn.close()


class PyMessenger(BaseMessenger):
    """Транспорт без бинарных зависимостей: блокирующие сокеты и потоки."""

    transport_name = "python"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._server: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._links: dict[str, _OutgoingLink] = {}
        self._links_lock = threading.Lock()
        self._incoming: set[ZmtpConnection] = set()

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if not _is_windows():  # на Windows SO_REUSEADDR позволяет «украсть» занятый порт
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self.requested_port))
        except OSError as exc:
            log.warning("Порт сообщений %d занят (%s), выбираю свободный", self.requested_port, exc)
            srv.bind(("", 0))
        srv.listen(64)
        srv.settimeout(0.5)
        self.port = srv.getsockname()[1]
        self._server = srv
        for target, name in ((self._accept_loop, "msg-server"), (self._timeout_loop, "msg-timeouts")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        log.info("Сообщения: ZeroMQ-совместимый транспорт на чистом Python, TCP %d", self.port)

    def stop(self) -> None:
        if self._server is None:
            return
        self._stop.set()
        with self._links_lock:
            links = list(self._links.values())
            self._links.clear()
        for link in links:
            link.close()
        for conn in list(self._incoming):
            conn.close()
        for th in self._threads:
            th.join(timeout=3)
        try:
            self._server.close()
        except OSError:
            pass
        self._server = None

    def forget_peer(self, node_id: str) -> None:
        with self._links_lock:
            link = self._links.pop(node_id, None)
        if link is not None:
            link.close()

    def _submit(self, msg: ChatMessage) -> None:
        # Отправка неблокирующая (только постановка в очереди), можно звать из GUI-потока.
        try:
            self._do_send(msg)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка отправки сообщения")

    def _submit_raw(self, peer_ids: list[str], payload: bytes) -> None:
        self._send_raw_now(peer_ids, payload)

    def _transport_send(self, peer: Peer, payload: bytes) -> bool:
        with self._links_lock:
            link = self._links.get(peer.node_id)
            if link is not None and (link.addr != (peer.ip, peer.msg_port) or link.closed.is_set()):
                link.close()
                link = None
            if link is None:
                link = _OutgoingLink(self, peer.node_id, peer.ip, peer.msg_port)
                self._links[peer.node_id] = link
        return link.enqueue(payload)

    def _timeout_loop(self) -> None:
        while not self._stop.wait(0.2):
            self._check_timeouts()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.2)
                continue
            threading.Thread(target=self._serve, args=(sock, addr), name=f"msg-in-{addr[0]}",
                             daemon=True).start()

    def _serve(self, sock: socket.socket, addr) -> None:
        conn = ZmtpConnection(sock, MAX_WIRE_MESSAGE)
        self._incoming.add(conn)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            conn.handshake("ROUTER")
            sock.settimeout(None)
            identity = conn.peer_identity
            while not self._stop.is_set():
                frames = conn.recv_message()
                if not frames:
                    continue
                ack = self._handle_incoming(identity, frames[-1])
                if ack is not None:
                    conn.send_message([ack])
        except (OSError, ZmtpError) as exc:
            log.debug("Входящее соединение %s: %s", addr[0], exc)
        finally:
            self._incoming.discard(conn)
            conn.close()


def _is_windows() -> bool:
    import os
    return os.name == "nt"
