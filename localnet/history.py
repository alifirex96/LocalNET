"""История переписки на диске (SQLite из стандартной библиотеки Python).

Хранит сообщения, отметки доставки и прочтения, реакции, передачи файлов и
известных собеседников, а также постоянный идентификатор узла — чтобы после
перезапуска переписка с тем же человеком продолжалась в том же чате.

Файл лежит рядом с файлом настроек: ``config.json`` → ``config.history.db``
(в portable-режиме — рядом с программой). Доступ потокобезопасный: одно
соединение под блокировкой; все операции короткие.
"""

from __future__ import annotations

import getpass
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .models import ALL_CHAT, ChatMessage, Transfer, TransferDirection, TransferState

try:
    import sqlite3
except Exception:  # noqa: BLE001 - урезанные сборки Python без _sqlite3
    sqlite3 = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contacts (
    node_id TEXT PRIMARY KEY, nickname TEXT NOT NULL, ip TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT PRIMARY KEY, conv TEXT NOT NULL, sender_id TEXT NOT NULL, sender_nick TEXT NOT NULL,
    text TEXT NOT NULL, recipient TEXT, ts REAL NOT NULL, outgoing INTEGER NOT NULL,
    targets INTEGER NOT NULL DEFAULT 1, read_sent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_conv_ts ON messages (conv, ts);
CREATE TABLE IF NOT EXISTS receipts (
    msg_id TEXT NOT NULL, peer_id TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (msg_id, peer_id, kind));
CREATE TABLE IF NOT EXISTS reactions (
    msg_id TEXT NOT NULL, peer_id TEXT NOT NULL, code TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (msg_id, peer_id));
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY, conv TEXT NOT NULL, direction TEXT NOT NULL, peer_id TEXT NOT NULL,
    peer_nick TEXT NOT NULL, peer_ip TEXT, name TEXT NOT NULL, size INTEGER NOT NULL, path TEXT,
    sha256 TEXT, state TEXT NOT NULL, error TEXT, started REAL NOT NULL, finished REAL,
    speed REAL, offset INTEGER);
