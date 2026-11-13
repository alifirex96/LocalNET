"""Модели данных LocalNET."""

from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Peer:
    """Удалённый узел LocalNET, обнаруженный через discovery."""

    node_id: str
    nickname: str
    ip: str
    msg_port: int
    file_port: int
    version: str = ""
    last_seen: float = field(default_factory=time.time)
    online: bool = True

    @property
    def msg_endpoint(self) -> str:
        return f"tcp://{self.ip}:{self.msg_port}"

    def display_name(self) -> str:
        return f"{self.nickname} ({self.ip})"


@dataclass
class ChatMessage:
    """Текстовое сообщение. ``to is None`` — общий чат, иначе — личное."""

    msg_id: str
    sender_id: str
    sender_nick: str
    text: str
    to: Optional[str] = None
    ts: float = field(default_factory=time.time)
    sender_ip: str = ""
    outgoing: bool = False

    @property
    def is_private(self) -> bool:
        return self.to is not None


class TransferDirection(str, enum.Enum):
    SEND = "send"
    RECEIVE = "receive"


class TransferState(str, enum.Enum):
    PENDING = "ожидание"
    HASHING = "хеширование"
    WAITING_ACCEPT = "ждём согласия"
    TRANSFERRING = "передача"
    VERIFYING = "проверка"
    DONE = "готово"
    FAILED = "ошибка"
    CANCELLED = "отменено"
    DECLINED = "отклонено"

    @property
    def finished(self) -> bool:
        return self in (
            TransferState.DONE,
            TransferState.FAILED,
            TransferState.CANCELLED,
            TransferState.DECLINED,
        )


@dataclass
class Transfer:
    """Состояние одной передачи файла (входящей или исходящей)."""

    transfer_id: str
    direction: TransferDirection
    peer_id: str
    peer_nick: str
    peer_ip: str
    name: str
    size: int
    path: str = ""  # исходный файл (SEND) или итоговый путь (RECEIVE)
    sha256: str = ""
    state: TransferState = TransferState.PENDING
    done: int = 0
    offset: int = 0  # с какого байта началась передача (докачка)
    started: float = field(default_factory=time.time)
    finished_at: float = 0.0
    speed: float = 0.0  # байт/с
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 1.0 if self.state == TransferState.DONE else 0.0
        return min(1.0, self.done / self.size)

    def snapshot(self) -> "Transfer":
        """Копия без общих мутабельных ссылок — безопасно отдавать в GUI-поток."""
        return Transfer(
            transfer_id=self.transfer_id,
            direction=self.direction,
            peer_id=self.peer_id,
            peer_nick=self.peer_nick,
            peer_ip=self.peer_ip,
            name=self.name,
            size=self.size,
            path=self.path,
            sha256=self.sha256,
            state=self.state,
            done=self.done,
            offset=self.offset,
            started=self.started,
            finished_at=self.finished_at,
            speed=self.speed,
            error=self.error,
            cancel_event=self.cancel_event,
        )


@dataclass
class FileOffer:
    """Входящее предложение файла, ожидающее решения пользователя."""

    transfer: Transfer
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _accepted: bool = False

    def resolve(self, accepted: bool) -> None:
        self._accepted = accepted
        self._event.set()

    def wait(self, timeout: float) -> bool:
        """Ждёт решения; по таймауту — отказ."""
        if not self._event.wait(timeout):
            return False
        return self._accepted


class EventType(str, enum.Enum):
    """События ядра, которые получает интерфейс (GUI/CLI) через очередь."""

    PEER_UPDATED = "peer_updated"
    PEER_OFFLINE = "peer_offline"
    PEER_REMOVED = "peer_removed"
    MESSAGE = "message"
    MESSAGE_DELIVERED = "message_delivered"
    MESSAGE_FAILED = "message_failed"
    TRANSFER_UPDATED = "transfer_updated"
    FILE_OFFER = "file_offer"
    INFO = "info"
    ERROR = "error"


@dataclass
class AppEvent:
    type: EventType
    data: Any = None
