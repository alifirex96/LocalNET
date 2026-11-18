"""Ядро LocalNET: связывает discovery, сообщения и файлы; общается с UI через очередь событий."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from . import APP_NAME, __version__
from .bans import Ban, BanList, load_trusted_key
from .config import Config
from .discovery import Discovery, PeerRegistry
from .file_transfer import ACCEPT_TIMEOUT, FileTransferManager
from .history import HistoryStore, history_path_for
from .messaging import ACK_TIMEOUT, Messenger
from .models import ALL_CHAT, REACTIONS, AppEvent, ChatMessage, EventType, FileOffer, Peer, Transfer
from .utils import app_dir, guess_local_ip, local_ident, user_config_dir

log = logging.getLogger(__name__)

# Получатель должен ответить раньше, чем у отправителя истечёт ACCEPT_TIMEOUT.
OFFER_DECISION_TIMEOUT = ACCEPT_TIMEOUT - 20
OUTBOX_RETRY_INTERVAL = ACK_TIMEOUT + 3


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
        self.ident = local_ident()
        # Блокировки: доверяем только списку, подписанному ключом администратора.
        dirs = [user_config_dir(), app_dir()]
        if config.path:
            dirs.insert(0, Path(config.path).parent)
        self.admin_key = load_trusted_key(config.admin_public_key, dirs)
        self.banlist = BanList()
        self._bans_lock = threading.Lock()
        stored = self.history.load_bans()
        if stored:
            bl = BanList.from_wire(stored)
            if bl is not None and bl.verify(self.admin_key):
                self.banlist = bl
        if self.admin_key:
            log.info("Ключ администратора: %s…, блокировок в списке: %d", self.admin_key[:12], len(self.banlist.bans))
        self._saved_transfer_state: dict[str, str] = {}
        self.events: "queue.Queue[AppEvent]" = queue.Queue()
        self.registry = PeerRegistry()
        self.local_ip = guess_local_ip()
        self._nick_lock = threading.Lock()
        self._nickname = config.nickname
        self._started = False
        self._outbox_lock = threading.RLock()
        self._outbox = {msg.msg_id: (msg, 0.0) for msg in self.history.pending_messages(self.node_id)}
        self._outbox_stop = threading.Event()

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
            on_delete=self._on_delete,
            on_bans=self._on_bans,
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
            ident=self.ident,
            get_ban_version=lambda: self.banlist.version,
            get_avatar=lambda: self.config.avatar,
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
        self._outbox_stop.clear()
        self._outbox_thread = threading.Thread(target=self._run_outbox, name="message-outbox", daemon=True)
        self._outbox_thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        log.info("Остановка…")
        self._outbox_stop.set()
        self._outbox_thread.join()
        if self.discovery is not None:
            self.discovery.stop()
        self.files.stop()
        self.messenger.stop()
        now = time.time()
        for peer in self.registry.online():
            self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, now, peer.ident)
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

    # ------------------------------------------------------------------ блокировки

    def ban_of(self, node_id: str, ident: str = "") -> Optional[Ban]:
        """Действующая блокировка пользователя (по ID узла или отпечатку), если есть."""
        if not ident:
            peer = self.registry.get(node_id)
            ident = peer.ident if peer else self.history.contact_ident(node_id)
        with self._bans_lock:
            return self.banlist.find(node_id, ident)

    def is_banned(self, node_id: str, ident: str = "") -> bool:
        return self.ban_of(node_id, ident) is not None

    def my_ban(self) -> Optional[Ban]:
        with self._bans_lock:
            return self.banlist.find(self.node_id, self.ident)

    def _check_can_send(self, to: Optional[str]) -> None:
        ban = self.my_ban()
        if ban is not None:
            raise ValueError(f"Вы заблокированы администратором {ban.describe()}"
                             + (f": {ban.reason}" if ban.reason else ""))
        if to is not None and self.is_banned(to):
            raise ValueError("Пользователь заблокирован администратором")

    def _on_bans(self, sender: str, obj: dict) -> None:
        bl = BanList.from_wire(obj)
        if bl is None or not bl.verify(self.admin_key):
            log.debug("Список блокировок от %s отклонён: неверная подпись или не задан ключ администратора",
                      sender[:8])
            return
        with self._bans_lock:
            if bl.version <= self.banlist.version:
                return
            self.banlist = bl
        self.history.save_bans(bl.to_wire())
        log.info("Получен список блокировок версии %d (%d записей)", bl.version, len(bl.bans))
        if self.discovery is not None:
            self.discovery.announce_now()
        # Передаём дальше тем, у кого версия старее.
        self._share_bans([p for p in self.registry.online() if p.ban_version < bl.version and p.node_id != sender])
        self._emit(EventType.BANS_UPDATED, bl)

    def _share_bans(self, peers: list[Peer]) -> None:
        with self._bans_lock:
            bl = self.banlist
        if bl.version and peers:
            self.messenger.send_bans(bl.to_wire(), [p.node_id for p in peers])

    # ------------------------------------------------------------------ удаление

    def delete_messages(self, msg_ids: list[str], for_all: bool = False) -> list[str]:
        """Удалить сообщения у себя; ``for_all`` — свои сообщения ещё и у собеседников."""
        with self._outbox_lock:
            convs = {m: self.history.message_conv(m) for m in msg_ids}
            own = [m for m in msg_ids if self.history.message_sender(m) == self.node_id]
            deleted = self.history.delete_messages(list(msg_ids))
            for msg_id in msg_ids:
                if self._outbox.pop(msg_id, None) is not None and msg_id not in deleted:
                    deleted.append(msg_id)
        if for_all and own:
            by_conv: dict[str, list[str]] = {}
            for m in own:
                by_conv.setdefault(convs.get(m) or ALL_CHAT, []).append(m)
            for conv, ids in by_conv.items():
                targets = [p.node_id for p in self.registry.online()] if conv == ALL_CHAT else [conv]
                self.messenger.send_delete(ids, targets)
        return deleted

    def delete_conversation(self, conv: str) -> None:
        with self._outbox_lock:
            self.history.delete_conversation(conv)
            self._outbox = {msg_id: pending for msg_id, pending in self._outbox.items() if pending[0].to != conv}

    def _on_delete(self, sender: str, msg_ids: list) -> None:
        # Удалить можно только свои сообщения: проверяем автора по истории.
        deleted = self.history.delete_messages(list(msg_ids), only_sender=sender)
        if deleted:
            self._emit(EventType.MESSAGES_DELETED, deleted)

    # ------------------------------------------------------------------ отправка

    def send_message(self, text: str, to: Optional[str] = None) -> ChatMessage:
        self._check_can_send(to)
        msg = self.messenger.prepare_message(text, to)
        if to is None:
            self.history.add_message(msg, len(self.registry.online()))
            self.messenger.submit_message(msg)
            return msg
        msg.queued = True
        with self._outbox_lock:
            saved = self.history.add_message(msg)
            if saved is None and self.history.enabled:
                raise ValueError("Не удалось сохранить сообщение в очереди")
            self._outbox[msg.msg_id] = (msg, 0.0)
        return msg

    def _run_outbox(self) -> None:
        while not self._outbox_stop.is_set():
            now = time.monotonic()
            ready = []
            with self._outbox_lock:
                for msg_id, (msg, retry_at) in self._outbox.items():
                    if retry_at > now:
                        continue
                    peer = self.registry.get(msg.to)
                    if peer is not None and peer.online:
                        self._outbox[msg_id] = (msg, now + OUTBOX_RETRY_INTERVAL)
                        ready.append(msg)
            for msg in ready:
                if self._outbox_stop.is_set():
                    return
                with self._outbox_lock:
                    pending = self._outbox.get(msg.msg_id)
                    if pending is None or pending[0] is not msg:
                        continue
                    if self.my_ban() is None and not self.is_banned(msg.to):
                        self.messenger.submit_message(msg)
            self._outbox_stop.wait(0.5)

    def clear_history(self) -> None:
        with self._outbox_lock:
            if not self.history.clear():
                raise ValueError("Не удалось очистить историю переписки")
            self._outbox.clear()

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

    def send_file(self, peer_id: str, path: str, *, group: bool = False) -> Transfer:
        self._check_can_send(peer_id)
        peer = self.registry.get(peer_id)
        if peer is None:
            raise ValueError("Пир не найден")
        if not peer.online:
            raise ValueError(f"{peer.nickname} сейчас offline")
        return self.files.send_file(peer, path, group=group)

    def cancel_transfer(self, transfer_id: str) -> bool:
        return self.files.cancel(transfer_id)

    # ------------------------------------------------------------------ #

    def _emit(self, etype: EventType, data=None) -> None:
        self.events.put(AppEvent(etype, data))

    def _on_peer_offline(self, peer: Peer) -> None:
        self.messenger.forget_peer(peer.node_id)
        self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, peer.last_seen, peer.ident)
        self._emit(EventType.PEER_OFFLINE, peer)

    def _on_peer_updated(self, peer: Peer) -> None:
        # Тот же человек под другим ID узла (другая копия программы, переустановка):
        # переносим переписку в один диалог, чтобы не было дублей.
        online_ids = {p.node_id for p in self.registry.online()}
        for old in self.history.find_duplicates(peer.node_id, peer.ident, peer.nickname, peer.ip):
            if old in online_ids:
                continue  # два экземпляра одновременно в сети — это не дубль
            with self._outbox_lock:
                self.history.merge_contact(old, peer.node_id)
                for msg_id, (msg, _) in self._outbox.items():
                    if msg.to == old:
                        self._outbox[msg_id] = (replace(msg, to=peer.node_id), 0.0)
            self._emit(EventType.PEER_MERGED, (old, peer.node_id))
        self.history.upsert_contact(peer.node_id, peer.nickname, peer.ip, peer.last_seen, peer.ident)
        if peer.ban_version < self.banlist.version:
            self._share_bans([peer])
        self._emit(EventType.PEER_UPDATED, peer)

    def _on_message(self, msg: ChatMessage) -> None:
        if self.is_banned(msg.sender_id):
            log.info("Сообщение от заблокированного пользователя %s отброшено", msg.sender_nick)
            return
        if self.history.add_message(msg) is not False:
            self._emit(EventType.MESSAGE, msg)

    def _on_receipt(self, msg_id: str, peer_id: str, kind: str, etype: EventType) -> None:
        if not self._record_receipt(msg_id, peer_id, kind):
            return
        self._emit(etype, (msg_id, peer_id))

    def _record_receipt(self, msg_id: str, peer_id: str, kind: str) -> bool:
        with self._outbox_lock:
            pending = self._outbox.get(msg_id)
            queued = pending is not None and pending[0].to == peer_id
            if queued and kind == "failed":
                return False
            if queued:
                if self.history.confirm_queued(msg_id, peer_id, kind):
                    del self._outbox[msg_id]
            else:
                self.history.add_receipt(msg_id, peer_id, kind)
        return True

    def _on_read(self, peer_id: str, msg_ids: list) -> None:
        for msg_id in msg_ids:
            self._record_receipt(msg_id, peer_id, "read")
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
        if self.is_banned(offer.transfer.peer_id):
            log.info("Файл от заблокированного пользователя %s отклонён", offer.transfer.peer_nick)
            return False
        if self.config.auto_accept:
            return True
        self._emit(EventType.FILE_OFFER, offer)
        accepted = offer.wait(OFFER_DECISION_TIMEOUT)
        if not accepted:
            log.info("Файл «%s» от %s не принят", offer.transfer.name, offer.transfer.peer_nick)
        return accepted
