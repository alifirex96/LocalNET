"""Сглаженная растеризация: корректный PNG, плавные края, иконки."""

import struct
import zlib

import pytest

from localnet import aa


def decode_png(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    w = h = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 4 + 1) + 1:(y + 1) * (w * 4 + 1)] for y in range(h)]
    return w, h, rows


def alpha(rows, x, y):
    return rows[y][x * 4 + 3]


def test_circle_is_antialiased():
    w, h, rows = decode_png(aa.render(20, 20, [(aa.sd_circle(10, 10, 9), "#e2615f")]))
    assert (w, h) == (20, 20)
    assert alpha(rows, 10, 10) == 255          # центр закрашен
    assert alpha(rows, 0, 0) == 0              # угол прозрачен
    edge = [alpha(rows, x, 10) for x in range(0, 4)]
    assert any(0 < a < 255 for a in edge)      # на краю — полупрозрачные пиксели (сглаживание)
    assert rows[10][40:43] == bytes([0xe2, 0x61, 0x5f])


def test_layers_composite():
    _, _, rows = decode_png(aa.render(10, 10, [(aa.sd_circle(5, 5, 5), "#ffffff"), (aa.sd_circle(5, 5, 2), "#000000")]))
    assert rows[5][20:23] == b"\x00\x00\x00" and rows[5][4:7] == b"\xff\xff\xff"


@pytest.mark.parametrize("name", sorted(aa.ICONS))
def test_icons_render(name):
    w, h, rows = decode_png(aa.render_icon(name, 24, "#1f2933"))
    assert (w, h) == (24, 24)
    alphas = [alpha(rows, x, y) for y in range(24) for x in range(24)]
    assert max(alphas) == 255                       # иконка нарисована
    assert any(0 < a < 255 for a in alphas)         # и края сглажены
    assert alphas.count(0) > 24 * 24 // 4           # фон прозрачный


def test_polygon_inside_outside():
    f = aa.sd_polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert f(5, 5) < 0 < f(15, 5)
