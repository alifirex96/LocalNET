"""Тесты кодека ZMTP 3.0 (транспорт сообщений на чистом Python)."""

import socket
import struct
import threading

import pytest

from localnet.zmtp import (
    GREETING_SIZE,
    ZmtpConnection,
    ZmtpError,
    build_greeting,
    encode_command,
    encode_frame,
    encode_message,
    encode_ready,
    parse_greeting,
    parse_properties,
    split_command,
)


def test_greeting_roundtrip():
    g = build_greeting()
    assert len(g) == GREETING_SIZE
    assert parse_greeting(g) == (3, 0)


@pytest.mark.parametrize("mutate", [
    lambda g: b"\x00" + g[1:],                       # нет сигнатуры
    lambda g: g[:10] + b"\x02\x00" + g[12:],         # ZMTP 2
    lambda g: g[:12] + b"CURVE".ljust(20, b"\x00") + g[32:],
    lambda g: g[:-1],
])
def test_bad_greeting(mutate):
    with pytest.raises(ZmtpError):
        parse_greeting(mutate(build_greeting()))


def test_short_and_long_frames():
    assert encode_frame(b"abc") == b"\x00\x03abc"
    assert encode_frame(b"abc", more=True) == b"\x01\x03abc"
    long = encode_frame(b"x" * 300)
    assert long[0] == 0x02 and struct.unpack(">Q", long[1:9])[0] == 300


def test_message_more_flags():
    data = encode_message([b"id", b"", b"payload"])
    assert data == b"\x01\x02id" + b"\x01\x00" + b"\x00\x07payload"


def test_ready_properties():
    frame = encode_ready("DEALER", b"node")
    assert frame[0] == 0x04
    name, data = split_command(frame[2:])
    assert name == b"READY"
    assert parse_properties(data) == {"socket-type": b"DEALER", "identity": b"node"}


def test_truncated_properties():
    with pytest.raises(ZmtpError):
        parse_properties(b"\x0bSocket-Type\x00\x00\x00\x09ROU")


def _pair(max_message=1024):
    a, b = socket.socketpair()
    return ZmtpConnection(a, max_message), ZmtpConnection(b, max_message)


def test_handshake_and_messages_with_ping():
    dealer, router = _pair()
    errors = []

    def server():
        try:
            router.handshake("ROUTER")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=server)
    th.start()
    dealer.handshake("DEALER", identity=b"me")
    th.join(5)
    assert not errors
    assert router.peer_identity == b"me" and router.peer_socket_type == "DEALER"
    assert dealer.peer_socket_type == "ROUTER"

    # PING от «libzmq» посреди потока: получатель отвечает PONG и выдаёт сообщение.
    dealer.sock.sendall(encode_command(b"PING", b"\x00\x0actx") + encode_message([b"hello"]))
    assert router.recv_message() == [b"hello"]
    flags_size = dealer.sock.recv(2)
    assert flags_size[0] == 0x04
    pong = dealer.sock.recv(flags_size[1])
    assert split_command(pong) == (b"PONG", b"ctx")
    dealer.close()
    router.close()


def test_incompatible_socket_type():
    """DEALER отказывается разговаривать с PUB."""
    a, b = _pair()
    errors = []

    def dealer():
        try:
            b.handshake("DEALER", identity=b"x")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=dealer)
    th.start()
    a._send(build_greeting())          # «собеседник» представляется PUB-сокетом
    parse_greeting(a._recv_exact(GREETING_SIZE))
    a._send(encode_ready("PUB"))
    th.join(5)
    assert len(errors) == 1 and isinstance(errors[0], ZmtpError)
    a.close()
    b.close()


def test_oversized_frame_rejected():
    a, b = _pair(max_message=10)
    a.sock.sendall(encode_frame(b"x" * 100))
    with pytest.raises(ZmtpError):
        b.recv_message()
    a.close()
    b.close()
