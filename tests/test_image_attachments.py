import struct
import threading
import time
import zlib
from pathlib import Path

import pytest

from localnet import attachments, file_transfer as ft
from localnet.app import LocalNetCore
from localnet.attachments import ImageAttachment, ImageStore, verify_image
from localnet.config import Config
from localnet.discovery import Presence
from localnet.history import HistoryStore
from localnet.models import TransferState, new_id
from localnet.utils import sha256_file


def _chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _png(path, width=32, height=24, color=(220, 90, 125, 127), big=False):
    channels = len(color)
    header = struct.pack(">IIBBBBB", width, height, 8, 6 if channels == 4 else 2, 0, 0, 0)
    compressor = zlib.compressobj(0 if big else 6)
    row = b"\0" + bytes(color) * width
    with path.open("wb") as out:
        out.write(b"\x89PNG\r\n\x1a\n")
        out.write(_chunk(b"IHDR", header))
        for _ in range(height):
            block = compressor.compress(row)
            if block:
                out.write(_chunk(b"IDAT", block))
        out.write(_chunk(b"IDAT", compressor.flush()))
        out.write(_chunk(b"IEND", b""))
    return path


@pytest.fixture
def peers(tmp_path):
    def create(name):
        config = Config(nickname=name, path=str(tmp_path / f"{name}.json"), transport="python",
                        auto_accept=True, discovery_port=0, msg_port=0, file_port=0,
                        broadcast_addresses=[], multicast_group="", secret="office")
        return LocalNetCore(config)

    sender, receiver = create("sender"), create("receiver")
    sender.start()
    receiver.start()
    try:
        for first, second in ((sender, receiver), (receiver, sender)):
            presence = Presence("hello", second.node_id, second.get_nickname(), second.messenger.port,
                                second.files.port, "0.4.1", time.time())
            first.registry.update(presence, "127.0.0.1")
        yield sender, receiver
    finally:
        sender.stop()
        receiver.stop()


def _wait(sender, receiver, sent, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        incoming = {t.transfer_id: t for t in receiver.files.all()}
        received = [incoming.get(t.transfer_id) for t in sent]
        if all(t.state.finished for t in sent) and all(t and t.state.finished for t in received):
            return received
        time.sleep(0.02)
    raise AssertionError(f"Передача не завершилась: {[(t.state, t.error) for t in sent]}")


def test_send_10_images_and_restore_album_and_deduplicate(tmp_path, peers):
    sender, receiver = peers
    files = [_png(tmp_path / f"Image {i}.png", color=(i, 60, 200, 127)) for i in range(10)]
    sent = sender.send_images(receiver.node_id, [str(p) for p in files], "file", "Таблица и фото")
    received = _wait(sender, receiver, sent)
    assert all(t.state == TransferState.DONE for t in sent + received)
    assert len({t.image.album for t in received}) == 1
    for source, t in zip(files, received):
        assert t.name == source.name and Path(t.path).read_bytes() == source.read_bytes()
        assert t.image.caption == "Таблица и фото"
    original = received[0]
    duplicate_source = tmp_path / "Same bytes.png"
    duplicate_source.write_bytes(files[0].read_bytes())
    repeated = sender.send_images(receiver.node_id, [str(duplicate_source)], "file", "")
    duplicate, = _wait(sender, receiver, repeated)
    assert duplicate.state == TransferState.DONE and duplicate.offset == duplicate.size
    assert duplicate.path == original.path and duplicate.name == "Same bytes.png"
    assert len(list((receiver.images.root / "originals").glob("*/*/*"))) == 10
    reopened = HistoryStore(receiver.history.path)
    try:
        history = reopened.load_transfers(sender.node_id)
        assert len(history) == 11
        gallery = [t for t in history if t.image.album == original.image.album]
        assert {t.image.index for t in gallery} == set(range(10))
        assert all(Path(t.path).is_file() for t in gallery)
    finally:
        reopened.close()


def test_large_original_resumes_and_success_requires_receiver(tmp_path, peers, monkeypatch):
    sender, receiver = peers
    path = _png(tmp_path / "50 MB.png", width=4096, height=4270, color=(20, 40, 60), big=True)
    assert path.stat().st_size >= 50 * 1024 * 1024
    original_receive = ft.receive_file_body
    broken = threading.Event()

    def interrupt(sock, output, hasher, offset, size, progress, cancel):
        if not broken.is_set():
            data = ft.recv_exact(sock, 256 * 1024)
            output.write(data)
            hasher.update(data)
            broken.set()
            raise ConnectionError("получатель отключился")
        return original_receive(sock, output, hasher, offset, size, progress, cancel)

    monkeypatch.setattr(ft, "receive_file_body", interrupt)
    failed, = sender.send_images(receiver.node_id, [str(path)], "file", "")
    failed_receiver, = _wait(sender, receiver, [failed], timeout=35)
    assert failed.state == failed_receiver.state == TransferState.FAILED
    assert not failed_receiver.path
    sent = sender.send_file(receiver.node_id, failed.path, image=failed.image, prepared=True, name=failed.name)
    received, = _wait(sender, receiver, [sent], timeout=35)
    assert sent.state == received.state == TransferState.DONE
    assert sent.offset == received.offset == 256 * 1024
    assert received.name == path.name and sha256_file(received.path) == sha256_file(path)
    assert not list((receiver.images.root / "partial").glob("*.part"))


def test_corrupt_image_is_rejected_even_with_matching_sha256(tmp_path, peers):
    sender, receiver = peers
    corrupt = tmp_path / "broken.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\n" + b"bad")
    meta = ImageAttachment(new_id(), 0, 1, "file")
    transfer = sender.send_file(receiver.node_id, str(corrupt), image=meta, prepared=True)
    incoming, = _wait(sender, receiver, [transfer])
    assert transfer.state == incoming.state == TransferState.FAILED
    assert not incoming.path
    assert not list((receiver.images.root / "originals").glob("*/*/*"))


