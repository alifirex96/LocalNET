"""Подписи Ed25519 (RFC 8032) на чистом Python.

Нужны для админ-панели: администратор подписывает список блокировок своим закрытым
ключом, а клиенты проверяют подпись открытым ключом. В стандартной библиотеке Python
нет асимметричной криптографии, а сторонние бинарные модули могут быть запрещены
(Astra Linux с ЗПС), поэтому реализация своя. Скорость — десятки миллисекунд на
операцию: подписи нужны редко (при изменении списка блокировок).

Реализация следует эталонному коду RFC 8032, раздел 6 (расширенные координаты).
"""

from __future__ import annotations

import hashlib
import os

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _sha512_mod_l(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little") % L


# Точки в расширенных координатах (X, Y, Z, T), x = X/Z, y = Y/Z, x*y = T/Z.

def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, p):
    q = (0, 1, 1, 0)  # нейтральный элемент
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p, q) -> bool:
    if (p[0] * q[2] - q[0] * p[2]) % P != 0:
        return False
    return (p[1] * q[2] - q[1] * p[2]) % P == 0


def _recover_x(y: int, sign: int):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _compress(p) -> bytes:
    zinv = pow(p[2], P - 2, P)
    x = p[0] * zinv % P
    y = p[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _expand_secret(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != 32:
        raise ValueError("Закрытый ключ Ed25519 — 32 байта")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def generate_secret() -> bytes:
    return os.urandom(32)


def public_key(secret: bytes) -> bytes:
    a, _ = _expand_secret(secret)
    return _compress(_mul(a, G))


def sign(secret: bytes, message: bytes) -> bytes:
    a, prefix = _expand_secret(secret)
    pub = _compress(_mul(a, G))
    r = _sha512_mod_l(prefix + message)
    rs = _compress(_mul(r, G))
    h = _sha512_mod_l(rs + pub + message)
    s = (r + h * a) % L
    return rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    if a is None:
        return False
    r = _decompress(signature[:32])
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = _sha512_mod_l(signature[:32] + public + message)
    return _equal(_mul(s, G), _add(r, _mul(h, a)))
