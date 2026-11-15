"""Вспомогательные функции: подписи HMAC, хеширование файлов, пути, форматирование."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import sys
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

# Размер блока для чтения файлов при хешировании и chunked-передаче.
# 1 МБ — хороший компромисс: мало системных вызовов и небольшой расход памяти.
IO_CHUNK = 1024 * 1024

# Символы, запрещённые в именах файлов Windows (на Linux допустимы, но
# нормализуем одинаково, чтобы файл можно было переслать дальше на Windows).
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
# JSON и подписи
# --------------------------------------------------------------------------- #

def canonical_json(obj: dict) -> bytes:
    """Детерминированная сериализация словаря (для подписи и передачи)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(obj: dict, secret: str) -> str:
    """HMAC-SHA256 от словаря без поля ``sig``. Пустой секрет — пустая подпись."""
    if not secret:
        return ""
    payload = {k: v for k, v in obj.items() if k != "sig"}
    return hmac.new(secret.encode("utf-8"), canonical_json(payload), hashlib.sha256).hexdigest()


def verify(obj: dict, secret: str) -> bool:
    """Проверка подписи. Если секрет не задан — принимаем всё."""
    if not secret:
        return True
    sig = obj.get("sig")
    if not isinstance(sig, str):
        return False
    return hmac.compare_digest(sig, sign(obj, secret))


# --------------------------------------------------------------------------- #
# Хеширование
# --------------------------------------------------------------------------- #

class Cancelled(Exception):
    """Операция отменена пользователем."""


def sha256_file(
    path: str | os.PathLike,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
    limit: Optional[int] = None,
    chunk_size: int = IO_CHUNK,
) -> str:
    """Потоковый SHA-256 файла (никогда не читает файл целиком в память).

    :param progress: вызывается как ``progress(done, total)``.
    :param cancel: если событие установлено — бросается :class:`Cancelled`.
    :param limit: хешировать только первые ``limit`` байт.
    """
    h = hashlib.sha256()
    total = os.path.getsize(path) if limit is None else limit
    done = 0
    buf = bytearray(chunk_size)
    view = memoryview(buf)
    with open(path, "rb", buffering=0) as f:
        while done < total:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            n = f.readinto(view[: min(chunk_size, total - done)])
            if not n:
                break
            h.update(view[:n])
            done += n
            if progress is not None:
                progress(done, total)
    if done != total:
        raise IOError(f"Файл укоротился во время чтения: {done} из {total} байт")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Пути и имена файлов
# --------------------------------------------------------------------------- #

def is_frozen() -> bool:
    """Запущены ли мы из собранного PyInstaller-исполняемого файла."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Каталог рядом с исполняемым файлом (portable-режим) или корень проекта."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_config_dir() -> Path:
    """Каталог настроек в профиле пользователя (не требует прав администратора)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "LocalNET"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "localnet"


def default_download_dir() -> Path:
    """``~/Downloads/LocalNET`` (на Linux учитываем XDG_DOWNLOAD_DIR, если задан)."""
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    base = Path(xdg) if xdg else Path.home() / "Downloads"
    return base / "LocalNET"


def safe_filename(name: str) -> str:
    """Безопасное имя файла: без каталогов, запрещённых символов и зарезервированных имён."""
    # Берём только последний компонент пути — защита от "../../etc/passwd".
    name = name.replace("\\", "/").split("/")[-1]
    name = _FORBIDDEN_CHARS.sub("_", name).strip().strip(".")
    if not name:
        name = "file"
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        name = "_" + name
    # Ограничиваем длину (255 байт — типичный лимит ФС), сохраняя расширение.
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        p = Path(name)
        ext = p.suffix[:20]
        stem_bytes = p.stem.encode("utf-8")[: 200 - len(ext.encode("utf-8"))]
        name = stem_bytes.decode("utf-8", "ignore") + ext
    return name


def path_from_uri(value: str) -> str:
    """``file:///home/u/%D0%A4.txt`` → ``/home/u/Ф.txt`` (перетаскивание/буфер обмена); путь — как есть."""
    value = value.strip()
    if value.startswith("file://"):
        path = unquote(urlparse(value).path)
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]  # /C:/... → C:/...
        return path
    return value


def unique_path(directory: Path, name: str) -> Path:
    """Свободное имя в каталоге: ``file.txt`` → ``file (1).txt`` → ``file (2).txt``…"""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    p = Path(name)
    for i in range(1, 10_000):
        candidate = directory / f"{p.stem} ({i}){p.suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Не удалось подобрать свободное имя для {name}")


# --------------------------------------------------------------------------- #
# Сеть
# --------------------------------------------------------------------------- #

def guess_local_ip() -> str:
    """Основной IP-адрес машины в LAN (без отправки пакетов наружу)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect() для UDP не шлёт пакетов, а лишь выбирает маршрут/интерфейс.
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tune_tcp_socket(sock: socket.socket, buf_size: int = 4 * 1024 * 1024) -> None:
    """Увеличенные буферы TCP — выше пропускная способность на гигабите и выше."""
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, buf_size)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def human_size(n: float) -> str:
    """Человекочитаемый размер: 1536 → '1.5 КБ'."""
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    for unit in units:
        if abs(n) < 1024 or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"  # pragma: no cover


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(bytes_per_sec)}/с"
