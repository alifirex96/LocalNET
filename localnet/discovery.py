"""Автообнаружение узлов LocalNET в подсети через UDP broadcast + multicast.

Каждый узел раз в ``announce_interval`` секунд рассылает небольшой JSON-пакет
присутствия (presence) на ``discovery_port``: в broadcast-адреса, в multicast-группу
и напрямую на ``static_peers``. Получатели обновляют таблицу пиров; узел, от
которого не было пакетов дольше ``peer_timeout``, помечается offline.
При корректном выходе рассылается пакет ``bye`` — пир сразу уходит в offline.

Формат пакета (UTF-8 JSON, ≤ 1 КБ)::

    {"magic": "LOCALNET", "proto": 1, "type": "hello"|"bye",
     "node_id": "...", "nick": "...", "msg_port": 45893, "file_port": 45894,
     "version": "0.1.0", "ts": 1700000000.0, "sig": "<hmac или пусто>"}
"""

from __future__ import annotations

import json
import logging
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import PROTOCOL_VERSION, __version__
from .models import Peer
from .utils import canonical_json, sign, verify

log = logging.getLogger(__name__)

MAGIC = "LOCALNET"
MAX_PACKET = 1024
MAX_NICK = 64
# Допустимое расхождение часов при включённом секрете (защита от повтора пакетов).
MAX_CLOCK_SKEW = 300.0
# Через сколько offline-пиров удалять из списка совсем.
FORGET_AFTER = 3600.0


@dataclass(frozen=True)
class Presence:
    """Разобранный пакет присутствия."""

    type: str
    node_id: str
    nickname: str
    msg_port: int
    file_port: int
    version: str
    ts: float


def build_packet(
    node_id: str,
    nickname: str,
    msg_port: int,
    file_port: int,
    secret: str = "",
    ptype: str = "hello",
    ts: Optional[float] = None,
) -> bytes:
    pkt = {
        "magic": MAGIC,
        "proto": PROTOCOL_VERSION,
        "type": ptype,
        "node_id": node_id,
        "nick": nickname[:MAX_NICK],
        "msg_port": int(msg_port),
        "file_port": int(file_port),
        "version": __version__,
        "ts": round(time.time() if ts is None else ts, 3),
    }
    pkt["sig"] = sign(pkt, secret)
    data = canonical_json(pkt)
    if len(data) > MAX_PACKET:  # pragma: no cover - ник обрезан, сюда не попадаем
        raise ValueError("Пакет присутствия слишком большой")
    return data


def _valid_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


def parse_packet(data: bytes, secret: str = "", now: Optional[float] = None) -> Optional[Presence]:
    """Разбирает и валидирует пакет. Возвращает ``None`` для любого мусора."""
    if not data or len(data) > MAX_PACKET:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("magic") != MAGIC:
        return None
    if obj.get("proto") != PROTOCOL_VERSION:
        return None
    ptype = obj.get("type")
    if ptype not in ("hello", "bye"):
        return None
    node_id, nick = obj.get("node_id"), obj.get("nick")
    if not isinstance(node_id, str) or not (8 <= len(node_id) <= 64):
        return None
    if not isinstance(nick, str) or not nick.strip():
        return None
    if not _valid_port(obj.get("msg_port")) or not _valid_port(obj.get("file_port")):
        return None
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    if not verify(obj, secret):
        return None
    if secret:
        current = time.time() if now is None else now
        if abs(current - float(ts)) > MAX_CLOCK_SKEW:
            return None
    version = obj.get("version")
    return Presence(
        type=ptype,
        node_id=node_id,
        nickname=nick.strip()[:MAX_NICK],
        msg_port=obj["msg_port"],
        file_port=obj["file_port"],
        version=version if isinstance(version, str) else "",
        ts=float(ts),
    )


