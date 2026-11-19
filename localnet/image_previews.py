"""Ограниченное по памяти создание миниатюр вне потока Tk."""

from __future__ import annotations

import base64
import io
import os
import queue
import re
import subprocess
import tempfile
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .attachments import DECODERS, FORMATS, MAX_DECODE_PIXELS, pil_modules, resize_with_system_codec

CACHE_ENTRIES = 56
THUMB_EDGE = 272
MAX_NATIVE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Preview:
    frames: tuple[tuple[bytes, int], ...]
    format: str = "png"
    edge: int = THUMB_EDGE

    def photo(self, root: tk.Misc, index: int = 0) -> tk.PhotoImage:
        data = base64.b64encode(self.frames[index][0]).decode("ascii")
        photo = tk.PhotoImage(master=root, data=data, format=self.format)
        stride = max(1, (max(photo.width(), photo.height()) + self.edge - 1) // self.edge)
        return photo.subsample(stride) if stride > 1 else photo


def decode_preview(path: Path, edge: int, animate: bool = False, disk_cache: Path | None = None) -> Preview:
    if disk_cache is not None and disk_cache.is_file() and disk_cache.stat().st_size <= MAX_NATIVE_BYTES:
        return Preview(((disk_cache.read_bytes(), 0),), edge=edge)
    modules = pil_modules()
    if modules is None:
        fmt = FORMATS.get(path.suffix.lower())
        if fmt:
            with tempfile.TemporaryDirectory() as folder:
                rendered = Path(folder) / "preview.png"
                try:
                    if resize_with_system_codec(path, rendered, fmt, edge):
                        return Preview(((rendered.read_bytes(), 0),), edge=edge)
                except (OSError, subprocess.SubprocessError):
                    pass
        if path.stat().st_size > MAX_NATIVE_BYTES:
            raise ValueError("Для просмотра этого изображения нужен Pillow")
        with path.open("rb") as stream:
            header = stream.read(24)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 24:
                fmt, order, start, size = "png", "big", 16, 4
            elif header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
                fmt, order, start, size = "gif", "little", 6, 2
            else:
                raise ValueError("Для просмотра этого формата нужен Pillow")
            width = int.from_bytes(header[start:start + size], order)
            height = int.from_bytes(header[start + size:start + 2 * size], order)
            if not 0 < width * height <= 2_000_000:
                raise ValueError("Для просмотра этого изображения нужен Pillow")
            stream.seek(0)
            return Preview(((stream.read(MAX_NATIVE_BYTES), 0),), fmt, edge)
    image, ops = modules
    with DECODERS, image.open(path) as raster:
        raster.draft("RGB", (edge, edge))
        if not 0 < raster.width * raster.height <= MAX_DECODE_PIXELS:
            raise ValueError("Изображение слишком велико для просмотра")
        frames = []
        frames_limit = 1
        if animate and getattr(raster, "is_animated", False):
            edge = min(640, edge)
            frames_limit = min(raster.n_frames, 40, max(2, 14_000_000 // (edge * edge)))
        for index in range(frames_limit):
            raster.seek(index)
            delay = min(2000, max(40, raster.info.get("duration", 100)))
            thumbnail = ops.exif_transpose(raster)
            thumbnail.thumbnail((edge, edge), image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            thumbnail.convert("RGBA").save(buffer, "PNG", compress_level=2)
            frames.append((buffer.getvalue(), delay))
    if disk_cache is not None:
        disk_cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=disk_cache.parent) as folder:
            temporary = Path(folder) / "preview.png"
            temporary.write_bytes(frames[0][0])
            os.replace(temporary, disk_cache)
    return Preview(tuple(frames), edge=edge)


class ImagePreviews:
    def __init__(self, root: tk.Tk, cache_dir: Path) -> None:
        self.root = root
        self.cache_dir = cache_dir
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image-preview")
        self._results = queue.Queue()
        self._pending = {}
        self._cache = OrderedDict()
        self._poll_id = None

    def request(self, filename: str, callback, *, edge: int = THUMB_EDGE, animate: bool = False,
                digest: str = "") -> None:
        path = Path(filename)
        try:
            stat = path.stat()
        except OSError:
            callback(None)
            return
        key = (path, stat.st_mtime_ns, stat.st_size, edge, animate)
        if key in self._cache:
            self._cache.move_to_end(key)
            callback(self._cache[key])
            return
        if key in self._pending:
            self._pending[key].add(callback)
            return
        if len(self._pending) >= 70:
            callback(None)
            return
        self._pending[key] = {callback}
        cache = (self.cache_dir / digest[:2] / f"{digest}-{edge}.png"
                 if re.fullmatch(r"[0-9a-f]{64}", digest) and edge == THUMB_EDGE and not animate else None)

        def produce() -> None:
            try:
                preview = decode_preview(path, edge, animate, cache)
            except (OSError, ValueError, SyntaxError, EOFError):
                preview = None
            except Exception:  # повреждённый кадр или недоступный декодер
                preview = None
            self._results.put((key, preview))

        self._executor.submit(produce)
        if self._poll_id is None:
            self._poll_id = self.root.after(35, self._poll)

    def _poll(self) -> None:
        self._poll_id = None
        for _ in range(16):
            try:
                key, preview = self._results.get_nowait()
            except queue.Empty:
                break
            if key[3] <= THUMB_EDGE and not key[4]:
                self._cache[key] = preview
                if len(self._cache) > CACHE_ENTRIES:
                    self._cache.popitem(last=False)
            for callback in self._pending.pop(key, ()):
                try:
                    callback(preview)
                except tk.TclError:
                    pass
        if self._pending:
            self._poll_id = self.root.after(35, self._poll)

    def close(self) -> None:
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
        self._executor.shutdown(wait=False)
