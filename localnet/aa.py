"""Сглаженная (антиалиасинг) графика для tkinter на чистом Python.

Холст Tk рисует круги, скругления и наклонные линии без сглаживания — края
«пиксельные». Здесь формы растрируются самостоятельно: для каждого пикселя
считается расстояние до границы фигуры (signed distance) и из него — степень
покрытия, результат упаковывается в PNG с альфа-каналом и показывается как
картинка (Tk 8.6 смешивает альфа-канал с фоном).

Большие прямоугольники с закруглениями собираются «девятью кусками»: четыре
сглаженных угла-картинки и обычные прямоугольники холста (прямые края и так
чёткие) — так рисуется быстро при любом размере. Всё кэшируется.

Без сторонних библиотек: работает и на Astra Linux с ЗПС.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from typing import Callable, Optional, Sequence

Point = tuple[float, float]


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def png_rgba(width: int, height: int, data: bytes) -> bytes:
    """Минимальный PNG-кодер (RGBA, 8 бит, без фильтров)."""
    raw = b"".join(b"\x00" + data[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# Функции расстояния (отрицательные внутри фигуры)
# --------------------------------------------------------------------------- #

def sd_circle(cx: float, cy: float, r: float) -> Callable[[float, float], float]:
    return lambda x, y: math.hypot(x - cx, y - cy) - r


def sd_ring(cx: float, cy: float, r: float, width: float) -> Callable[[float, float], float]:
    mid = r - width / 2
    return lambda x, y: abs(math.hypot(x - cx, y - cy) - mid) - width / 2


def _seg_dist(px: float, py: float, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def sd_polyline(points: Sequence[Point], width: float) -> Callable[[float, float], float]:
    segs = list(zip(points, points[1:]))
    return lambda x, y: min(_seg_dist(x, y, a, b) for a, b in segs) - width / 2


def sd_polygon(points: Sequence[Point]) -> Callable[[float, float], float]:
    pts = list(points)
    edges = list(zip(pts, pts[1:] + pts[:1]))

    def f(x: float, y: float) -> float:
        d = min(_seg_dist(x, y, a, b) for a, b in edges)
        inside = False
        for (ax, ay), (bx, by) in edges:
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
        return -d if inside else d
    return f


def render(width: int, height: int, layers: Sequence[tuple[Callable[[float, float], float], str]],
           samples: int = 1) -> bytes:
    """Растрирует слои (функция расстояния, цвет) поверх прозрачного фона → PNG."""
    out = bytearray(width * height * 4)
    cols = [(_rgb(c), f) for f, c in layers]
    for y in range(height):
        for x in range(width):
            r = g = b = 0.0
            a = 0.0
            for (cr, cg, cb), f in cols:
                d = f(x + 0.5, y + 0.5)
                cov = 0.5 - d
                if cov <= 0:
                    continue
                if cov > 1:
                    cov = 1.0
                # «source over» в предумноженных величинах
                r = cr * cov + r * (1 - cov)
                g = cg * cov + g * (1 - cov)
                b = cb * cov + b * (1 - cov)
                a = cov + a * (1 - cov)
            i = (y * width + x) * 4
            if a > 0:
                out[i] = min(255, int(r / a + 0.5))
                out[i + 1] = min(255, int(g / a + 0.5))
                out[i + 2] = min(255, int(b / a + 0.5))
                out[i + 3] = min(255, int(a * 255 + 0.5))
    return png_rgba(width, height, bytes(out))


# --------------------------------------------------------------------------- #
# Иконки: контуры в системе координат 24×24
# --------------------------------------------------------------------------- #

def _arc(cx, cy, r, a1, a2, n=10) -> list[Point]:
    return [(cx + r * math.cos(math.radians(a1 + (a2 - a1) * i / n)),
             cy + r * math.sin(math.radians(a1 + (a2 - a1) * i / n))) for i in range(n + 1)]


ICONS: dict[str, list[tuple[str, object]]] = {
    # («stroke», точки) — линия толщиной stroke; («fill», точки) — залитый многоугольник;
    # («ring», (cx, cy, r)) — окружность; («dot», (cx, cy, r)) — круг.
    "chats": [("stroke", _arc(7, 6, 3, 180, 270) + _arc(14, 6, 3, 270, 360) + _arc(14, 12, 3, 0, 90)
               + [(8, 15), (5, 18.5), (5.5, 15)] + _arc(7, 12, 3, 90, 180) + [(4, 6)]),
              ("stroke", [(17.5, 9), (19, 9)] + _arc(19, 11, 2, 270, 360) + [(21, 18), (19, 16), (12, 16)])],
    "files": [("stroke", [(6, 3), (14, 3), (19, 8), (19, 21), (6, 21), (6, 3)]),
              ("stroke", [(14, 3), (14, 8), (19, 8)]),
              ("stroke", [(12.5, 10.5), (12.5, 17.5)]),
              ("stroke", [(9.5, 14.5), (12.5, 17.5), (15.5, 14.5)])],
    "gear": [("ring", (12, 12, 7.2)), ("ring", (12, 12, 3.2))]
            + [("stroke", [(12 + 7 * math.cos(math.radians(a)), 12 + 7 * math.sin(math.radians(a))),
                           (12 + 10 * math.cos(math.radians(a)), 12 + 10 * math.sin(math.radians(a)))])
               for a in range(0, 360, 45)],
    "clip": [("stroke", [(15, 7), (15, 15.5)] + _arc(12.5, 15.5, 2.5, 0, 180) + [(10, 6)]
              + _arc(13.25, 6, 3.25, 180, 360) + [(16.5, 16)] + _arc(12.5, 16, 4, 0, 180) + [(8.5, 9)])],
    "send": [("fill", [(4, 4.5), (21, 12), (4, 19.5), (6.5, 12)])],
    "plus": [("ring", (12, 12, 9)), ("stroke", [(12, 7.5), (12, 16.5)]), ("stroke", [(7.5, 12), (16.5, 12)])],
    "search": [("ring", (10.5, 10.5, 6.5)), ("stroke", [(15.3, 15.3), (20, 20)])],
    "up": [("stroke", [(12, 18), (12, 6)]), ("stroke", [(7, 11), (12, 6), (17, 11)])],
    "down": [("stroke", [(12, 6), (12, 18)]), ("stroke", [(7, 13), (12, 18), (17, 13)])],
    "dots": [("dot", (12, 5, 1.8)), ("dot", (12, 12, 1.8)), ("dot", (12, 19, 1.8))],
    "logo": [("stroke", [(4.5, 17), (19.5, 17), (12, 5), (4.5, 17)]),
             ("dot", (4.5, 17, 3.3)), ("dot", (19.5, 17, 3.3)), ("dot", (12, 5, 3.3))],
    "lock": [("stroke", [(8, 11), (8, 8)] + _arc(12, 8, 4, 180, 360) + [(16, 11)]),
             ("fill", [(5.5, 11), (18.5, 11), (18.5, 20), (5.5, 20)])],
}


def render_icon(name: str, size: int, color: str, width: float = 2.0) -> bytes:
    """PNG иконки из ICONS (контуры заданы в сетке 24×24)."""
    scale = size / 24.0
    layers = []
    for kind, data in ICONS[name]:
        if kind == "stroke":
            layers.append((sd_polyline([(x * scale, y * scale) for x, y in data], width), color))  # type: ignore
        elif kind == "fill":
            layers.append((sd_polygon([(x * scale, y * scale) for x, y in data]), color))  # type: ignore
        elif kind == "ring":
            cx, cy, r = data  # type: ignore[misc]
            layers.append((sd_ring(cx * scale, cy * scale, r * scale + width / 2, width), color))
        elif kind == "dot":
            cx, cy, r = data  # type: ignore[misc]
            layers.append((sd_circle(cx * scale, cy * scale, r * scale), color))
    return render(size, size, layers)


class AA:
    """Кэш сглаженных картинок для одного окна Tk."""

    def __init__(self, master) -> None:
        import tkinter as tk
        self._tk = tk
        self.master = master
        self._cache: dict[tuple, object] = {}

    def _photo(self, key: tuple, make: Callable[[], bytes]):
        img = self._cache.get(key)
        if img is None:
            img = self._tk.PhotoImage(master=self.master, data=base64.b64encode(make()))
            self._cache[key] = img
        return img

    # ------------------------------------------------------------------ формы

    def circle(self, d: int, fill: str, outline: Optional[str] = None, width: float = 0):
        def make() -> bytes:
            layers = [(sd_circle(d / 2, d / 2, d / 2), outline or fill)]
            if outline and width:
                layers.append((sd_circle(d / 2, d / 2, d / 2 - width), fill))
            return render(d, d, layers)
        return self._photo(("circle", d, fill, outline, width), make)

    def ring(self, d: int, color: str, width: float):
        return self._photo(("ring", d, color, width), lambda: render(d, d, [(sd_ring(d / 2, d / 2, d / 2, width), color)]))

    def corner(self, r: int, fill: str, quadrant: str, outline: Optional[str] = None, width: float = 0):
        """Четверть круга радиуса ``r`` для угла прямоугольника: tl, tr, bl, br."""
        cx = r if quadrant in ("tl", "bl") else 0
        cy = r if quadrant in ("tl", "tr") else 0

        def make() -> bytes:
            layers = []
            if fill:
                layers.append((sd_circle(cx, cy, r - (width if outline else 0) / 2 if outline else r), fill))
            if outline and width:
                layers.append((sd_ring(cx, cy, r, width), outline))
            return render(r, r, layers)
        return self._photo(("corner", r, fill, quadrant, outline, width), make)

    def icon(self, name: str, size: int, color: str, width: float = 2.0):
        return self._photo(("icon", name, size, color, width), lambda: render_icon(name, size, color, width))

    # ------------------------------------------------------------------ рисование на холсте

    def draw_circle(self, cv, cx: float, cy: float, r: float, fill: str, outline: Optional[str] = None,
                    width: float = 0, tags=(), state: str = "normal") -> int:
        d = max(2, int(round(r * 2)))
        return cv.create_image(int(round(cx - d / 2)), int(round(cy - d / 2)), anchor="nw",
                               image=self.circle(d, fill, outline, width), tags=tags, state=state)

    def draw_icon(self, cv, cx: float, cy: float, name: str, size: int, color: str, width: float = 2.0,
                  tags=(), state: str = "normal") -> int:
        return cv.create_image(int(round(cx - size / 2)), int(round(cy - size / 2)), anchor="nw",
                               image=self.icon(name, size, color, width), tags=tags, state=state)

    def draw_rect(self, cv, x1: float, y1: float, x2: float, y2: float, r: float, fill: str,
                  outline: Optional[str] = None, width: float = 1.0, tags=(), state: str = "normal") -> list[int]:
        """Скруглённый прямоугольник: сглаженные углы-картинки + прямые части холста."""
        x1, y1, x2, y2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
        r = int(max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2)))
        ids: list[int] = []
        kw = {"tags": tags, "state": state}
        if r <= 0:
            return [cv.create_rectangle(x1, y1, x2, y2, fill=fill or "", outline=outline or "", **kw)]
        if fill:
            ids.append(cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="", width=0, **kw))
            ids.append(cv.create_rectangle(x1, y1 + r, x1 + r, y2 - r, fill=fill, outline="", width=0, **kw))
            ids.append(cv.create_rectangle(x2 - r, y1 + r, x2, y2 - r, fill=fill, outline="", width=0, **kw))
        for q, x, y in (("tl", x1, y1), ("tr", x2 - r, y1), ("bl", x1, y2 - r), ("br", x2 - r, y2 - r)):
            ids.append(cv.create_image(x, y, anchor="nw", image=self.corner(r, fill, q, outline, width), **kw))
        if outline and width:
            w = max(1, int(round(width)))
            h = w / 2
            ids.append(cv.create_line(x1 + r, y1 + h, x2 - r, y1 + h, fill=outline, width=w, **kw))
            ids.append(cv.create_line(x1 + r, y2 - h, x2 - r, y2 - h, fill=outline, width=w, **kw))
            ids.append(cv.create_line(x1 + h, y1 + r, x1 + h, y2 - r, fill=outline, width=w, **kw))
            ids.append(cv.create_line(x2 - h, y1 + r, x2 - h, y2 - r, fill=outline, width=w, **kw))
        return ids