class PeerRegistry:
    """Потокобезопасная таблица пиров."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}

    def get(self, node_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(node_id)

    def all(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def online(self) -> list[Peer]:
        with self._lock:
            return [p for p in self._peers.values() if p.online]

    def find(self, query: str) -> Optional[Peer]:
        """Поиск по node_id, нику или IP (для CLI)."""
        q = query.strip().lower()
        with self._lock:
            peers = list(self._peers.values())
        for pred in (
            lambda p: p.node_id == q,
            lambda p: p.nickname.lower() == q,
            lambda p: p.ip == q,
            lambda p: p.node_id.startswith(q),
            lambda p: p.nickname.lower().startswith(q),
        ):
            matches = [p for p in peers if pred(p)]
            if matches:
                matches.sort(key=lambda p: (not p.online, -p.last_seen))
                return matches[0]
        return None

    def update(self, presence: Presence, ip: str, now: Optional[float] = None) -> tuple[Peer, bool, list[Peer]]:
        """Применяет пакет hello. Возвращает (пир, изменился_ли, удалённые_дубли).

        Если узел перезапустился (новый node_id с того же IP и с тем же ником),
        его старая offline-запись удаляется, чтобы список не засорялся.
        Online-записи не трогаем: на одной машине могут работать два экземпляра.
        """
        now = time.time() if now is None else now
        removed: list[Peer] = []
        with self._lock:
            peer = self._peers.get(presence.node_id)
            if peer is None:
                for old_id, old in list(self._peers.items()):
                    if not old.online and old.ip == ip and old.nickname == presence.nickname:
                        removed.append(self._peers.pop(old_id))
                peer = Peer(
                    node_id=presence.node_id,
                    nickname=presence.nickname,
                    ip=ip,
                    msg_port=presence.msg_port,
                    file_port=presence.file_port,
                    version=presence.version,
                    last_seen=now,
                    online=True,
                )
                self._peers[peer.node_id] = peer
                return peer, True, removed
            changed = (
                not peer.online
                or peer.nickname != presence.nickname
                or peer.ip != ip
                or peer.msg_port != presence.msg_port
                or peer.file_port != presence.file_port
            )
            peer.nickname = presence.nickname
            peer.ip = ip
            peer.msg_port = presence.msg_port
            peer.file_port = presence.file_port
            peer.version = presence.version
            peer.last_seen = now
            peer.online = True
            return peer, changed, removed

    def mark_offline(self, node_id: str) -> Optional[Peer]:
        with self._lock:
            peer = self._peers.get(node_id)
            if peer is None or not peer.online:
                return None
            peer.online = False
            return peer

    def expire(self, timeout: float, now: Optional[float] = None) -> tuple[list[Peer], list[Peer]]:
        """Помечает устаревших пиров offline; возвращает (ушли_в_offline, забыты)."""
        now = time.time() if now is None else now
        went_offline, forgotten = [], []
        with self._lock:
            for node_id, peer in list(self._peers.items()):
                age = now - peer.last_seen
                if peer.online and age > timeout:
                    peer.online = False
                    went_offline.append(peer)
                elif not peer.online and age > FORGET_AFTER:
                    forgotten.append(self._peers.pop(node_id))
        return went_offline, forgotten


class Discovery:
    """Фоновый поток discovery: рассылает анонсы и принимает анонсы других узлов."""

    def __init__(
        self,
        node_id: str,
        get_nickname: Callable[[], str],
        msg_port: int,
        file_port: int,
        registry: PeerRegistry,
        discovery_port: int,
        broadcast_addresses: list[str],
        multicast_group: str = "",
        static_peers: Optional[list[str]] = None,
        secret: str = "",
        interval: float = 1.0,
        timeout: float = 6.0,
        on_peer_updated: Optional[Callable[[Peer], None]] = None,
        on_peer_offline: Optional[Callable[[Peer], None]] = None,
        on_peer_removed: Optional[Callable[[Peer], None]] = None,
    ) -> None:
        self.node_id = node_id
        self.get_nickname = get_nickname
        self.msg_port = msg_port
        self.file_port = file_port
        self.registry = registry
        self.port = discovery_port
        self.broadcast_addresses = list(broadcast_addresses)
        self.multicast_group = multicast_group
        self.static_peers = list(static_peers or [])
        self.secret = secret
        self.interval = interval
        self.timeout = timeout
        self.on_peer_updated = on_peer_updated
        self.on_peer_offline = on_peer_offline
        self.on_peer_removed = on_peer_removed

        self._stop = threading.Event()
        self._announce_now = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recv_sock: Optional[socket.socket] = None
        self._send_sock: Optional[socket.socket] = None
        self._send_errors_logged: set[str] = set()

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._recv_sock = self._make_recv_socket()
        self._send_sock = self._make_send_socket()
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self._thread.start()
        log.info("Discovery запущен на UDP %d (broadcast: %s, multicast: %s)",
                 self.port, ", ".join(self.broadcast_addresses) or "-", self.multicast_group or "-")

    def stop(self) -> None:
        if self._thread is None:
            return
        try:
            self._send_all(self._packet("bye"))
        except Exception:  # noqa: BLE001
            pass
        self._stop.set()
        self._thread.join(timeout=3)
        for s in (self._recv_sock, self._send_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._thread = None
        log.info("Discovery остановлен")

    def announce_now(self) -> None:
        """Внеочередной анонс (например, после смены ника)."""
        self._announce_now.set()

    # ------------------------------------------------------------------ #

    def _make_recv_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        # SO_REUSEADDR позволяет нескольким экземплярам на одной машине
        # слушать один discovery-порт (broadcast/multicast получат все).
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", self.port))
        if self.multicast_group:
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(self.multicast_group), socket.inet_aton("0.0.0.0"))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                log.warning("Не удалось подписаться на multicast %s: %s", self.multicast_group, exc)
        s.setblocking(False)
        return s

    def _make_send_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)  # не выходим за пределы LAN
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        return s

    def _packet(self, ptype: str = "hello") -> bytes:
        return build_packet(self.node_id, self.get_nickname(), self.msg_port, self.file_port,
                            self.secret, ptype)

    def _send_all(self, data: bytes) -> None:
        if self._send_sock is None:
            return
        targets = list(self.broadcast_addresses) + list(self.static_peers)
        if self.multicast_group:
            targets.append(self.multicast_group)
        for addr in targets:
            try:
                self._send_sock.sendto(data, (addr, self.port))
            except OSError as exc:
                # Ошибка на одном адресе (нет маршрута, сеть отключена) не роняет остальные.
                if addr not in self._send_errors_logged:
                    log.warning("Не удалось отправить анонс на %s:%d: %s", addr, self.port, exc)
                    self._send_errors_logged.add(addr)
            else:
                self._send_errors_logged.discard(addr)

    def _run(self) -> None:
        next_announce = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_announce or self._announce_now.is_set():
                self._announce_now.clear()
                try:
                    self._send_all(self._packet("hello"))
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка рассылки анонса")
                next_announce = now + self.interval
                self._expire()
            wait = max(0.05, min(0.5, next_announce - time.monotonic()))
            try:
                ready, _, _ = select.select([self._recv_sock], [], [], wait)
            except (OSError, ValueError):
                if self._stop.is_set():
                    break
                time.sleep(0.2)
                continue
            if ready:
                self._drain()

    def _drain(self) -> None:
        assert self._recv_sock is not None
        for _ in range(256):  # не более N пакетов за раз — не голодаем остальным
            try:
                data, (ip, _port) = self._recv_sock.recvfrom(MAX_PACKET + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                # На Windows ICMP "port unreachable" приходит как ConnectionResetError.
                log.debug("Ошибка приёма discovery: %s", exc)
                return
            self.handle_datagram(data, ip)

    def handle_datagram(self, data: bytes, ip: str) -> None:
        presence = parse_packet(data, self.secret)
        if presence is None or presence.node_id == self.node_id:
            return
        if presence.type == "bye":
            peer = self.registry.mark_offline(presence.node_id)
            if peer is not None:
                log.info("Пир ушёл: %s", peer.display_name())
                self._emit(self.on_peer_offline, peer)
            return
        peer, changed, removed = self.registry.update(presence, ip)
        for old in removed:
            self._emit(self.on_peer_removed, old)
        if changed:
            log.info("Пир online: %s msg=%d file=%d", peer.display_name(), peer.msg_port, peer.file_port)
            self._emit(self.on_peer_updated, peer)
            # Ответный анонс — новичок узнает о нас без ожидания интервала.
            self.announce_now()

    def _expire(self) -> None:
        offline, forgotten = self.registry.expire(self.timeout)
        for peer in offline:
            log.info("Пир offline по таймауту: %s", peer.display_name())
            self._emit(self.on_peer_offline, peer)
        for peer in forgotten:
            self._emit(self.on_peer_removed, peer)

    @staticmethod
    def _emit(cb: Optional[Callable[[Peer], None]], peer: Peer) -> None:
        if cb is None:
            return
        try:
            cb(peer)
        except Exception:  # noqa: BLE001 - ошибка подписчика не должна ронять discovery
            log.exception("Ошибка в обработчике discovery")
