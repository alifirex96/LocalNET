"""Конфигурация LocalNET (JSON).

Поиск файла настроек (первый найденный):

1. путь из аргумента ``--config``;
2. ``localnet.json`` рядом с исполняемым файлом — portable-режим (флешка, сетевая папка);
3. ``%APPDATA%\\LocalNET\\config.json`` (Windows) или ``~/.config/localnet/config.json`` (Linux).

Если файла нет, он создаётся в профиле пользователя со значениями по умолчанию.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .utils import app_dir, default_download_dir, user_config_dir

log = logging.getLogger(__name__)

PORTABLE_CONFIG_NAME = "localnet.json"
USER_CONFIG_NAME = "config.json"

# Все порты > 1024 — права администратора не нужны.
DEFAULT_DISCOVERY_PORT = 45892
DEFAULT_MSG_PORT = 45893
DEFAULT_FILE_PORT = 45894
DEFAULT_MULTICAST_GROUP = "239.255.45.92"


def _default_nickname() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - на некоторых системах getuser() падает
        user = "user"
    return f"{user}@{socket.gethostname()}"


@dataclass
class Config:
    nickname: str = field(default_factory=_default_nickname)
    # UDP-порт discovery (broadcast/multicast). Должен совпадать у всех узлов.
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    # TCP-порт сообщений (ZeroMQ ROUTER). Если занят — берётся свободный, он анонсируется.
    msg_port: int = DEFAULT_MSG_PORT
    # TCP-порт передачи файлов. Если занят — берётся свободный, он анонсируется.
    file_port: int = DEFAULT_FILE_PORT
    # Адреса для broadcast-анонсов. Можно добавить направленный broadcast подсети,
    # например "192.168.1.255", если 255.255.255.255 режется роутером/ОС.
    broadcast_addresses: list[str] = field(default_factory=lambda: ["255.255.255.255"])
    # Multicast-группа (дублирует broadcast; пустая строка — отключить).
    multicast_group: str = DEFAULT_MULTICAST_GROUP
    # IP-адреса узлов, которым анонсы шлются напрямую (unicast) — для сегментов,
    # где broadcast и multicast заблокированы.
    static_peers: list[str] = field(default_factory=list)
    # Общий секрет: узлы с разными секретами не видят друг друга. Пусто — без проверки.
    secret: str = ""
    # Папка для входящих файлов. Пусто — ~/Downloads/LocalNET.
    download_dir: str = ""
    # Принимать входящие файлы без подтверждения.
    auto_accept: bool = False
    # Интервал анонсов присутствия и таймаут, после которого узел считается offline.
    announce_interval: float = 1.0
    peer_timeout: float = 6.0
    log_level: str = "INFO"
    # Транспорт сообщений: auto (pyzmq, если доступен, иначе чистый Python), zmq, python.
    # Оба транспорта совместимы по протоколу.
    transport: str = "auto"
    # Сохранять историю переписки на диске (файл <имя конфига>.history.db рядом с настройками).
    save_history: bool = True
    # Свои сообщения справа (по умолчанию все сообщения слева, свои — зелёные).
    own_messages_right: bool = False

    # Путь, откуда загружен конфиг (не сохраняется в файл).
    path: Optional[str] = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ #

    @property
    def download_path(self) -> Path:
        return Path(self.download_dir).expanduser() if self.download_dir else default_download_dir()

    def validate(self) -> None:
        """Приводит значения к допустимым, исправляя очевидные ошибки."""
        for name in ("discovery_port", "msg_port", "file_port"):
            value = int(getattr(self, name))
            if not (1024 < value < 65536) and value != 0:
                log.warning("Порт %s=%s вне диапазона 1025..65535, используется значение по умолчанию",
                            name, value)
                value = _DEFAULTS[name]
            setattr(self, name, value)
        self.nickname = (str(self.nickname).strip() or _default_nickname())[:64]
        self.announce_interval = max(0.2, float(self.announce_interval))
        self.peer_timeout = max(self.announce_interval * 3, float(self.peer_timeout))
        if self.transport not in ("auto", "zmq", "python"):
            log.warning("Неизвестный transport=%r, используется auto", self.transport)
            self.transport = "auto"
        if isinstance(self.broadcast_addresses, str):
            self.broadcast_addresses = [self.broadcast_addresses]
        if isinstance(self.static_peers, str):
            self.static_peers = [self.static_peers]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("path", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)} - {"path"}
        unknown = set(data) - known
        if unknown:
            log.warning("Неизвестные поля в конфиге проигнорированы: %s", ", ".join(sorted(unknown)))
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------ #

    def save(self, path: Optional[str | os.PathLike] = None) -> Path:
        target = Path(path or self.path or (user_config_dir() / USER_CONFIG_NAME))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)  # атомарная запись
        self.path = str(target)
        return target


_DEFAULTS = {
    "discovery_port": DEFAULT_DISCOVERY_PORT,
    "msg_port": DEFAULT_MSG_PORT,
    "file_port": DEFAULT_FILE_PORT,
}


def find_config_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    portable = app_dir() / PORTABLE_CONFIG_NAME
    if portable.exists():
        return portable
    return user_config_dir() / USER_CONFIG_NAME


def load_config(explicit: Optional[str] = None) -> Config:
    """Загружает конфиг; при отсутствии или ошибке — создаёт конфиг по умолчанию."""
    path = find_config_path(explicit)
    cfg: Config
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("корень JSON должен быть объектом")
            cfg = Config.from_dict(data)
        except (OSError, ValueError, TypeError) as exc:
            log.error("Не удалось прочитать конфиг %s: %s — используются значения по умолчанию", path, exc)
            cfg = Config()
            cfg.path = str(path)
            return cfg  # не перезаписываем повреждённый файл пользователя
        cfg.path = str(path)
        return cfg

    cfg = Config()
    cfg.validate()
    try:
        cfg.save(path)
        log.info("Создан файл настроек: %s", path)
    except OSError as exc:
        log.warning("Не удалось сохранить конфиг %s: %s", path, exc)
        cfg.path = str(path)
    return cfg
