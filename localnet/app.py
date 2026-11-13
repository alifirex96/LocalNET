"""Ядро LocalNET: связывает discovery, сообщения и файлы; общается с UI через очередь событий."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from .config import Config
from .discovery import Discovery, PeerRegistry
from .file_transfer import ACCEPT_TIMEOUT, FileTransferManager
from .history import HistoryStore, history_path_for
from .messaging import Messenger
from .models import ALL_CHAT, REACTIONS, AppEvent, ChatMessage, EventType, FileOffer, Peer, Transfer
from .utils import guess_local_ip, user_config_dir

log = logging.getLogger(__name__)

# Получатель должен ответить раньше, чем у отправителя истечёт ACCEPT_TIMEOUT.
OFFER_DECISION_TIMEOUT = ACCEPT_TIMEOUT - 20


def setup_logging(level: str = "INFO", log_dir: Optional[Path] = None) -> Path:
    """Логи в консоль и во вращаемый файл ``<профиль>/logs/localnet.log``."""
    log_dir = log_dir or (user_config_dir() / "logs")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    log_file = log_dir / "localnet.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        print(f"Не удалось открыть лог-файл {log_file}: {exc}", file=sys.stderr)
    # В собранном оконном exe stderr может отсутствовать.
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return log_file


class LocalNetCore:
    """Сетевое ядро без UI. GUI/CLI читают ``events`` и вызывают публичные методы."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # История и постоянный ID узла (чтобы после перезапуска переписка продолжалась).
        self.history = HistoryStore(history_path_for(config.path), enabled=config.save_history)
        self.node_id = self.history.node_id()
        self._saved_transfer_state: dict[str, str] = {}
        self.events: "queue.Queue[AppEvent]" = queue.Queue()
        self.registry = PeerRegistry()
        self.local_ip = guess_local_ip()
        self._nick_lock = threading.Lock()
        self._nickname = config.nickname
        self._started = False

        self.messenger = Messenger(
            node_id=self.node_id,
            get_nickname=self.get_nickname,
            registry=self.registry,
            port=config.msg_port,
            secret=config.secret,
            on_message=self._on_message,
            on_delivered=lambda mid, pid: self._on_receipt(mid, pid, "delivered", EventType.MESSAGE_DELIVERED),
            on_failed=lambda mid, pid: self._on_receipt(mid, pid, "failed", EventType.MESSAGE_FAILED),
            on_read=self._on_read,
            on_reaction=self._on_reaction,
            transport=config.transport,
        )
        self.files = FileTransferManager(
            node_id=self.node_id,
            get_nickname=self.get_nickname,
            port=config.file_port,
            get_download_dir=lambda: self.config.download_path,
            secret=config.secret,
            on_update=self._on_transfer,
            on_offer=self._on_offer,
            resolve_peer=self._resolve_peer,
        )
        self.discovery: Optional[Discovery] = None

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        log.info("%s %s запускается: ник «%s», node_id %s, IP %s",
                 APP_NAME, __version__, self._nickname, self.node_id[:8], self.local_ip)
        self.messenger.start()
        self.files.start()
        # Discovery стартует последним: анонсируем реальные (возможно, запасные) порты.
        self.discovery = Discovery(
            node_id=self.node_id,
            get_nickname=self.get_nickname,
            msg_port=self.messenger.port,
            file_port=self.files.port,
            registry=self.registry,
            discovery_port=self.config.discovery_port,
            broadcast_addresses=self.config.broadcast_addresses,
            multicast_group=self.config.multicast_group,
            static_peers=self.config.static_peers,
            secret=self.config.secret,
            interval=self.config.announce_interval,
            timeout=self.config.peer_timeout,
            on_peer_updated=self._on_peer_updated,
            on_peer_offline=self._on_peer_offline,
            on_peer_removed=lambda p: self._emit(EventType.PEER_REMOVED, p),
        )
        try:
            self.discovery.start()
        except OSError as exc:
            # Без discovery приложение продолжает работать (например, порт занят чужой программой).
            log.error("Discovery не запущен (UDP %d): %s", self.config.discovery_port, exc)
            self._emit(EventType.ERROR, f"Не удалось открыть UDP-порт {self.config.discovery_port} "
                                        f"для обнаружения: {exc}")
            self.discovery = None
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info("Остановка…")
        if self.discovery is not None:
            self.discovery.stop()
        self.files.stop()
        self.messenger.stop()
        now = time.time()
        for peer in self.registry.online():
            self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, now)
        self.history.close()
        log.info("Остановлено")

    # ------------------------------------------------------------------ #

    def get_nickname(self) -> str:
        with self._nick_lock:
            return self._nickname

    def set_nickname(self, nickname: str) -> None:
        nickname = nickname.strip()[:64]
        if not nickname:
            raise ValueError("Ник не может быть пустым")
        with self._nick_lock:
            self._nickname = nickname
        self.config.nickname = nickname
        self.save_config()
        if self.discovery is not None:
            self.discovery.announce_now()

    def set_download_dir(self, path: str) -> None:
        self.config.download_dir = path
        self.save_config()

    def save_config(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            log.warning("Не удалось сохранить настройки: %s", exc)

    def send_message(self, text: str, to: Optional[str] = None) -> ChatMessage:
        targets = len(self.registry.online()) if to is None else 1
        msg = self.messenger.send(text, to)
        self.history.add_message(msg, targets)
        return msg

    def mark_read(self, conv: str) -> list[str]:
        """Отметить входящие сообщения чата прочитанными и сообщить об этом отправителям."""
        unread = self.history.unread(conv)
        if not unread:
            return []
        by_sender: dict[str, list[str]] = {}
        for msg_id, sender in unread:
            by_sender.setdefault(sender, []).append(msg_id)
        for sender, ids in by_sender.items():
            self.messenger.send_read(sender, ids)
        ids = [m for m, _ in unread]
        self.history.mark_read_sent(ids)
        return ids

    def react(self, msg_id: str, code: str, conv: Optional[str] = None) -> None:
        """Поставить (или снять, ``code=""``) свою реакцию на сообщение."""
        if code and code not in REACTIONS:
            raise ValueError(f"Неизвестная реакция {code!r}")
        conv = conv or self.history.message_conv(msg_id) or ALL_CHAT
        self.history.set_reaction(msg_id, self.node_id, code)
        if conv == ALL_CHAT:
            targets = [p.node_id for p in self.registry.online()]
        else:
            targets = [conv]
        self.messenger.send_reaction(msg_id, code, targets)

    def send_file(self, peer_id: str, path: str) -> Transfer:
        peer = self.registry.get(peer_id)
        if peer is None:
            raise ValueError("Пир не найден")
        if not peer.online:
            raise ValueError(f"{peer.nickname} сейчас offline")
        return self.files.send_file(peer, path)

    def cancel_transfer(self, transfer_id: str) -> bool:
        return self.files.cancel(transfer_id)

    # ------------------------------------------------------------------ #

    def _emit(self, etype: EventType, data=None) -> None:
        self.events.put(AppEvent(etype, data))

    def _on_peer_offline(self, peer: Peer) -> None:
        self.messenger.forget_peer(peer.node_id)
        self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, peer.last_seen)
        self._emit(EventType.PEER_OFFLINE, peer)

    def _on_peer_updated(self, peer: Peer) -> None:
        self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, peer.last_seen)
        self._emit(EventType.PEER_UPDATED, peer)

    def _on_message(self, msg: ChatMessage) -> None:
        self.history.add_message(msg)
        self._emit(EventType.MESSAGE, msg)

    def _on_receipt(self, msg_id: str, peer_id: str, kind: str, etype: EventType) -> None:
        self.history.add_receipt(msg_id, peer_id, kind)
        self._emit(etype, (msg_id, peer_id))

    def _on_read(self, peer_id: str, msg_ids: list) -> None:
        for msg_id in msg_ids:
            self.history.add_receipt(msg_id, peer_id, "read")
        self._emit(EventType.MESSAGE_READ, (peer_id, list(msg_ids)))

    def _on_reaction(self, msg_id: str, peer_id: str, code: str) -> None:
        self.history.set_reaction(msg_id, peer_id, code)
        self._emit(EventType.REACTION, (msg_id, peer_id, code))

    def _on_transfer(self, t: Transfer) -> None:
        # В историю — только смена состояния (не каждый шаг прогресса).
        if self._saved_transfer_state.get(t.transfer_id) != t.state.name:
            self._saved_transfer_state[t.transfer_id] = t.state.name
            self.history.upsert_transfer(t)
        self._emit(EventType.TRANSFER_UPDATED, t)

    def _resolve_peer(self, node_id: str, ip: str) -> tuple[str, str]:
        peer = self.registry.get(node_id)
        if peer is not None:
            return peer.nickname, peer.ip
        return "неизвестный", ip

    def _on_offer(self, offer: FileOffer) -> bool:
        if self.config.auto_accept:
            return True
        self._emit(EventType.FILE_OFFER, offer)
        accepted = offer.wait(OFFER_DECISION_TIMEOUT)
        if not accepted:
            log.info("Файл «%s» от %s не принят", offer.transfer.name, offer.transfer.peer_nick)
        return accepted
