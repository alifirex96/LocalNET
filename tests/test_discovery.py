"""Тесты разбора пакетов discovery и таблицы пиров."""

import json
import time

import pytest

from localnet import PROTOCOL_VERSION
from localnet.discovery import (
    FORGET_AFTER,
    MAX_PACKET,
    Discovery,
    PeerRegistry,
    Presence,
    build_packet,
    parse_packet,
)
from localnet.utils import canonical_json, sign

NODE = "a" * 32


def _raw(**overrides) -> dict:
    obj = json.loads(build_packet(NODE, "Вася", 45893, 45894))
    obj.update(overrides)
    return obj


def _enc(obj: dict) -> bytes:
    return canonical_json(obj)


# ------------------------------------------------------------------ разбор


def test_roundtrip_unicode_nick():
    p = parse_packet(build_packet(NODE, "Вася Пупкин 🚀", 45893, 45894))
    assert p is not None
    assert p.type == "hello"
    assert p.node_id == NODE
    assert p.nickname == "Вася Пупкин 🚀"
    assert (p.msg_port, p.file_port) == (45893, 45894)


def test_bye_packet():
    p = parse_packet(build_packet(NODE, "x", 1025, 1026, ptype="bye"))
    assert p is not None and p.type == "bye"


def test_long_nick_truncated_and_fits_packet():
    data = build_packet(NODE, "я" * 500, 45893, 45894)
    assert len(data) <= MAX_PACKET
    p = parse_packet(data)
    assert p is not None and len(p.nickname) == 64


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xfe\x00garbage",
        b"not json",
        b"[1,2,3]",
        b"x" * (MAX_PACKET + 1),
    ],
)
def test_garbage_rejected(data):
    assert parse_packet(data) is None


@pytest.mark.parametrize(
    "override",
    [
        {"magic": "OTHER"},
        {"proto": PROTOCOL_VERSION + 1},
        {"type": "evil"},
        {"node_id": "short"},
        {"node_id": 123},
        {"nick": ""},
        {"nick": "   "},
        {"msg_port": 0},
        {"msg_port": 70000},
        {"msg_port": "45893"},
        {"file_port": True},
        {"ts": "now"},
    ],
)
def test_invalid_fields_rejected(override):
    assert parse_packet(_enc(_raw(**override))) is None


def test_missing_field_rejected():
    obj = _raw()
    del obj["file_port"]
    assert parse_packet(_enc(obj)) is None


# ------------------------------------------------------------------ секрет


def test_secret_valid_signature():
    data = build_packet(NODE, "x", 45893, 45894, secret="s3cr3t")
    assert parse_packet(data, secret="s3cr3t") is not None


def test_secret_mismatch_rejected():
    data = build_packet(NODE, "x", 45893, 45894, secret="one")
    assert parse_packet(data, secret="two") is None


def test_unsigned_rejected_when_secret_set():
    assert parse_packet(build_packet(NODE, "x", 45893, 45894), secret="s") is None


def test_tampered_packet_rejected():
    obj = json.loads(build_packet(NODE, "x", 45893, 45894, secret="s"))
    obj["msg_port"] = 50000
    assert parse_packet(_enc(obj), secret="s") is None


def test_signed_packet_accepted_without_local_secret():
    # Узел без секрета принимает и подписанные пакеты (подпись просто игнорируется).
    assert parse_packet(build_packet(NODE, "x", 45893, 45894, secret="s")) is not None


def test_replay_old_packet_rejected_with_secret():
    old = build_packet(NODE, "x", 45893, 45894, secret="s", ts=time.time() - 3600)
    assert parse_packet(old, secret="s") is None


def test_sign_ignores_sig_field():
    obj = {"a": 1}
    assert sign(obj, "k") == sign({**obj, "sig": "zzz"}, "k")


# ------------------------------------------------------------------ реестр


def _presence(node_id=NODE, nick="Вася", msg_port=45893, file_port=45894):
    return Presence("hello", node_id, nick, msg_port, file_port, "0.1.0", time.time())


