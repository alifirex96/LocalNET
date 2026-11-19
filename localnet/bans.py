"""Блокировки пользователей, подписанные администратором.

Сервера нет, поэтому блокировку применяет каждый клиент сам: не принимает сообщения
и файлы заблокированного, не показывает его в списке и не даёт ему писать. Список
подписывается закрытым ключом администратора (Ed25519); клиенты доверяют только
списку, подписанному ключом из настроек (``admin_public_key``) или из файла
``localnet-admin.pub``. Обычный пользователь подделать или отменить блокировку не может.

Список распространяется «по цепочке»: каждый узел сообщает в анонсе присутствия
версию своего списка (``bv``), и узел с более новой версией отправляет её соседу.
Так блокировка доходит и до компьютеров, которые были выключены во время её выдачи.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import ed25519
from .utils import canonical_json

log = logging.getLogger(__name__)

PUB_FILE_NAME = "localnet-admin.pub"
MAX_BANS = 500  # ~125 КБ — помещается в один кадр сообщений


@dataclass
class Ban:
    node_id: str
    ident: str = ""          # отпечаток «пользователь@компьютер» — бан переживает переустановку
    nick: str = ""
    until: float = 0.0       # 0 — навсегда, иначе время окончания (Unix time)
    reason: str = ""
    created: float = field(default_factory=time.time)

    def active(self, now: Optional[float] = None) -> bool:
        return self.until == 0 or self.until > (time.time() if now is None else now)

    def describe(self) -> str:
        if self.until == 0:
            return "навсегда"
        return "до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(self.until))


@dataclass
class BanList:
    version: int = 0
    bans: list[Ban] = field(default_factory=list)
    public: str = ""      # hex открытого ключа подписавшего
    signature: str = ""   # hex подписи Ed25519

    # ------------------------------------------------------------------ данные

    def payload(self) -> dict:
        return {"version": self.version, "bans": [asdict(b) for b in self.bans]}

    def to_wire(self) -> dict:
        return {"list": self.payload(), "pub": self.public, "esig": self.signature}

    @classmethod
    def from_wire(cls, obj: dict) -> Optional["BanList"]:
        """Разбор без проверки подписи. ``None`` для некорректных данных."""
        try:
            lst, pub, sig = obj["list"], obj["pub"], obj["esig"]
            if not isinstance(lst, dict) or not isinstance(pub, str) or not isinstance(sig, str):
                return None
            version = lst["version"]
            raw = lst["bans"]
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                return None
            if not isinstance(raw, list) or len(raw) > MAX_BANS:
                return None
            bans = []
            for b in raw:
                if not isinstance(b, dict) or not isinstance(b.get("node_id"), str):
                    return None
                until = b.get("until", 0)
                created = b.get("created", 0)
                if not isinstance(until, (int, float)) or not isinstance(created, (int, float)):
                    return None
                bans.append(Ban(node_id=b["node_id"][:64], ident=str(b.get("ident", ""))[:64],
                                nick=str(b.get("nick", ""))[:64], until=float(until),
                                reason=str(b.get("reason", ""))[:300], created=float(created)))
            bytes.fromhex(pub)
            bytes.fromhex(sig)
        except (KeyError, TypeError, ValueError):
            return None
        return cls(version=version, bans=bans, public=pub, signature=sig)

    # ------------------------------------------------------------------ подпись

    def sign(self, secret: bytes) -> None:
        self.public = ed25519.public_key(secret).hex()
        self.signature = ed25519.sign(secret, canonical_json(self.payload())).hex()

    def verify(self, trusted_public: str) -> bool:
        """Подпись верна и сделана доверенным ключом администратора."""
        if not trusted_public or self.public.lower() != trusted_public.lower():
            return False
        try:
            return ed25519.verify(bytes.fromhex(self.public), canonical_json(self.payload()),
                                  bytes.fromhex(self.signature))
        except ValueError:
            return False

    # ------------------------------------------------------------------ поиск

    def find(self, node_id: str, ident: str = "", now: Optional[float] = None) -> Optional[Ban]:
        for b in self.bans:
            if not b.active(now):
                continue
            if b.node_id == node_id or (ident and b.ident and b.ident == ident):
                return b
        return None


# ---------------------------------------------------------------------- ключи

def load_trusted_key(config_value: str, search_dirs: list[Path]) -> str:
    """Открытый ключ администратора: из настроек или из файла ``localnet-admin.pub``."""
    value = (config_value or "").strip().lower()
    if _valid_pub(value):
        return value
    for d in search_dirs:
        f = d / PUB_FILE_NAME
        try:
            if f.is_file():
                value = f.read_text(encoding="utf-8").strip().split()[0].lower()
                if _valid_pub(value):
                    return value
                log.warning("Файл %s не содержит корректного ключа администратора", f)
        except (OSError, IndexError):
            continue
    return ""


def _valid_pub(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def load_or_create_admin_secret(path: Path) -> bytes:
    """Закрытый ключ администратора (создаётся при первом запуске админ-панели)."""
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return bytes.fromhex(data["secret"])
    secret = ed25519.generate_secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"secret": secret.hex(), "public": ed25519.public_key(secret).hex(),
                               "warning": "ЗАКРЫТЫЙ КЛЮЧ АДМИНИСТРАТОРА — никому не передавайте"},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return secret