CREATE INDEX IF NOT EXISTS transfers_conv ON transfers (conv, started);
"""


def history_path_for(config_path: Optional[str]) -> Optional[Path]:
    """``.../config.json`` → ``.../config.history.db``."""
    if not config_path:
        return None
    p = Path(config_path)
    return p.with_name(p.stem + ".history.db")


def _owner() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "?"
    return f"{user}@{socket.gethostname()}"


@dataclass
class StoredMessage:
    """Сообщение из истории вместе с отметками и реакциями."""

    msg: ChatMessage
    conv: str
    targets: int = 1
    read_sent: bool = False
    delivered: set = field(default_factory=set)
    failed: set = field(default_factory=set)
    read: set = field(default_factory=set)
    reactions: dict = field(default_factory=dict)  # peer_id -> code


class HistoryStore:
    """Хранилище истории. Если SQLite недоступен или файл не открывается — работает «в памяти»."""

    def __init__(self, path: Optional[Path], enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.RLock()
        self._db = None
        target = ":memory:"
        if path is not None and sqlite3 is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                target = str(path)
            except OSError as exc:
                log.warning("Нет доступа к каталогу истории %s: %s — история не сохраняется", path.parent, exc)
        if sqlite3 is None:
            log.warning("Модуль sqlite3 недоступен — история переписки не сохраняется")
            self.enabled = False
            return
        try:
            self._db = sqlite3.connect(target, check_same_thread=False, timeout=5)
            self._db.executescript(_SCHEMA)
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
                try:
                    self._db.execute(pragma)
                except sqlite3.DatabaseError:
                    pass
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            self._db.commit()
        except sqlite3.DatabaseError as exc:
            log.error("Файл истории %s повреждён или недоступен (%s) — используется временная история", path, exc)
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
            self._db.executescript(_SCHEMA)
        if target == ":memory:":
            self.enabled = False

    # ------------------------------------------------------------------ служебное

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                except Exception:  # noqa: BLE001
                    pass
                self._db = None

    def _exec(self, sql: str, args: Iterable = (), commit: bool = True):
        if self._db is None:
            return []
        with self._lock:
            try:
                cur = self._db.execute(sql, tuple(args))
                rows = cur.fetchall()
                if commit:
                    self._db.commit()
                return rows
            except Exception as exc:  # noqa: BLE001 - ошибка записи истории не должна ронять чат
                log.warning("Ошибка истории: %s (%s)", exc, sql.split()[0])
                return []

    def _store(self) -> bool:
        return self._db is not None and self.enabled

    # ------------------------------------------------------------------ узел

    def node_id(self) -> str:
        """Постоянный ID узла. Новый, если файл скопирован с другого компьютера/пользователя."""
        owner = _owner()
        rows = self._exec("SELECT key, value FROM meta WHERE key IN ('node_id', 'owner')", commit=False)
        meta = dict(rows)
        if meta.get("node_id") and meta.get("owner") == owner:
            return meta["node_id"]
        node_id = uuid.uuid4().hex
        if meta.get("node_id"):
            log.info("Файл истории создан на %s — для %s выдан новый ID узла", meta.get("owner"), owner)
        self._exec("INSERT OR REPLACE INTO meta VALUES ('node_id', ?)", (node_id,))
        self._exec("INSERT OR REPLACE INTO meta VALUES ('owner', ?)", (owner,))
        return node_id

    # ------------------------------------------------------------------ контакты

    def upsert_contact(self, node_id: str, nickname: str, ip: str, last_seen: float) -> None:
        if self._store():
            self._exec("INSERT INTO contacts VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
                       "nickname=excluded.nickname, ip=excluded.ip, last_seen=excluded.last_seen",
                       (node_id, nickname, ip, last_seen))

    def contacts(self) -> dict[str, tuple[str, str, float]]:
        rows = self._exec("SELECT node_id, nickname, ip, last_seen FROM contacts", commit=False)
        return {r[0]: (r[1], r[2] or "", r[3] or 0.0) for r in rows}

    # ------------------------------------------------------------------ сообщения

    @staticmethod
    def conv_of(msg: ChatMessage) -> str:
        if msg.to is None:
            return ALL_CHAT
        return msg.to if msg.outgoing else msg.sender_id

    def add_message(self, msg: ChatMessage, targets: int = 1) -> None:
        if self._store():
            self._exec("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                       (msg.msg_id, self.conv_of(msg), msg.sender_id, msg.sender_nick, msg.text, msg.to,
                        msg.ts, int(msg.outgoing), targets))

    def add_receipt(self, msg_id: str, peer_id: str, kind: str) -> None:
        if self._store():
            self._exec("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?)", (msg_id, peer_id, kind, time.time()))

    def set_reaction(self, msg_id: str, peer_id: str, code: str) -> None:
        if not self._store():
            return
        if code:
            self._exec("INSERT OR REPLACE INTO reactions VALUES (?, ?, ?, ?)", (msg_id, peer_id, code, time.time()))
        else:
            self._exec("DELETE FROM reactions WHERE msg_id=? AND peer_id=?", (msg_id, peer_id))

    def message_conv(self, msg_id: str) -> Optional[str]:
        rows = self._exec("SELECT conv FROM messages WHERE msg_id=?", (msg_id,), commit=False)
        return rows[0][0] if rows else None

    def unread(self, conv: str) -> list[tuple[str, str]]:
        """Входящие без отправленной отметки «прочитано»: [(msg_id, sender_id)]."""
        return [(r[0], r[1]) for r in self._exec(
            "SELECT msg_id, sender_id FROM messages WHERE conv=? AND outgoing=0 AND read_sent=0 ORDER BY ts",
            (conv,), commit=False)]

    def unread_counts(self) -> dict[str, int]:
        rows = self._exec("SELECT conv, COUNT(*) FROM messages WHERE outgoing=0 AND read_sent=0 GROUP BY conv",
                          commit=False)
        return {r[0]: r[1] for r in rows}

    def mark_read_sent(self, msg_ids: list[str]) -> None:
        if self._store() and msg_ids:
            with self._lock:
                for i in range(0, len(msg_ids), 500):
                    chunk = msg_ids[i:i + 500]
                    self._exec(f"UPDATE messages SET read_sent=1 WHERE msg_id IN ({','.join('?' * len(chunk))})",
                               chunk)

    def load_messages(self, conv: str, limit: int = 500) -> list[StoredMessage]:
        rows = self._exec(
            "SELECT * FROM (SELECT msg_id, sender_id, sender_nick, text, recipient, ts, outgoing, targets, read_sent "
            "FROM messages WHERE conv=? ORDER BY ts DESC LIMIT ?) ORDER BY ts", (conv, limit), commit=False)
        result: dict[str, StoredMessage] = {}
        for msg_id, sender_id, nick, text, to, ts, outgoing, targets, read_sent in rows:
            msg = ChatMessage(msg_id=msg_id, sender_id=sender_id, sender_nick=nick, text=text, to=to, ts=ts,
                              outgoing=bool(outgoing))
            result[msg_id] = StoredMessage(msg, conv, targets, bool(read_sent))
        if result:
            ids = list(result)
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for msg_id, peer_id, kind in self._exec(
                        f"SELECT msg_id, peer_id, kind FROM receipts WHERE msg_id IN ({marks})", chunk, commit=False):
                    sm = result[msg_id]
                    {"delivered": sm.delivered, "failed": sm.failed, "read": sm.read}.get(kind, set()).add(peer_id)
                for msg_id, peer_id, code in self._exec(
                        f"SELECT msg_id, peer_id, code FROM reactions WHERE msg_id IN ({marks})", chunk, commit=False):
                    result[msg_id].reactions[peer_id] = code
        return list(result.values())

    # ------------------------------------------------------------------ передачи

    def upsert_transfer(self, t: Transfer) -> None:
        if self._store():
            self._exec("INSERT OR REPLACE INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (t.transfer_id, t.peer_id, t.direction.value, t.peer_id, t.peer_nick, t.peer_ip, t.name,
                        t.size, t.path, t.sha256, t.state.name, t.error, t.started, t.finished_at, t.speed,
                        t.offset))

    def load_transfers(self, conv: Optional[str] = None, limit: int = 500) -> list[Transfer]:
        where, args = ("WHERE conv=?", [conv]) if conv is not None else ("", [])
        rows = self._exec(
            f"SELECT * FROM (SELECT transfer_id, direction, peer_id, peer_nick, peer_ip, name, size, path, sha256, "
            f"state, error, started, finished, speed, offset FROM transfers {where} ORDER BY started DESC LIMIT ?) "
            f"ORDER BY started", [*args, limit], commit=False)
        result = []
        for (tid, direction, peer_id, nick, ip, name, size, path, sha, state, error, started, finished, speed,
             offset) in rows:
            try:
                st = TransferState[state]
            except KeyError:
                st = TransferState.FAILED
            if not st.finished:
                # Программа закрылась посреди передачи.
                st, error = TransferState.FAILED, "прервано при закрытии программы"
            t = Transfer(transfer_id=tid, direction=TransferDirection(direction), peer_id=peer_id, peer_nick=nick,
                         peer_ip=ip or "", name=name, size=size, path=path or "", sha256=sha or "", state=st,
                         done=size if st == TransferState.DONE else 0, offset=offset or 0, started=started,
                         finished_at=finished or 0.0, speed=speed or 0.0, error=error or "")
            result.append(t)
        return result

    # ------------------------------------------------------------------ сводка

    def conversations(self) -> dict[str, tuple[float, str]]:
        """conv -> (время последнего события, текст для превью)."""
        out: dict[str, tuple[float, str]] = {}
        for conv, ts, nick, text, outgoing in self._exec(
                "SELECT m.conv, m.ts, m.sender_nick, m.text, m.outgoing FROM messages m "
                "JOIN (SELECT conv, MAX(ts) AS mts FROM messages GROUP BY conv) x ON x.conv=m.conv AND x.mts=m.ts",
                commit=False):
            if outgoing:
                prev = f"Вы: {text}"
            elif conv == ALL_CHAT:
                prev = f"{nick}: {text}"
            else:
                prev = text
            out[conv] = (ts, prev)
        for conv, ts, name, direction in self._exec(
                "SELECT t.conv, t.started, t.name, t.direction FROM transfers t "
                "JOIN (SELECT conv, MAX(started) AS ms FROM transfers GROUP BY conv) x "
                "ON x.conv=t.conv AND x.ms=t.started", commit=False):
            if ts > out.get(conv, (0.0, ""))[0]:
                out[conv] = (ts, ("↑ " if direction == "send" else "↓ ") + name)
        return out

    def clear(self) -> None:
        """Удалить всю переписку (ID узла и контакты сохраняются)."""
        for table in ("messages", "receipts", "reactions", "transfers"):
            self._exec(f"DELETE FROM {table}")
