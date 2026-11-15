"""Тесты хранилища истории переписки."""

import time

import localnet.history as history_mod
from localnet.history import HistoryStore, history_path_for
from localnet.models import ALL_CHAT, ChatMessage, Transfer, TransferDirection, TransferState

ME, BOB = "m" * 32, "b" * 32


def _msg(i, *, to=None, outgoing=False, sender=BOB, text=None):
    return ChatMessage(msg_id=f"id{i:030d}", sender_id=ME if outgoing else sender,
                       sender_nick="Я" if outgoing else "Боб", text=text or f"сообщение {i}", to=to,
                       ts=1000.0 + i, outgoing=outgoing)


def test_path_next_to_config(tmp_path):
    assert history_path_for(str(tmp_path / "config.json")) == tmp_path / "config.history.db"
    assert history_path_for(None) is None


def test_node_id_persistent(tmp_path):
    p = tmp_path / "h.db"
    a = HistoryStore(p)
    nid = a.node_id()
    a.close()
    b = HistoryStore(p)
    assert b.node_id() == nid
    b.close()


def test_node_id_regenerated_on_other_machine(tmp_path, monkeypatch):
    p = tmp_path / "h.db"
    a = HistoryStore(p)
    nid = a.node_id()
    a.close()
    monkeypatch.setattr(history_mod, "_owner", lambda: "other@pc")
    b = HistoryStore(p)
    assert b.node_id() != nid


def test_messages_roundtrip_with_receipts_and_reactions(tmp_path):
    p = tmp_path / "h.db"
    h = HistoryStore(p)
    out = _msg(1, outgoing=True, to=BOB, text="Привет, Боб! 👋")
    inc = _msg(2, to=ME)
    pub = _msg(3)
    for m in (out, inc, pub):
        h.add_message(m, targets=1)
    h.add_message(out)  # повтор не дублирует
    h.add_receipt(out.msg_id, BOB, "delivered")
    h.add_receipt(out.msg_id, BOB, "read")
    h.set_reaction(out.msg_id, BOB, "like")
    h.set_reaction(out.msg_id, ME, "ok")
    h.set_reaction(out.msg_id, ME, "")  # снята
    h.close()

    h = HistoryStore(p)
    private = h.load_messages(BOB)
    assert [s.msg.text for s in private] == ["Привет, Боб! 👋", "сообщение 2"]
    assert private[0].msg.outgoing and private[0].delivered == {BOB} and private[0].read == {BOB}
    assert private[0].reactions == {BOB: "like"}
    assert [s.msg.msg_id for s in h.load_messages(ALL_CHAT)] == [pub.msg_id]
    assert h.message_conv(inc.msg_id) == BOB


def test_unread_and_mark_read(tmp_path):
    h = HistoryStore(tmp_path / "h.db")
    for i in range(3):
        h.add_message(_msg(i, to=ME))
    h.add_message(_msg(10, outgoing=True, to=BOB))
    h.add_message(_msg(11))
    assert h.unread_counts() == {BOB: 3, ALL_CHAT: 1}
    unread = h.unread(BOB)
    assert len(unread) == 3 and all(s == BOB for _, s in unread)
    h.mark_read_sent([m for m, _ in unread])
    assert h.unread(BOB) == []
    assert h.unread_counts() == {ALL_CHAT: 1}


def test_load_limit_returns_latest(tmp_path):
    h = HistoryStore(tmp_path / "h.db")
    for i in range(20):
        h.add_message(_msg(i))
    got = h.load_messages(ALL_CHAT, limit=5)
    assert [s.msg.text for s in got] == [f"сообщение {i}" for i in range(15, 20)]


def _transfer(tid="t" * 32, state=TransferState.DONE, started=2000.0):
    return Transfer(transfer_id=tid, direction=TransferDirection.RECEIVE, peer_id=BOB, peer_nick="Боб",
                    peer_ip="10.0.0.2", name="отчёт.pdf", size=1234, path="/tmp/отчёт.pdf", sha256="0" * 64,
                    state=state, started=started)


def test_transfers_roundtrip_and_interrupted(tmp_path):
    p = tmp_path / "h.db"
    h = HistoryStore(p)
    h.upsert_transfer(_transfer())
    h.upsert_transfer(_transfer("u" * 32, TransferState.TRANSFERRING, 2001.0))
    h.close()
    h = HistoryStore(p)
    ts = h.load_transfers(BOB)
    assert [t.state for t in ts] == [TransferState.DONE, TransferState.FAILED]
    assert ts[0].done == 1234 and ts[0].name == "отчёт.pdf"
    assert "прервано" in ts[1].error


def test_conversations_preview(tmp_path):
    h = HistoryStore(tmp_path / "h.db")
    h.add_message(_msg(1, outgoing=True, to=BOB, text="как дела?"))
    h.add_message(_msg(2, text="всем привет"))
    h.upsert_transfer(_transfer(started=5000.0))
    conv = h.conversations()
    assert conv[ALL_CHAT] == (1002.0, "Боб: всем привет")
    assert conv[BOB] == (5000.0, "↓ отчёт.pdf")


def test_contacts(tmp_path):
    h = HistoryStore(tmp_path / "h.db")
    h.upsert_contact(BOB, "Боб", "10.0.0.2", 100.0)
    h.upsert_contact(BOB, "Борис", "10.0.0.3", 200.0)
    assert h.contacts() == {BOB: ("Борис", "10.0.0.3", 200.0)}


def test_disabled_history_keeps_node_id_but_not_messages(tmp_path):
    p = tmp_path / "h.db"
    h = HistoryStore(p, enabled=False)
    nid = h.node_id()
    h.add_message(_msg(1))
    assert h.load_messages(ALL_CHAT) == []
    h.close()
    assert HistoryStore(p, enabled=False).node_id() == nid


def test_corrupted_file_does_not_crash(tmp_path):
    p = tmp_path / "h.db"
    p.write_bytes(b"this is not a sqlite database" * 100)
    h = HistoryStore(p)
    h.add_message(_msg(1))
    assert isinstance(h.node_id(), str)


def test_no_path_in_memory():
    h = HistoryStore(None)
    h.add_message(_msg(1))
    assert len(h.node_id()) == 32
    assert time.time() > 0