def test_registry_add_and_update():
    reg = PeerRegistry()
    peer, changed, removed = reg.update(_presence(), "10.0.0.5", now=100)
    assert changed and not removed
    assert peer.ip == "10.0.0.5" and peer.online
    # Повторный тот же пакет — без изменений, но last_seen обновлён.
    peer, changed, _ = reg.update(_presence(), "10.0.0.5", now=101)
    assert not changed and peer.last_seen == 101
    # Смена порта — изменение.
    _, changed, _ = reg.update(_presence(msg_port=50000), "10.0.0.5", now=102)
    assert changed
    assert reg.get(NODE).msg_port == 50000


def test_registry_expire_and_forget():
    reg = PeerRegistry()
    reg.update(_presence(), "10.0.0.5", now=100)
    offline, forgotten = reg.expire(timeout=6, now=105)
    assert not offline and not forgotten
    offline, forgotten = reg.expire(timeout=6, now=107)
    assert [p.node_id for p in offline] == [NODE]
    assert not reg.get(NODE).online
    assert reg.online() == []
    # Возвращение в сеть — снова online и "изменился".
    _, changed, _ = reg.update(_presence(), "10.0.0.5", now=108)
    assert changed and reg.get(NODE).online
    reg.expire(timeout=6, now=200)
    _, forgotten = reg.expire(timeout=6, now=108 + FORGET_AFTER + 1)
    assert [p.node_id for p in forgotten] == [NODE]
    assert reg.get(NODE) is None


def test_registry_restart_replaces_stale_entry():
    reg = PeerRegistry()
    reg.update(_presence(node_id="1" * 32), "10.0.0.5", now=100)
    # Пока старая запись online — это может быть второй экземпляр на той же машине.
    _, _, removed = reg.update(_presence(node_id="3" * 32), "10.0.0.5", now=101)
    assert removed == []
    reg.mark_offline("1" * 32)
    reg.mark_offline("3" * 32)
    _, _, removed = reg.update(_presence(node_id="2" * 32), "10.0.0.5", now=110)
    assert sorted(p.node_id for p in removed) == ["1" * 32, "3" * 32]
    assert [p.node_id for p in reg.all()] == ["2" * 32]


def test_registry_find():
    reg = PeerRegistry()
    reg.update(_presence(node_id="abc" + "0" * 29, nick="Алиса"), "10.0.0.1")
    reg.update(_presence(node_id="def" + "0" * 29, nick="Боб"), "10.0.0.2")
    assert reg.find("алиса").nickname == "Алиса"
    assert reg.find("10.0.0.2").nickname == "Боб"
    assert reg.find("def").nickname == "Боб"
    assert reg.find("нет такого") is None


# ------------------------------------------------------------------ Discovery


def _discovery(reg, events):
    return Discovery(
        node_id="f" * 32,
        get_nickname=lambda: "me",
        msg_port=1,
        file_port=2,
        registry=reg,
        discovery_port=45892,
        broadcast_addresses=[],
        on_peer_updated=lambda p: events.append(("up", p.node_id)),
        on_peer_offline=lambda p: events.append(("off", p.node_id)),
    )


def test_handle_datagram_ignores_self_and_garbage():
    reg, events = PeerRegistry(), []
    d = _discovery(reg, events)
    d.handle_datagram(build_packet("f" * 32, "me", 1, 2), "10.0.0.1")
    d.handle_datagram(b"junk", "10.0.0.1")
    assert reg.all() == [] and events == []


def test_handle_datagram_hello_and_bye():
    reg, events = PeerRegistry(), []
    d = _discovery(reg, events)
    d.handle_datagram(build_packet(NODE, "Вася", 45893, 45894), "10.0.0.7")
    d.handle_datagram(build_packet(NODE, "Вася", 45893, 45894), "10.0.0.7")
    d.handle_datagram(build_packet(NODE, "Вася", 45893, 45894, ptype="bye"), "10.0.0.7")
    assert events == [("up", NODE), ("off", NODE)]
    assert reg.get(NODE).ip == "10.0.0.7"
    assert not reg.get(NODE).online
