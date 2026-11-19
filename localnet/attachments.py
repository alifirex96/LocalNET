"""Описания альбомов и локальное хранилище изображений."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import zlib
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path

from .utils import IO_CHUNK, Cancelled, safe_filename, sha256_file

FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF", ".bmp": "BMP"}
MAX_ALBUM = 30
MAX_CAPTION = 2048
MAX_IMAGE_BYTES = 512 * 1024 * 1024
MAX_DECODE_PIXELS = 25_000_000
PHOTO_SIZE = 2048
DECODERS = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class ImageAttachment:
    album: str
    index: int
    count: int
    mode: str
    caption: str = ""

    def wire(self) -> dict:
        return asdict(self)

    @classmethod
    def from_wire(cls, obj: object) -> ImageAttachment:
        if not isinstance(obj, dict):
            raise ValueError("Некорректное описание изображения")
        album, index, count, mode, caption = (obj.get(key) for key in ("album", "index", "count", "mode", "caption"))
        if (not isinstance(album, str) or re.fullmatch(r"[0-9a-f]{32}", album) is None
                or type(index) is not int or type(count) is not int or not 0 <= index < count <= MAX_ALBUM
                or mode not in ("photo", "file") or not isinstance(caption, str) or len(caption) > MAX_CAPTION
                or "\x00" in caption):
            raise ValueError("Некорректное описание изображения")
        return cls(album, index, count, mode, caption)


def pil_modules():
    try:
        from PIL import Image, ImageOps
    except (ImportError, OSError):
        return None
    return Image, ImageOps


def system_codec() -> str | None:
    return shutil.which("magick") or shutil.which("convert")


def resize_with_system_codec(source: Path, destination: Path, fmt: str, edge: int) -> bool:
    binary = system_codec()
    if binary is None or fmt not in FORMATS.values():
        return False
    with DECODERS, source.open("rb") as stream:
        subprocess.run([binary, "-limit", "memory", "96MiB", "-limit", "map", "96MiB",
                        "-limit", "disk", "0", f"{fmt.lower()}:-[0]", "-auto-orient",
                        "-thumbnail", f"{edge}x{edge}>", f"png:{destination}"],
                       stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=True)
    return True


def verify_image(path: Path, name: str) -> str:
    """Проверяет соответствие контейнера имени и обнаруживает повреждённые изображения."""
    expected = FORMATS.get(Path(name).suffix.lower())
    size = path.stat().st_size
    if expected is None or not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("Неподдерживаемый формат или размер изображения")
    with path.open("rb") as stream:
        head = stream.read(32)
        valid = {
            "PNG": head.startswith(b"\x89PNG\r\n\x1a\n"),
            "JPEG": head.startswith(b"\xff\xd8\xff"),
            "GIF": head.startswith((b"GIF87a", b"GIF89a")),
            "WEBP": head.startswith(b"RIFF") and head[8:12] == b"WEBP",
            "BMP": head.startswith(b"BM") and len(head) >= 26,
        }[expected]
        if not valid:
            raise ValueError("Содержимое изображения не соответствует формату файла")
        if expected == "PNG":
            stream.seek(8)
            _verify_png(stream)
        elif expected == "JPEG":
            stream.seek(-min(size, 65536), os.SEEK_END)
            if stream.read().rfind(b"\xff\xd9") < 0:
                raise ValueError("JPEG оборван")
        elif expected == "GIF":
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b";":
                raise ValueError("GIF оборван")
        elif expected == "WEBP" and int.from_bytes(head[4:8], "little") + 8 != size:
            raise ValueError("Неверная длина WebP")
        elif expected == "BMP" and not 26 <= int.from_bytes(head[10:14], "little") < size:
            raise ValueError("Некорректный заголовок BMP")
    modules = pil_modules()
    if modules:
        image, _ = modules
        try:
            with DECODERS, image.open(path, formats=[expected]) as raster:
                _limit_decode(raster)
                raster.verify()
            with DECODERS, image.open(path, formats=[expected]) as raster:
                raster.load()
        except (OSError, SyntaxError, image.DecompressionBombError) as exc:
            raise ValueError("Не удалось декодировать изображение") from exc
    return expected


def _verify_png(stream) -> None:
    first = True
    data_seen = False
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("PNG оборван")
        length, kind = struct.unpack(">I4s", header)
        if first and (kind != b"IHDR" or length != 13):
            raise ValueError("Некорректный заголовок PNG")
        first = False
        crc = zlib.crc32(kind)
        remaining = length
        while remaining:
            chunk = stream.read(min(remaining, IO_CHUNK))
            if not chunk:
                raise ValueError("PNG оборван")
            crc = zlib.crc32(chunk, crc)
            remaining -= len(chunk)
        if stream.read(4) != struct.pack(">I", crc):
            raise ValueError("Повреждён блок PNG")
        data_seen = data_seen or kind == b"IDAT"
        if kind == b"IEND":
            if not data_seen or length:
                raise ValueError("В PNG отсутствуют данные изображения")
            return


def _limit_decode(raster) -> None:
    if not 0 < raster.width * raster.height <= MAX_DECODE_PIXELS:
        raise ValueError("Изображение слишком велико для декодирования")


class ImageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._prepared: OrderedDict = OrderedDict()

    def find_original(self, digest: str) -> Path | None:
        folder = self.root / "originals" / digest[:2] / digest
        return next((p for p in folder.iterdir() if p.is_file() and not p.is_symlink()), None) if folder.is_dir() else None

    def finalize(self, part: Path, digest: str, name: str) -> Path:
        """Сохраняет один проверенный экземпляр содержимого, сохраняя имя в записи передачи."""
        with self._lock:
            existing = self.find_original(digest)
            if existing is not None and sha256_file(existing) == digest:
                part.unlink()
                return existing
            folder = self.root / "originals" / digest[:2] / digest
            folder.mkdir(parents=True, exist_ok=True)
            target = existing or (folder / safe_filename(name))
            os.replace(part, target)
            return target

    def partial(self, digest: str, peer_id: str, meta: ImageAttachment) -> Path:
        path = self.root / "partial"
        path.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{peer_id}:{meta.album}:{meta.index}".encode()).hexdigest()
        return path / f"{digest}-{key}.part"

    def capture(self, data: bytes, suffix: str) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        name = f"Изображение-{digest[:16]}{suffix}"
        staging = self.root / "partial"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as tmp:
            path = Path(tmp) / "clipboard"
            path.write_bytes(data)
            verify_image(path, name)
            return self.finalize(path, digest, name)

    def prepare(self, source: Path, name: str, mode: str, cancel: threading.Event) -> tuple[Path, str, str]:
        """Готовит неизменяемую копию для передачи и повторных попыток."""
        stat = source.stat()
        key = (source.resolve(), stat.st_size, stat.st_mtime_ns, name, mode)
        with self._prepare_lock:
            if cancel.is_set():
                raise Cancelled()
            cached = self._prepared.get(key)
            if cached and cached[0].is_file():
                self._prepared.move_to_end(key)
                return cached
            fmt = verify_image(source, name)
            staging = self.root / "partial"
            staging.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=staging) as tmp:
                payload = Path(tmp) / "payload"
                if mode == "photo" and fmt != "GIF":
                    name = _prepare_photo(source, payload, name)
                else:
                    with source.open("rb") as reader, payload.open("wb") as writer:
                        while block := reader.read(IO_CHUNK):
                            if cancel.is_set():
                                raise Cancelled()
                            writer.write(block)
                verify_image(payload, name)
                digest = sha256_file(payload, cancel=cancel)
                result = self.finalize(payload, digest, name), name, digest
            self._prepared[key] = result
            if len(self._prepared) > 64:
                self._prepared.popitem(last=False)
            return result


def _prepare_photo(source: Path, payload: Path, name: str) -> str:
    modules = pil_modules()
    if modules is None:
        try:
            if resize_with_system_codec(source, payload, FORMATS[Path(name).suffix.lower()], PHOTO_SIZE):
                return str(Path(name).with_suffix(".png"))
        except subprocess.SubprocessError as exc:
            raise ValueError("Системный декодер не смог обработать изображение") from exc
        raise ValueError("Для оптимизации фото нужен Pillow или ImageMagick. Доступна отправка «Как файл».")
    image, ops = modules
    with DECODERS, image.open(source) as raster:
        if getattr(raster, "is_animated", False):
            shutil.copyfile(source, payload)
            return name
        raster.draft("RGB", (PHOTO_SIZE, PHOTO_SIZE))
        _limit_decode(raster)
        raster = ops.exif_transpose(raster)
        raster.thumbnail((PHOTO_SIZE, PHOTO_SIZE), image.Resampling.LANCZOS)
        if "A" in raster.getbands() or "transparency" in raster.info:
            raster.convert("RGBA").save(payload, "PNG")
            suffix = ".png"
        else:
            raster.convert("RGB").save(payload, "JPEG", quality=86, optimize=True)
            suffix = ".jpg"
    return str(Path(name).with_suffix(suffix))
