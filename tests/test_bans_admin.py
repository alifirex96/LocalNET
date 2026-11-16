"""Блокировки: подпись администратора, применение в ядре, админ-панель (без сети)."""

import time

import pytest

from localnet import ed25519
from localnet.admin import AdminCore
from localnet.app import LocalNetCore
from localnet.bans import Ban, BanList, load_trusted_key
from localnet.config import Config
from localnet.models import ALL_CHAT, ChatMessage, EventType, Peer

ADMIN_SK = ed25519.generate_secret()
ADMIN_PK = ed25519.public_key(ADMIN_SK).hex()
BOB = "b" * 32


def signed(bans, version=1, sk=ADMIN_SK):
    bl = BanList(version=version, bans=bans)
    bl.sign(sk)
    return bl


def test_banlist_sign_verify_and_tamper():
    bl = signed([Ban(BOB, ident="i" * 24, nick="Боб", reason="спам")])
    assert bl.verify(ADMIN_PK)
    wire = bl.to_wire()
    again = BanList.from_wire(wire)
    assert again is not None and again.verify(ADMIN_PK)
    wire["list"]["bans"] = []          # пользователь пытается снять с себя бан
    assert not BanList.from_wire(wire).verify(ADMIN_PK)
    forged = signed([], version=99, sk=ed25519.generate_secret())  # чужой ключ
    assert not forged.verify(ADMIN_PK)
    assert not bl.verify("")           # ключ администратора не настроен — не доверяем


def test_ban_expiry_and_ident():
    now = time.time()
    bl = signed([Ban(BOB, ident="x" * 24, until=now + 60), Ban("c" * 32, until=now - 1)])
    assert bl.find(BOB) is not None
    assert bl.find("z" * 32, ident="x" * 24) is not None   # новый ID, тот же человек
    assert bl.find("c" * 32) is None                         # срок истёк
    assert bl.find(BOB, now=now + 120) is None
    assert Ban(BOB).describe() == "навсегда"


def test_from_wire_rejects_garbage():
    assert BanList.from_wire({}) is None
    assert BanList.from_wire({"list": {"version": -1, "bans": []}, "pub": "00", "esig": "00"}) is None
    assert BanList.from_wire({"list": {"version": 1, "bans": [{"x": 1}]}, "pub": "00", "esig": "00"}) is None


def test_trusted_key_from_file(tmp_path):
    (tmp_path / "localnet-admin.pub").write_text(ADMIN_PK + "\n")
    assert load_trusted_key("", [tmp_path]) == ADMIN_PK
    assert load_trusted_key("", [tmp_path / "нет"]) == ""
    assert load_trusted_key(ADMIN_PK.upper(), []) == ADMIN_PK


@pytest.fixture
def core(tmp_path):
    cfg = Config(nickname="Я", admin_public_key=ADMIN_PK)
    cfg.path = str(tmp_path / "cfg.json")
    c = LocalNetCore(cfg)
    yield c
    c.history.close()


def _incoming(i, sender=BOB, to=None):
    return ChatMessage(msg_id=f"m{i:031d}", sender_id=sender, sender_nick="Боб", text=f"привет {i}", to=to,
                       ts=1000.0 + i)


def test_core_applies_signed_bans(core):
    core._on_message(_incoming(1))
    assert len(core.history.load_messages(ALL_CHAT)) == 1
    core._on_bans("a" * 32, {"type": "bans", **signed([Ban(BOB, reason="флуд")], version=5).to_wire()})
    assert core.is_banned(BOB)
    assert core.banlist.version == 5
    core._on_message(_incoming(2))               # от заблокированного — не принимаем
    assert len(core.history.load_messages(ALL_CHAT)) == 1
    with pytest.raises(ValueError):
        core.send_message("тебе", to=BOB)
    # старая версия не перезаписывает новую, неподписанная — игнорируется
    core._on_bans("a" * 32, {"type": "bans", **signed([], version=4).to_wire()})
    assert core.is_banned(BOB)
    core._on_bans("a" * 32, {"type": "bans", **signed([], version=9, sk=ed25519.generate_secret()).to_wire()})
    assert core.is_banned(BOB)
    core._on_bans("a" * 32, {"type": "bans", **signed([], version=6).to_wire()})  # разбан
    assert not core.is_banned(BOB)


