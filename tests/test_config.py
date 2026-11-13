"""Тесты загрузки/сохранения конфигурации."""

import json

from localnet.config import DEFAULT_MSG_PORT, Config, load_config


def test_creates_default(tmp_path):
    path = tmp_path / "cfg.json"
    cfg = load_config(str(path))
    assert path.exists()
    assert cfg.msg_port == DEFAULT_MSG_PORT
    assert json.loads(path.read_text(encoding="utf-8"))["nickname"] == cfg.nickname


def test_roundtrip_unicode(tmp_path):
    path = tmp_path / "cfg.json"
    cfg = Config(nickname="Вася", msg_port=50001, secret="ключ", static_peers=["10.0.0.9"])
    cfg.save(path)
    loaded = load_config(str(path))
    assert (loaded.nickname, loaded.msg_port, loaded.secret, loaded.static_peers) == \
        ("Вася", 50001, "ключ", ["10.0.0.9"])


def test_invalid_values_fixed(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"msg_port": 80, "nickname": "  ", "announce_interval": 0,
                                "static_peers": "10.0.0.1", "unknown": 1}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.msg_port == DEFAULT_MSG_PORT  # привилегированный порт не используем
    assert cfg.nickname
    assert cfg.announce_interval >= 0.2
    assert cfg.peer_timeout >= cfg.announce_interval * 3
    assert cfg.static_peers == ["10.0.0.1"]


def test_corrupted_file_not_overwritten(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text("{ broken", encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.msg_port == DEFAULT_MSG_PORT
    assert path.read_text(encoding="utf-8") == "{ broken"


def test_utf8_bom_accepted(tmp_path):
    # Блокнот Windows сохраняет UTF-8 с BOM.
    path = tmp_path / "cfg.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"nickname": "Петя"}, ensure_ascii=False).encode("utf-8"))
    assert load_config(str(path)).nickname == "Петя"
