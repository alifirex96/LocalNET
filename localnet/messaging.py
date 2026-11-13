"""Текстовые сообщения поверх ZeroMQ (ROUTER/DEALER).

Схема:

* каждый узел держит один сокет **ROUTER**, привязанный к ``tcp://*:msg_port``,
  — это его «почтовый ящик»;
* для отправки пиру создаётся сокет **DEALER** с identity = свой ``node_id``,
  подключённый к ROUTER пира; DEALER'ы кешируются по ``node_id``;
* получатель отвечает на каждое сообщение подтверждением (``ack``) через свой
  ROUTER — оно приходит обратно в DEALER отправителя. Так отправитель узнаёт,
  доставлено ли сообщение (✓) или нет (по таймауту ``ACK_TIMEOUT``);
* «общий чат» — это рассылка одного и того же сообщения каждому online-пиру
  (fan-out). PUB/SUB не выбран, т.к. он не даёт подтверждений, теряет первые
  сообщения после подключения («slow joiner») и требует второй сокет/порт.

ZeroMQ-сокеты не потокобезопасны, поэтому ими владеет единственный поток
``messaging``; остальные потоки передают ему команды через очередь.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import zmq

from . import PROTOCOL_VERSION
from .discovery import PeerRegistry
from .models import ChatMessage, Peer, new_id
from .utils import canonical_json, sign, verify

log = logging.getLogger(__name__)

MAX_TEXT = 16 * 1024            # максимальная длина текста сообщения (символов)
MAX_WIRE_MESSAGE = 256 * 1024   # жёсткий лимит размера кадра ZeroMQ (защита памяти)
ACK_TIMEOUT = 8.0               # сек. ожидания подтверждения доставки
POLL_MS = 50


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
    if obj.get("type") not in ("chat", "ack"):
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
    if not verify(obj, secret):
        return None
    return obj


class Messenger:
    """Поток обмена сообщениями."""

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

        self._ctx = zmq.Context.instance()
        self._router: Optional[zmq.Socket] = None
        self._dealers: dict[str, tuple[str, zmq.Socket]] = {}  # node_id -> (endpoint, socket)
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._seen: dict[str, float] = {}  # защита от дублей при повторной доставке
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # Публичный API (вызывается из любых потоков)
    # ------------------------------------------------------------------ #

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
        log.info("Сообщения: ZeroMQ ROUTER на TCP %d", self.port)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3)
        self._thread = None

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
        self._commands.put(("send", msg))
        return msg

    def forget_peer(self, node_id: str) -> None:
        """Закрыть DEALER пира (ушёл offline / сменил адрес)."""
        self._commands.put(("forget", node_id))

    # ------------------------------------------------------------------ #
    # Поток messaging
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        poller = zmq.Poller()
        assert self._router is not None
        poller.register(self._router, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                self._process_commands(poller)
                try:
                    events = dict(poller.poll(POLL_MS))
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

    def _process_commands(self, poller: zmq.Poller) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd[0] == "send":
                    self._do_send(cmd[1], poller)
                elif cmd[0] == "forget":
                    self._close_dealer(cmd[1], poller)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки команды %s", cmd[0])

    def _do_send(self, msg: ChatMessage, poller: zmq.Poller) -> None:
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
            dealer = self._get_dealer(peer, poller)
            try:
                dealer.send(payload, zmq.NOBLOCK)
            except zmq.ZMQError as exc:
                log.warning("Не удалось отправить сообщение %s: %s", peer.display_name(), exc)
                self._emit(self.on_failed, msg.msg_id, peer.node_id)
                continue
            self._pending[(msg.msg_id, peer.node_id)] = _Pending(msg, peer.node_id, time.monotonic() + ACK_TIMEOUT)

    def _get_dealer(self, peer: Peer, poller: zmq.Poller) -> zmq.Socket:
        endpoint = peer.msg_endpoint
        cached = self._dealers.get(peer.node_id)
        if cached is not None:
            if cached[0] == endpoint:
                return cached[1]
            self._close_dealer(peer.node_id, poller)
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
        poller.register(sock, zmq.POLLIN)
        self._dealers[peer.node_id] = (endpoint, sock)
        return sock

    def _close_dealer(self, node_id: str, poller: zmq.Poller) -> None:
        cached = self._dealers.pop(node_id, None)
        if cached is None:
            return
        try:
            poller.unregister(cached[1])
        except KeyError:
            pass
        cached[1].close(linger=0)

    def _drain(self, sock: zmq.Socket) -> None:
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
                self._handle_incoming(identity, data)
            else:
                if len(frames) != 1:
                    continue
                self._handle_ack(frames[0])

    def _handle_incoming(self, identity: bytes, data: bytes) -> None:
        obj = decode_frame(data, self.secret)
        if obj is None or obj["type"] != "chat":
            log.debug("Отброшен некорректный кадр от %r", identity[:32])
            return
        # identity DEALER'а = node_id отправителя: сверяем, чтобы нельзя было подделать поле from.
        if identity.decode("ascii", "replace") != obj["from"]:
            log.debug("identity не совпадает с полем from — отброшено")
            return
        ack = {"proto": PROTOCOL_VERSION, "type": "ack", "id": obj["id"], "from": self.node_id}
        ack["sig"] = sign(ack, self.secret)
        try:
            assert self._router is not None
            self._router.send_multipart([identity, canonical_json(ack)], zmq.NOBLOCK)
        except zmq.ZMQError as exc:
            log.debug("Не удалось отправить ack: %s", exc)

        now = time.monotonic()
        if obj["id"] in self._seen:
            return  # дубль — подтверждаем повторно, но не показываем
        self._seen[obj["id"]] = now
        if len(self._seen) > 5000:
            cutoff = now - 600
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        to = obj.get("to")
        if to is not None and to != self.node_id:
            return  # личное сообщение не нам
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

    def _handle_ack(self, data: bytes) -> None:
        obj = decode_frame(data, self.secret)
        if obj is None or obj["type"] != "ack":
            return
        pending = self._pending.pop((obj["id"], obj["from"]), None)
        if pending is not None:
            self._emit(self.on_delivered, obj["id"], obj["from"])

    def _check_timeouts(self) -> None:
        if not self._pending:
            return
        now = time.monotonic()
        for key, pending in list(self._pending.items()):
            if now > pending.deadline:
                del self._pending[key]
                log.info("Сообщение %s не подтверждено пиром %s", key[0][:8], key[1][:8])
                self._emit(self.on_failed, key[0], key[1])

    def _close_all(self) -> None:
        for _endpoint, sock in self._dealers.values():
            sock.close(linger=0)
        self._dealers.clear()
        if self._router is not None:
            self._router.close(linger=0)
            self._router = None

    @staticmethod
    def _emit(cb: Optional[Callable[[str, str], None]], msg_id: str, peer_id: str) -> None:
        if cb is None:
            return
        try:
            cb(msg_id, peer_id)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработчика статуса сообщения")