def test_self_ban_blocks_sending(core):
    core._on_bans("a" * 32, {"type": "bans", **signed([Ban(core.node_id, until=time.time() + 3600,
                                                          reason="отдых")], version=7).to_wire()})
    assert core.my_ban() is not None
    with pytest.raises(ValueError, match="заблокированы"):
        core.send_message("всем")


def test_bans_survive_restart(tmp_path):
    cfg = Config(nickname="Я", admin_public_key=ADMIN_PK)
    cfg.path = str(tmp_path / "cfg.json")
    c = LocalNetCore(cfg)
    c._on_bans("a" * 32, {"type": "bans", **signed([Ban(BOB)], version=3).to_wire()})
    c.history.close()
    c2 = LocalNetCore(cfg)
    assert c2.is_banned(BOB) and c2.banlist.version == 3
    c2.history.close()


def test_delete_for_me_and_remote_delete(core):
    core._on_message(_incoming(1, to=core.node_id))
    core._on_message(_incoming(2, to=core.node_id))
    # собеседник удаляет своё сообщение у всех
    core._on_delete(BOB, [_incoming(1).msg_id])
    assert [m.msg.text for m in core.history.load_messages(BOB)] == ["привет 2"]
    assert core.events.get_nowait().type == EventType.MESSAGE  # первые события — сообщения
    # чужие сообщения удалить у нас нельзя
    core._on_delete("e" * 32, [_incoming(2).msg_id])
    assert len(core.history.load_messages(BOB)) == 1
    core.delete_messages([_incoming(2).msg_id])
    assert core.history.load_messages(BOB) == []


def test_delete_conversation_hides_contact(core):
    core.history.upsert_contact(BOB, "Боб", "10.0.0.2", 1.0)
    core._on_message(_incoming(1, to=core.node_id))
    core.delete_conversation(BOB)
    assert core.history.load_messages(BOB) == []
    assert BOB not in core.history.contacts()
    core._on_message(_incoming(2, to=core.node_id))   # новое сообщение — диалог снова виден
    assert BOB in core.history.contacts()


def test_duplicate_contact_is_merged(core):
    old, new = "1" * 32, "2" * 32
    core.history.upsert_contact(old, "Боб", "10.0.0.2", 1.0, ident="same-person")
    core._on_message(_incoming(1, sender=old, to=core.node_id))
    peer = Peer(new, "Боб", "10.0.0.9", 1, 1, ident="same-person")
    core._on_peer_updated(peer)
    assert old not in core.history.contacts()
    assert [m.msg.text for m in core.history.load_messages(new)] == ["привет 1"]
    kinds = []
    while not core.events.empty():
        kinds.append(core.events.get_nowait())
    assert any(e.type == EventType.PEER_MERGED and e.data == (old, new) for e in kinds)


def test_namesakes_on_different_computers_not_merged(core):
    core.history.upsert_contact("1" * 32, "user@astra", "10.0.0.2", 1.0, ident="pc-one")
    core._on_peer_updated(Peer("2" * 32, "user@astra", "10.0.0.2", 1, 1, ident="pc-two"))
    assert "1" * 32 in core.history.contacts()


def test_admin_core_ban_unban_persist(tmp_path):
    cfg = Config()
    cfg.path = str(tmp_path / "cfg.json")
    admin = AdminCore(cfg, data_dir=tmp_path / "admin")
    admin.seen[BOB] = {"nick": "Боб", "ip": "10.0.0.2", "ident": "id-bob", "last_seen": 1.0}
    admin.ban(BOB, 3600, "спам")
    assert admin.banlist.verify(admin.public)
    ban = admin.ban_of(BOB)
    assert ban.ident == "id-bob" and ban.reason == "спам" and ban.until > time.time()
    v1 = admin.banlist.version
    admin.ban("c" * 32, 0)
    assert admin.banlist.version > v1 and admin.ban_of("c" * 32).until == 0
    admin.messenger.stop()
    again = AdminCore(cfg, data_dir=tmp_path / "admin")   # тот же ключ и список после перезапуска
    assert again.public == admin.public and again.ban_of(BOB) is not None
    assert again.banlist.verify(admin.public)
    again.unban(BOB)
    assert again.ban_of(BOB) is None
    again.banlist.bans.append(Ban("d" * 32, until=time.time() - 5))
    assert again.clear_expired() == 0 and all(b.active() for b in again.banlist.bans)
