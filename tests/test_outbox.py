"""Доставка личных сообщений с сохранением очереди между запусками."""

import queue
import time

from localnet import app
from localnet.app import LocalNetCore
from localnet.bans import Ban, BanList
from localnet.config import Config
from localnet.discovery import Presence
from localnet.models import EventType


def _config(tmp_path, name, save_history=True):
    return Config(nickname=name, path=str(tmp_path / f"{name}.json"), transport="python",
                  save_history=save_history, discovery_port=0, msg_port=0, file_port=0,
                  broadcast_addresses=[], multicast_group="")


def _wait_for(core, event_type, timeout=5):
    until = time.monotonic() + timeout
    while True:
        try:
            event = core.events.get(timeout=max(0, until - time.monotonic()))
        except queue.Empty as exc:
            raise AssertionError(f"Не дождались {event_type}") from exc
        if event.type == event_type:
            return event.data
        assert time.monotonic() < until, f"Не дождались {event_type}"


def test_outbox_delivers_after_sender_restarts_and_receiver_appears(tmp_path, monkeypatch):
    alice_config = _config(tmp_path, "Алиса")
    bob_config = _config(tmp_path, "Боб")
    alice = LocalNetCore(alice_config)
    bob = LocalNetCore(bob_config)
    alice.start()
    try:
        queued = alice.send_message("Боб, ты в сети?", bob.node_id)
        assert queued.queued
        assert alice.history.pending_messages(alice.node_id) == [queued]
    finally:
        alice.stop()

    alice = LocalNetCore(alice_config)
    monkeypatch.setattr(app, "OUTBOX_RETRY_INTERVAL", 0.1)
    send = alice.messenger._transport_send
    attempts = []

    def fail_first_attempt(peer, payload):
        attempts.append(peer.node_id)
        return send(peer, payload) if len(attempts) > 1 else False

    monkeypatch.setattr(alice.messenger, "_transport_send", fail_first_attempt)
    alice.start()
    bob.start()
    try:
        assert alice.node_id == queued.sender_id
        peer, _, _ = alice.registry.update(
            Presence("hello", bob.node_id, "Боб", bob.messenger.port, bob.files.port, "", time.time()),
            "127.0.0.1")
        alice._on_peer_updated(peer)
        received = _wait_for(bob, EventType.MESSAGE)
        assert received.msg_id == queued.msg_id
        assert received.text == queued.text
        assert _wait_for(alice, EventType.MESSAGE_DELIVERED) == (queued.msg_id, bob.node_id)
        assert len(attempts) >= 2
        assert alice.history.pending_messages(alice.node_id) == []
    finally:
        alice.stop()
        bob.stop()

    with_reopened_history = LocalNetCore(bob_config)
    try:
        with_reopened_history._on_message(received)
        assert with_reopened_history.events.empty()
        assert len(with_reopened_history.history.load_messages(queued.sender_id)) == 1
    finally:
        with_reopened_history.history.close()


def test_outbox_does_not_write_messages_when_history_disabled(tmp_path):
    config = _config(tmp_path, "Алиса", save_history=False)
    core = LocalNetCore(config)
    try:
        queued = core.send_message("Сообщение на время сеанса", "a" * 32)
        assert queued.queued
        assert core.history.pending_messages(core.node_id) == []
        assert core.history.load_messages(queued.to) == []
    finally:
        core.history.close()


def test_outbox_is_retargeted_when_contacts_merge_and_cleared_on_delete(tmp_path):
    core = LocalNetCore(_config(tmp_path, "Алиса"))
    old_id, new_id = "a" * 32, "b" * 32
    try:
        core.history.upsert_contact(old_id, "Боб", "127.0.0.1", time.time(), "bob-ident")
        queued = core.send_message("После переустановки", old_id)
        peer, _, _ = core.registry.update(
            Presence("hello", new_id, "Боб", 45893, 45894, "0.4.0", time.time(), ident="bob-ident"),
            "127.0.0.1")
        core._on_peer_updated(peer)
        pending, = core.history.pending_messages(core.node_id)
        assert pending.msg_id == queued.msg_id and pending.to == new_id
        assert core._outbox[queued.msg_id][0].to == new_id

        removed = core.send_message("Отменить отправку", new_id)
        assert core.delete_messages([removed.msg_id]) == [removed.msg_id]
        assert removed.msg_id not in core._outbox
        core.delete_conversation(new_id)
        assert not core._outbox and core.history.pending_messages(core.node_id) == []
    finally:
        core.history.close()


def test_outbox_respects_a_ban_received_after_queuing(tmp_path, monkeypatch):
    core = LocalNetCore(_config(tmp_path, "Алиса"))
    peer_id = "b" * 32
    try:
        queued = core.send_message("Ожидает отправки", peer_id)
        core.registry.update(Presence("hello", peer_id, "Боб", 45893, 45894, "0.4.0", time.time()),
                             "127.0.0.1")
        core.banlist = BanList(version=1, bans=[Ban(node_id=peer_id)])
        attempts = []
        monkeypatch.setattr(core.messenger, "submit_message", attempts.append)
        monkeypatch.setattr(core._outbox_stop, "wait", lambda _timeout: core._outbox_stop.set())
        core._run_outbox()
        assert not attempts
        assert core.history.pending_messages(core.node_id) == [queued]
    finally:
        core.history.close()
