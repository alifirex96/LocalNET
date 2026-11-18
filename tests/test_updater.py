import hashlib
import io
import json
import sys
import tarfile

import pytest

from localnet import updater

TAG = "v0.4.2"
ASSET = f"LocalNET-{TAG}-python.tar.gz"


def _archive(extra=None):
    prefix = f"LocalNET-{TAG}-python/localnet"
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        entries = {f"{prefix}/__init__.py": b"__version__ = '0.4.2'\n",
                   f"{prefix}/__main__.py": b"pass\n", **(extra or {})}
        for name, content in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return data.getvalue()


def test_latest_release_uses_matching_asset_with_checksum(monkeypatch):
    digest = "a" * 64
    metadata = {"tag_name": TAG, "assets": [{"name": ASSET, "digest": f"sha256:{digest}"}]}
    monkeypatch.setattr(updater, "_fetch", lambda *_: json.dumps(metadata).encode())
    assert updater.latest_release() == updater.Release(TAG, ASSET, digest)
    metadata["tag_name"] = "v0.3.0"
    assert updater.latest_release() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Python-обновление для Linux")
def test_install_checks_digest_and_rejects_untrusted_paths(tmp_path, monkeypatch):
    package = tmp_path / "localnet"
    package.mkdir()
    (package / "__init__.py").write_text("original", encoding="utf-8")
    config = tmp_path / "localnet.json"
    config.write_text("user settings", encoding="utf-8")
    bad = _archive({f"LocalNET-{TAG}-python/localnet/../../localnet.json": b"overwrite"})
    monkeypatch.setattr(updater, "_fetch", lambda *_: bad)
    with pytest.raises(ValueError, match="Контрольная сумма"):
        updater.install_release(updater.Release(TAG, ASSET, "0" * 64), package)
    with pytest.raises(ValueError, match="Недопустимый путь"):
        updater.install_release(updater.Release(TAG, ASSET, hashlib.sha256(bad).hexdigest()), package)
    assert (package / "__init__.py").read_text(encoding="utf-8") == "original"
    assert config.read_text(encoding="utf-8") == "user settings"

    valid = _archive()
    monkeypatch.setattr(updater, "_fetch", lambda *_: valid)
    updater.install_release(updater.Release(TAG, ASSET, hashlib.sha256(valid).hexdigest()), package)
    assert "0.4.2" in (package / "__init__.py").read_text(encoding="utf-8")
    assert config.read_text(encoding="utf-8") == "user settings"
