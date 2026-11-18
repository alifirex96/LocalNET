"""Обновление Linux-архива LocalNET из официального GitHub Releases."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

REPOSITORY = "alifirex96/LocalNET"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_ROOT = f"https://github.com/{REPOSITORY}/releases/download"
ALLOWED_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com",
                 "objects.githubusercontent.com"}
MAX_ARCHIVE = 12 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024


@dataclass(frozen=True)
class Release:
    tag: str
    asset: str
    checksum: str


def package_directory() -> Optional[Path]:
    package = Path(__file__).resolve().parent
    if (sys.platform.startswith("linux") and not getattr(sys, "frozen", False)
            and (package.parent / "localnet.png").is_file()):
        return package
    return None


def _trusted_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
            and not parsed.username and not parsed.password and parsed.port in (None, 443))


class _ReleaseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl):
            raise ValueError("GitHub перенаправил на неизвестный сервер")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, limit: int) -> bytes:
    if not _trusted_url(url):
        raise ValueError("Неизвестный адрес GitHub Releases")
    request = Request(url, headers={"User-Agent": f"LocalNET/{__version__}"})
    with build_opener(_ReleaseRedirects()).open(request, timeout=6) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Ответ GitHub превышает допустимый размер")
    return data


def latest_release() -> Optional[Release]:
    data = json.loads(_fetch(RELEASE_API, 512 * 1024))
    tag = data.get("tag_name", "")
    match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
    current = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", __version__)
    if not match or not current:
        return None
    version = tuple(map(int, match.groups()))
    installed = tuple(map(int, current.groups()))
    if version < installed or (version == installed and current.end() == len(__version__)):
        return None
    asset_name = f"LocalNET-{tag}-python.tar.gz"
    assets = {asset["name"]: asset for asset in data.get("assets", [])}
    if asset_name not in assets:
        raise ValueError("В новом релизе нет архива для системного Python")
    digest = assets[asset_name].get("digest") or ""
    if re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        return Release(tag, asset_name, digest[7:])
    if "SHA256SUMS.txt" not in assets:
        raise ValueError("В релизе нет контрольной суммы")
    sums = _fetch(f"{DOWNLOAD_ROOT}/{tag}/SHA256SUMS.txt", 64 * 1024).decode("ascii")
    for line in sums.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].lstrip("*") == asset_name and re.fullmatch("[a-f0-9]{64}", parts[0]):
            return Release(tag, asset_name, parts[0])
    raise ValueError("Не найдена контрольная сумма Python-архива")


def _extract_package(data: bytes, release: Release, destination: Path) -> None:
    expected = PurePosixPath(f"LocalNET-{release.tag}-python/localnet")
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for index, entry in enumerate(archive):
            if index >= 1000:
                raise ValueError("В архиве обновления слишком много файлов")
            path = PurePosixPath(entry.name)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.name:
                raise ValueError("Недопустимый путь в архиве обновления")
            total += entry.size
            if entry.size < 0 or total > MAX_UNPACKED:
                raise ValueError("Архив обновления слишком большой")
            if path.parts[:len(expected.parts)] != expected.parts:
                continue
            target = destination.joinpath(*path.parts[len(expected.parts):])
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not entry.isfile():
                raise ValueError("Недопустимый файл в архиве обновления")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    if not all((destination / filename).is_file() for filename in ("__init__.py", "__main__.py")):
        raise ValueError("В архиве отсутствуют файлы запуска LocalNET")


def _replace_package(package: Path, staged: Path) -> None:
    exchange = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if exchange is not None:
        exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        exchange.restype = ctypes.c_int
        # RENAME_EXCHANGE меняет каталоги местами одной операцией на поддерживаемых ФС.
        if exchange(-100, os.fsencode(package), -100, os.fsencode(staged), 2) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error))
    backup = package.with_name(".localnet-previous")
    if backup.exists():
        raise OSError(f"Уже есть резервная копия программы в {backup}")
    os.replace(package, backup)
    try:
        os.replace(staged, package)
    except OSError:
        os.replace(backup, package)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def install_release(release: Release, package: Path) -> None:
    import fcntl

    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release.tag) or release.asset != (
            f"LocalNET-{release.tag}-python.tar.gz"):
        raise ValueError("Некорректное имя архива релиза")
    with (package.parent / ".localnet-update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = _fetch(f"{DOWNLOAD_ROOT}/{release.tag}/{release.asset}", MAX_ARCHIVE)
        if hashlib.sha256(data).hexdigest() != release.checksum:
            raise ValueError("Контрольная сумма архива не совпала")
        with tempfile.TemporaryDirectory(prefix=".localnet-stage-", dir=package.parent) as temp:
            staged = Path(temp) / "package"
            _extract_package(data, release, staged)
            _replace_package(package, staged)