def test_photo_mode_preserves_alpha_proportions_and_source(tmp_path):
    image = pytest.importorskip("PIL.Image")
    path = _png(tmp_path / "transparent.png", width=2600, height=1300)
    original = sha256_file(path)
    payload, name, digest = ImageStore(tmp_path / "storage").prepare(path, path.name, "photo", threading.Event())
    assert sha256_file(path) == original and digest != original
    assert name == path.name and verify_image(payload, name) == "PNG"
    with image.open(payload) as photo:
        assert photo.size == (2048, 1024)
        assert photo.getpixel((0, 0))[3] == 127


def test_original_file_can_exceed_the_preview_pixel_budget(tmp_path, monkeypatch):
    pytest.importorskip("PIL.Image")
    monkeypatch.setattr(attachments, "MAX_DECODE_PIXELS", 100)
    path = _png(tmp_path / "original.png", width=32, height=24)
    store = ImageStore(tmp_path / "store")
    payload, name, _ = store.prepare(path, path.name, "file", threading.Event())
    assert name == path.name and sha256_file(payload) == sha256_file(path)
    with pytest.raises(ValueError, match="слишком велико"):
        store.prepare(path, path.name, "photo", threading.Event())


@pytest.mark.parametrize("suffix,container", [
    (".jpg", "JPEG"), (".jpeg", "JPEG"), (".png", "PNG"), (".webp", "WEBP"), (".gif", "GIF"), (".bmp", "BMP"),
])
def test_supported_image_formats(tmp_path, suffix, container):
    image = pytest.importorskip("PIL.Image")
    path = tmp_path / ("Sample" + suffix)
    image.new("RGB", (12, 30), color="purple").save(path, container)
    assert verify_image(path, path.name) == container


def test_rejects_untrusted_album_metadata():
    good = ImageAttachment(new_id(), 0, 1, "file").wire()
    for override in ({"album": "../../path"}, {"count": 1000}, {"index": False}, {"mode": "binary"},
                     {"caption": "x" * 2049}):
        with pytest.raises(ValueError):
            ImageAttachment.from_wire({**good, **override})
