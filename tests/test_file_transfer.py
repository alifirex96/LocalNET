"""Тесты хеширования, chunked/sendfile-передачи и полного протокола передачи файлов."""

import hashlib
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from localnet import file_transfer as ft
from localnet.file_transfer import (
    MAGIC,
    FileTransferManager,
    ProtocolError,
    receive_file_body,
    recv_exact,
    recv_frame,
    send_file_body,
    send_frame,
    validate_offer,
)
from localnet.models import Peer, TransferState
from localnet.utils import Cancelled, safe_filename, sha256_file, sign, unique_path


def _make_file(path: Path, size: int, seed: int = 1) -> Path:
    """Псевдослучайный файл без чтения в память целиком."""
    block = hashlib.sha256(str(seed).encode()).digest() * 4096  # 128 КБ
    with open(path, "wb") as f:
        left = size
        while left:
            n = min(left, len(block))
            f.write(block[:n])
            left -= n
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ хеш


@pytest.mark.parametrize("size", [0, 1, 1024 * 1024 - 1, 1024 * 1024, 3 * 1024 * 1024 + 17])
def test_sha256_file_matches_hashlib(tmp_path, size):
    f = _make_file(tmp_path / "f.bin", size)
    assert sha256_file(f) == _sha(f)


def test_sha256_file_progress_and_small_chunks(tmp_path):
    f = _make_file(tmp_path / "f.bin", 10_000)
    calls = []
    digest = sha256_file(f, progress=lambda d, t: calls.append((d, t)), chunk_size=4096)
    assert digest == _sha(f)
    assert calls == [(4096, 10_000), (8192, 10_000), (10_000, 10_000)]


def test_sha256_file_limit(tmp_path):
    f = _make_file(tmp_path / "f.bin", 5000)
    assert sha256_file(f, limit=1234) == hashlib.sha256(f.read_bytes()[:1234]).hexdigest()


def test_sha256_file_cancel(tmp_path):
    f = _make_file(tmp_path / "f.bin", 10_000)
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        sha256_file(f, cancel=ev)


