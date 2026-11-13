"""Тесты обмена сообщениями через ZeroMQ на loopback."""

import threading
import time

import pytest

from localnet.discovery import PeerRegistry, Presence
from localnet.messaging import Messenger, decode_frame, encode_chat
from localnet.models import ChatMessage


class Node:
    def __init__(self, node_id: str, nick: str, secret: str = ""):
        self.registry = PeerRegistry()
        self.inbox: list[ChatMessage] = []
        self.delivered: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.cond = threading.Condition()
        self.m = Messenger(node_id, lambda: nick, self.registry, 0, secret=secret,
                           on_message=self._add(self.inbox), on_delivered=self._add2(self.delivered),
                           on_failed=self._add2(self.failed))
        self.m.start()

    def _add(self, lst):
        return lambda x: lst.append(x)

    def _add2(self, lst):
        return lambda a, b: lst.append((a, b))

    def know(self, other: "Node"):
        self.registry.update(Presence("hello", other.m.node_id, "x", other.m.port, 1, "", time.time()), "127.0.0.1")


def wait_for(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def nodes():
    created = []

    def make(*args, **kw):
        n = Node(*args, **kw)
        created.append(n)
        return n

    yield make
    for n in created:
        n.m.stop()


def test_private_and_broadcast(nodes):
    a, b, c = nodes("a" * 32, "Алиса"), nodes("b" * 32, "Боб"), nodes("c" * 32, "Вика")
    for x in (a, b, c):
        for y in (a, b, c):
            if x is not y:
                x.know(y)

    msg = a.m.send("лично Бобу", to=b.m.node_id)
    assert wait_for(lambda: b.inbox and a.delivered)
    assert b.inbox[0].text == "лично Бобу" and b.inbox[0].is_private
    assert b.inbox[0].sender_nick == "Алиса"
    assert a.delivered == [(msg.msg_id, b.m.node_id)]
    assert c.inbox == []

    msg2 = a.m.send("всем привет")
    assert wait_for(lambda: len(a.delivered) == 3)
    assert [m.text for m in b.inbox] == ["лично Бобу", "всем привет"]
    assert [m.text for m in c.inbox] == ["всем привет"]
    assert not c.inbox[0].is_private
    assert {p for mid, p in a.delivered if mid == msg2.msg_id} == {b.m.node_id, c.m.node_id}


def test_unknown_peer_fails(nodes):
    a = nodes("a" * 32, "a")
    msg = a.m.send("кому?", to="z" * 32)
    assert wait_for(lambda: a.failed)
    assert a.failed == [(msg.msg_id, "z" * 32)]


def test_secret_mismatch_not_delivered(nodes, monkeypatch):
    import localnet.messaging as messaging
    monkeypatch.setattr(messaging, "ACK_TIMEOUT", 1.0)
    a, b = nodes("a" * 32, "a", secret="1"), nodes("b" * 32, "b", secret="2")
    a.know(b)
    a.m.send("тайна", to=b.m.node_id)
    assert wait_for(lambda: a.failed, timeout=5)
    assert b.inbox == []


def test_empty_and_too_long_rejected(nodes):
    a = nodes("a" * 32, "a")
    with pytest.raises(ValueError):
        a.m.send("   ")
    with pytest.raises(ValueError):
        a.m.send("x" * 100_000)


def test_encode_decode_roundtrip():
    msg = ChatMessage("i" * 32, "f" * 32, "ник", "текст", to=None)
    obj = decode_frame(encode_chat(msg, "k"), "k")
    assert obj["text"] == "текст" and obj["nick"] == "ник"
    assert decode_frame(encode_chat(msg, "k"), "other") is None
    assert decode_frame(b"\x00garbage", "") is None
