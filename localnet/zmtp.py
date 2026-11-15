"""Минимальная реализация протокола ZeroMQ (ZMTP 3.0, механизм NULL) на чистом Python.

Зачем: на Astra Linux в режиме замкнутой программной среды (ЗПС) запрещена загрузка
неподписанных бинарных библиотек — в том числе ``.so`` из pip-пакета ``pyzmq``.
Системный ``python3`` подписан, поэтому чисто-Python-код работает всегда.

Модуль реализует ровно то, что нужно LocalNET: роль ROUTER (входящие соединения) и
DEALER (исходящие), обмен одно-/многокадровыми сообщениями и ответ на PING.
Реализация совместима «по проводу» с libzmq: узел без pyzmq общается с узлом,
использующим pyzmq, и наоборот.

Спецификация: https://rfc.zeromq.org/spec/23/ (ZMTP 3.0), https://rfc.zeromq.org/spec/37/ (3.1, PING).
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Optional

FLAG_MORE = 0x01
FLAG_LONG = 0x02
FLAG_COMMAND = 0x04

GREETING_SIZE = 64


class ZmtpError(Exception):
    """Нарушение протокола или несовместимый собеседник."""


def build_greeting() -> bytes:
    """64-байтное приветствие: сигнатура, версия 3.0, механизм NULL, as-server=0."""
    signature = b"\xff" + b"\x00" * 8 + b"\x7f"
    version = b"\x03\x00"
    mechanism = b"NULL".ljust(20, b"\x00")
    as_server = b"\x00"
    filler = b"\x00" * 31
    greeting = signature + version + mechanism + as_server + filler
    assert len(greeting) == GREETING_SIZE
    return greeting


def parse_greeting(data: bytes) -> tuple[int, int]:
    """Проверяет приветствие собеседника, возвращает (major, minor)."""
    if len(data) != GREETING_SIZE or data[0] != 0xFF or data[9] != 0x7F:
        raise ZmtpError("Собеседник не говорит на ZMTP")
    major, minor = data[10], data[11]
    if major < 3:
        raise ZmtpError(f"Неподдерживаемая версия ZMTP {major}.{minor}")
    mechanism = data[12:32].rstrip(b"\x00")
    if mechanism != b"NULL":
        raise ZmtpError(f"Неподдерживаемый механизм безопасности {mechanism!r}")
    return major, minor


def encode_frame(body: bytes, more: bool = False, command: bool = False) -> bytes:
    flags = (FLAG_MORE if more else 0) | (FLAG_COMMAND if command else 0)
    if len(body) > 255:
        return struct.pack(">BQ", flags | FLAG_LONG, len(body)) + body
    return struct.pack(">BB", flags, len(body)) + body


def encode_message(frames: list[bytes]) -> bytes:
    if not frames:
        raise ValueError("Сообщение должно содержать хотя бы один кадр")
    last = len(frames) - 1
    return b"".join(encode_frame(f, more=(i < last)) for i, f in enumerate(frames))


def encode_command(name: bytes, data: bytes = b"") -> bytes:
    return encode_frame(bytes([len(name)]) + name + data, command=True)


def encode_ready(socket_type: str, identity: Optional[bytes] = None) -> bytes:
    props = _prop(b"Socket-Type", socket_type.encode("ascii"))
    if identity is not None:
        props += _prop(b"Identity", identity)
    return encode_command(b"READY", props)


def _prop(name: bytes, value: bytes) -> bytes:
    return bytes([len(name)]) + name + struct.pack(">I", len(value)) + value


def parse_properties(data: bytes) -> dict[str, bytes]:
    props: dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        nlen = data[pos]
        pos += 1
        name = data[pos:pos + nlen]
        pos += nlen
        if pos + 4 > len(data):
            raise ZmtpError("Обрезанные свойства READY")
        (vlen,) = struct.unpack(">I", data[pos:pos + 4])
        pos += 4
        value = data[pos:pos + vlen]
        if len(value) != vlen or len(name) != nlen:
            raise ZmtpError("Обрезанные свойства READY")
        pos += vlen
        props[name.decode("ascii", "replace").lower()] = value
    return props


def split_command(body: bytes) -> tuple[bytes, bytes]:
    if not body:
        raise ZmtpError("Пустая команда")
    nlen = body[0]
    if 1 + nlen > len(body):
        raise ZmtpError("Обрезанная команда")
    return body[1:1 + nlen], body[1 + nlen:]


class ZmtpConnection:
    """Одно установленное ZMTP-соединение поверх блокирующего TCP-сокета.

    Чтение и запись можно вести из разных потоков: все записи идут под
    внутренней блокировкой, поэтому ответ PONG не перемешается с сообщением.
    """

    # Совместимость ролей: кому с кем разрешено разговаривать (ZMTP 3.0, раздел 5).
    _COMPATIBLE = {"ROUTER": {"DEALER", "REQ", "ROUTER"}, "DEALER": {"ROUTER", "REP", "DEALER"}}

    def __init__(self, sock: socket.socket, max_message: int) -> None:
        self.sock = sock
        self.max_message = max_message
        self.peer_identity: bytes = b""
        self.peer_socket_type: str = ""
        self._wlock = threading.Lock()

    def _send(self, data: bytes) -> None:
        with self._wlock:
            self.sock.sendall(data)

    # ---------------------------------------------------------------- I/O

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray(n)
        view = memoryview(buf)
        got = 0
        while got < n:
            r = self.sock.recv_into(view[got:], n - got)
            if r == 0:
                raise ConnectionError("Соединение закрыто")
            got += r
        return bytes(buf)

    def _recv_frame(self) -> tuple[int, bytes]:
        flags = self._recv_exact(1)[0]
        if flags & ~(FLAG_MORE | FLAG_LONG | FLAG_COMMAND):
            raise ZmtpError(f"Неизвестные флаги кадра {flags:#x}")
        if flags & FLAG_LONG:
            (size,) = struct.unpack(">Q", self._recv_exact(8))
        else:
            size = self._recv_exact(1)[0]
        if size > self.max_message:
            raise ZmtpError(f"Кадр {size} байт превышает лимит {self.max_message}")
        return flags, self._recv_exact(size) if size else b""

    # ----------------------------------------------------------- handshake

    def handshake(self, socket_type: str, identity: Optional[bytes] = None) -> None:
        """Обмен приветствиями и командами READY. Бросает ZmtpError/OSError."""
        self._send(build_greeting())
        parse_greeting(self._recv_exact(GREETING_SIZE))
        self._send(encode_ready(socket_type, identity))
        flags, body = self._recv_frame()
        if not flags & FLAG_COMMAND:
            raise ZmtpError("Ожидалась команда READY")
        name, data = split_command(body)
        if name != b"READY":
            raise ZmtpError(f"Ожидалась команда READY, получено {name!r}")
        props = parse_properties(data)
        self.peer_socket_type = props.get("socket-type", b"").decode("ascii", "replace").upper()
        if self.peer_socket_type not in self._COMPATIBLE.get(socket_type, set()):
            raise ZmtpError(f"Несовместимый тип сокета собеседника: {self.peer_socket_type}")
        self.peer_identity = props.get("identity", b"")

    # ------------------------------------------------------------ messages

    def recv_message(self) -> list[bytes]:
        """Читает следующее сообщение (список кадров); команды PING обрабатывает сам."""
        frames: list[bytes] = []
        total = 0
        while True:
            flags, body = self._recv_frame()
            if flags & FLAG_COMMAND:
                self._handle_command(body)
                continue
            frames.append(body)
            total += len(body)
            if total > self.max_message:
                raise ZmtpError("Сообщение превышает лимит")
            if not flags & FLAG_MORE:
                return frames

    def _handle_command(self, body: bytes) -> None:
        name, data = split_command(body)
        if name == b"PING":
            # PING: 2 байта TTL + контекст; PONG возвращает контекст.
            self._send(encode_command(b"PONG", data[2:]))
        # PONG, ERROR и прочие команды игнорируем.

    def send_message(self, frames: list[bytes]) -> None:
        self._send(encode_message(frames))

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
