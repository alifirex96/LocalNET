"""Изображения профиля и табличный текст без сторонних Python-пакетов."""

from __future__ import annotations

import base64
import binascii
import math
import shutil
import struct
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

AVATAR_SIZE = 16
AVATAR_BYTES = AVATAR_SIZE ** 2
AVATAR_LENGTH = 4 * math.ceil(AVATAR_BYTES / 3)
IMAGE_SUFFIXES = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_PREVIEW_BYTES = 12 * 1024 * 1024
MAX_PREVIEW_PIXELS = 12_000_000


def avatar_pixels(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != AVATAR_LENGTH:
        return b""
    try:
        pixels = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return b""
    return pixels if len(pixels) == AVATAR_BYTES else b""


def encode_avatar(photo) -> str:
    pixels = bytearray()
    side = min(photo.width(), photo.height())
    left = (photo.width() - side) // 2
    top = (photo.height() - side) // 2
    for y in range(AVATAR_SIZE):
        for x in range(AVATAR_SIZE):
            px = left + (2 * x + 1) * side // (2 * AVATAR_SIZE)
            py = top + (2 * y + 1) * side // (2 * AVATAR_SIZE)
            r, g, b = photo.get(px, py)
            pixels.append((r & 224) | ((g & 224) >> 3) | (b >> 6))
    return base64.b64encode(pixels).decode("ascii")


def avatar_photo(root, value: str, zoom: int):
    import tkinter as tk

    pixels = avatar_pixels(value)
    if not pixels:
        return None
    size = AVATAR_SIZE * zoom
    photo = tk.PhotoImage(master=root, width=size, height=size)
    for y in range(size):
        row = []
        start = 0
        for x in range(size):
            if (2 * x + 1 - size) ** 2 + (2 * y + 1 - size) ** 2 > size ** 2:
                continue
            if not row:
                start = x
            color = pixels[y // zoom * AVATAR_SIZE + x // zoom]
            r, g, b = color >> 5, (color >> 2) & 7, color & 3
            row.append(f"#{r * 255 // 7:02x}{g * 255 // 7:02x}{b * 85:02x}")
        if row:
            photo.put("{" + " ".join(row) + "}", to=(start, y))
    return photo


def image_preview(root, filename: str, width: int, height: int):
    """Загружает ограниченное по размеру превью. PNG и GIF поддерживаются самим Tk."""
    import tkinter as tk

    path = Path(filename)
    if path.suffix.lower() not in IMAGE_SUFFIXES or path.stat().st_size > MAX_PREVIEW_BYTES:
        return None
    with path.open("rb") as stream:
        header = stream.read(24)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 24:
        fmt, dimensions = "png", struct.unpack(">II", header[16:24])
    elif header[:6] in (b"GIF87a", b"GIF89a") and len(header) >= 10:
        fmt, dimensions = "gif", struct.unpack("<HH", header[6:10])
    else:
        if header.startswith(b"\xff\xd8\xff"):
            coder = "jpeg"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            coder = "webp"
        elif header.startswith(b"BM"):
            coder = "bmp"
        else:
            return None
        thumbnailer = shutil.which("gdk-pixbuf-thumbnailer")
        converter = shutil.which("magick") or shutil.which("convert")
        if thumbnailer is None and converter is None:
            return None
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "preview.png"
            source = str(path.resolve())
            if thumbnailer is not None:
                command = [thumbnailer, "-s", str(max(width, height)), source, str(output)]
            else:
                command = [converter, "-limit", "memory", "64MiB", "-limit", "map", "64MiB",
                           "-limit", "disk", "0", f"{coder}:{source}[0]", "-thumbnail", f"{width}x{height}>",
                           f"png:{output}"]
            subprocess.run(command,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4, check=True)
            return image_preview(root, str(output), width, height)
    if min(dimensions) < 1 or math.prod(dimensions) > MAX_PREVIEW_PIXELS:
        return None
    photo = tk.PhotoImage(master=root, file=str(path), format=fmt)
    stride = max(1, math.ceil(photo.width() / width), math.ceil(photo.height() / height))
    return photo.subsample(stride) if stride > 1 else photo


def table_rows(text: str) -> list[list[str]]:
    rows = [line.split("\t") for line in text.strip("\n").splitlines()]
    columns = max((len(row) for row in rows), default=0)
    if columns < 2 or len(rows) > 300 or columns > 40:
        return []
    return [row + [""] * (columns - len(row)) for row in rows]


class _ClipboardTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.in_table = False
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if tag == "table" and not self.finished:
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.row is not None and tag in ("td", "th"):
            self.cell = []
        elif self.cell is not None and tag in ("br", "p"):
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
            self.cell = None
        elif tag == "table" and self.in_table:
            self.in_table = False
            self.finished = True

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def table_from_html(text: str) -> str:
    if len(text) > 2_000_000 or "<table" not in text.lower():
        return ""
    parser = _ClipboardTable()
    parser.feed(text)
    rows = parser.rows
    if not any(len(row) > 1 for row in rows):
        return ""
    return "\n".join("\t".join(row) for row in rows)