# ------------------------------------------------------------------ имена файлов


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("отчёт.docx", "отчёт.docx"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\Windows\\win.ini", "win.ini"),
        ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
        ("CON.txt", "_CON.txt"),
        ("", "file"),
        ("...", "file"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_long_name_keeps_extension():
    name = safe_filename("я" * 300 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode("utf-8")) <= 200


def test_unique_path(tmp_path):
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "a (1).txt").write_text("2")
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"


# ------------------------------------------------------------------ кадры


def test_frames_roundtrip():
    a, b = socket.socketpair()
    with a, b:
        send_frame(a, {"type": "accept", "offset": 5, "текст": "привет"})
        assert recv_frame(b) == {"type": "accept", "offset": 5, "текст": "привет"}


def test_recv_exact_detects_close():
    a, b = socket.socketpair()
    with b:
        a.sendall(b"abc")
        a.close()
        with pytest.raises(ConnectionError):
            recv_exact(b, 10)


def test_oversized_frame_rejected():
    a, b = socket.socketpair()
    with a, b:
        a.sendall((ft.MAX_FRAME + 1).to_bytes(4, "big"))
        with pytest.raises(ProtocolError):
            recv_frame(b)


def _offer(**kw):
    obj = {"proto": 1, "type": "offer", "id": "x" * 32, "from": "y" * 32, "nick": "n",
           "name": "f.bin", "size": 10, "sha256": "0" * 64}
    obj.update(kw)
    return obj


def test_validate_offer():
    validate_offer(_offer(), "")
    for bad in (_offer(size=-1), _offer(size=True), _offer(sha256="xyz"), _offer(name=""), _offer(type="hi")):
        with pytest.raises(ProtocolError):
            validate_offer(bad, "")
    signed = _offer()
    signed["sig"] = sign(signed, "k")
    validate_offer(signed, "k")
    with pytest.raises(ProtocolError):
        validate_offer(signed, "other")


# ------------------------------------------------------------------ тело файла


@pytest.mark.parametrize("use_sendfile", [False, pytest.param(True, marks=pytest.mark.skipif(
    not hasattr(os, "sendfile"), reason="нет os.sendfile"))])
@pytest.mark.parametrize("offset", [0, 777_777])
def test_body_transfer_chunked_and_sendfile(tmp_path, use_sendfile, offset):
    size = 5 * 1024 * 1024 + 3
    src = _make_file(tmp_path / "src.bin", size)
    dst = tmp_path / "dst.bin"
    expected = src.read_bytes()[offset:]
    a, b = socket.socketpair()
    result = {}

    def receiver():
        h = hashlib.sha256()
        with open(dst, "wb") as f:
            result["n"] = receive_file_body(b, f, h, offset, size, lambda _p: None, threading.Event(),
                                            chunk_size=64 * 1024)
        result["sha"] = h.hexdigest()

    th = threading.Thread(target=receiver)
    th.start()
    progress = []
    with open(src, "rb") as f:
        sent = send_file_body(a, f, offset, size, progress.append, threading.Event(), use_sendfile=use_sendfile)
    th.join(10)
    a.close()
    b.close()
    assert sent == size - offset == result["n"]
    assert dst.read_bytes() == expected
    assert result["sha"] == hashlib.sha256(expected).hexdigest()
    assert progress[-1] == size
    assert progress == sorted(progress)


def test_receive_body_detects_truncation(tmp_path):
    a, b = socket.socketpair()
    a.sendall(b"x" * 100)
    a.close()
    with open(tmp_path / "out", "wb") as f, pytest.raises(ConnectionError):
        receive_file_body(b, f, hashlib.sha256(), 0, 1000, lambda _p: None, threading.Event())
    b.close()


def test_send_body_respects_cancel(tmp_path):
    src = _make_file(tmp_path / "src.bin", 1024)
    a, b = socket.socketpair()
    ev = threading.Event()
    ev.set()
    with a, b, open(src, "rb") as f, pytest.raises(Cancelled):
        send_file_body(a, f, 0, 1024, lambda _p: None, ev, use_sendfile=False)


# ------------------------------------------------------------------ полный протокол


class Pair:
    """Отправитель и получатель на loopback."""

    def __init__(self, tmp_path, *, accept=True, sender_secret="", receiver_secret="", on_update=None,
                 offer_hook=None):
        self.dl = tmp_path / "downloads"
        self.updates = []
        self.done = {}

        def record(t):
            self.updates.append(t)
            if t.state.finished:
                self.done[(t.direction.value, t.transfer_id)] = t
            if on_update:
                on_update(t, self)

        def on_offer(offer):
            if offer_hook:
                return offer_hook(offer, self)
            return accept

        self.receiver = FileTransferManager("r" * 32, lambda: "получатель", 0, lambda: self.dl,
                                            secret=receiver_secret, on_update=record, on_offer=on_offer)
        self.sender = FileTransferManager("s" * 32, lambda: "отправитель", 0, lambda: tmp_path / "unused",
                                          secret=sender_secret, on_update=record)
        self.receiver.start()
        self.sender.start()
        self.peer = Peer("r" * 32, "получатель", "127.0.0.1", 1, self.receiver.port)

    def wait(self, transfer, timeout=20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            send = self.done.get(("send", transfer.transfer_id))
            pending_recv = [t for t in self.receiver.all() if not t.state.finished]
            recv = [t for (d, _), t in self.done.items() if d == "receive"]
            if send is not None and not pending_recv and len(recv) == len(self.receiver.all()):
                return send, (recv[-1] if recv else None)
            time.sleep(0.02)
        raise AssertionError("передача не завершилась вовремя")

    def close(self):
        self.sender.stop()
        self.receiver.stop()


@pytest.fixture
def pair_factory(tmp_path):
    pairs = []

    def make(**kw):
        p = Pair(tmp_path, **kw)
        pairs.append(p)
        return p

    yield make
    for p in pairs:
        p.close()


def test_full_transfer_success(tmp_path, pair_factory):
    src = _make_file(tmp_path / "Отчёт 2026.bin", 7 * 1024 * 1024 + 11)
    pair = pair_factory()
    t = pair.sender.send_file(pair.peer, src)
    send, recv = pair.wait(t)
    assert send.state == TransferState.DONE, send.error
    assert recv.state == TransferState.DONE, recv.error
    out = Path(recv.path)
    assert out == pair.dl / "Отчёт 2026.bin"
    assert _sha(out) == _sha(src) == send.sha256 == recv.sha256
    assert not list(pair.dl.glob("*.part"))
    states = [u.state for u in pair.updates if u.direction.value == "send"]
    assert TransferState.HASHING in states and TransferState.TRANSFERRING in states


def test_empty_file(tmp_path, pair_factory):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    pair = pair_factory()
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert send.state == recv.state == TransferState.DONE
    assert Path(recv.path).read_bytes() == b""


def test_name_collision_gets_suffix(tmp_path, pair_factory):
    src = _make_file(tmp_path / "a.bin", 1000)
    pair = pair_factory()
    _, r1 = pair.wait(pair.sender.send_file(pair.peer, src))
    _, r2 = pair.wait(pair.sender.send_file(pair.peer, src))
    assert Path(r1.path).name == "a.bin"
    assert Path(r2.path).name == "a (1).bin"


def test_declined(tmp_path, pair_factory):
    src = _make_file(tmp_path / "a.bin", 1000)
    pair = pair_factory(accept=False)
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert send.state == TransferState.DECLINED
    assert recv.state == TransferState.DECLINED
    assert not pair.dl.exists() or not any(pair.dl.iterdir())


def test_secret_mismatch_fails(tmp_path, pair_factory):
    src = _make_file(tmp_path / "a.bin", 1000)
    pair = pair_factory(sender_secret="один", receiver_secret="два")
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert send.state == TransferState.FAILED
    assert recv is None  # получатель даже не создал передачу


def test_cancel_by_sender_midway(tmp_path, pair_factory, monkeypatch):
    monkeypatch.setattr(ft, "SENDFILE_CHUNK", 256 * 1024)
    monkeypatch.setattr(ft, "IO_CHUNK", 256 * 1024)
    src = _make_file(tmp_path / "big.bin", 64 * 1024 * 1024)

    def on_update(t, pair):
        if t.direction.value == "send" and t.state == TransferState.TRANSFERRING and t.done > 0:
            pair.sender.cancel(t.transfer_id)

    pair = pair_factory(on_update=on_update)
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert send.state == TransferState.CANCELLED
    assert recv is not None and recv.state == TransferState.FAILED  # для получателя — обрыв
    assert not (pair.dl / "big.bin").exists()


def test_cancel_by_receiver_while_waiting_accept(tmp_path, pair_factory):
    src = _make_file(tmp_path / "a.bin", 1000)

    def hook(offer, pair):
        pair.receiver.cancel(offer.transfer.transfer_id)
        return True

    pair = pair_factory(offer_hook=hook)
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert recv.state == TransferState.CANCELLED
    assert send.state in (TransferState.FAILED, TransferState.DECLINED)


def test_resume_from_partial(tmp_path, pair_factory):
    size = 3 * 1024 * 1024 + 5
    src = _make_file(tmp_path / "r.bin", size)
    sha = _sha(src)
    pair = pair_factory()
    pair.dl.mkdir(parents=True)
    half = size // 2
    (pair.dl / f".r.bin.{sha[:16]}.part").write_bytes(src.read_bytes()[:half])
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert send.state == recv.state == TransferState.DONE
    assert send.offset == recv.offset == half
    assert _sha(Path(recv.path)) == sha


def test_corrupted_partial_detected(tmp_path, pair_factory):
    size = 1024 * 1024
    src = _make_file(tmp_path / "c.bin", size)
    sha = _sha(src)
    pair = pair_factory()
    pair.dl.mkdir(parents=True)
    part = pair.dl / f".c.bin.{sha[:16]}.part"
    part.write_bytes(b"\x00" * 1000)  # «испорченная» докачка
    send, recv = pair.wait(pair.sender.send_file(pair.peer, src))
    assert recv.state == TransferState.FAILED and "сумма" in recv.error
    assert send.state == TransferState.FAILED
    assert not part.exists()
    assert not (pair.dl / "c.bin").exists()


def test_connection_refused(tmp_path):
    src = _make_file(tmp_path / "a.bin", 10)
    done = threading.Event()
    result = {}

    def upd(t):
        if t.state.finished:
            result["t"] = t
            done.set()

    mgr = FileTransferManager("s" * 32, lambda: "x", 0, lambda: tmp_path, on_update=upd)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # порт свободен — подключение будет отклонено
    mgr.send_file(Peer("p" * 32, "p", "127.0.0.1", 1, port), src)
    assert done.wait(10)
    assert result["t"].state == TransferState.FAILED


def test_bad_magic_is_ignored(tmp_path, pair_factory):
    pair = pair_factory()
    with socket.create_connection(("127.0.0.1", pair.receiver.port)) as s:
        s.sendall(b"GET / HTTP/1.1\r\n\r\n")
        s.settimeout(5)
        try:
            assert s.recv(10) == b""  # сервер закрыл соединение
        except ConnectionResetError:
            pass  # или сбросил его — тоже корректно
    assert pair.receiver.all() == []
    assert MAGIC == b"LNF1"
