"""Консольный интерфейс LocalNET — для отладки и серверов без графики.

Команды::

    текст                 — сообщение в общий чат
    /peers                — список пиров
    /msg <пир> <текст>    — личное сообщение (пир: ник, IP или начало node_id)
    /send <пир> <путь>    — отправить файл
    /transfers            — список передач
    /cancel <id>          — отменить передачу (достаточно начала id)
    /accept <id> | /decline <id> — решение по входящему файлу
    /nick <новый ник>     — сменить ник
    /help, /quit
"""

from __future__ import annotations

import queue
import shlex
import sys
import threading
import time
from datetime import datetime

from .app import LocalNetCore
from .models import EventType, FileOffer, TransferState
from .utils import human_size, human_speed


class CliApp:
    def __init__(self, core: LocalNetCore) -> None:
        self.core = core
        self.offers: dict[str, FileOffer] = {}
        self._stop = threading.Event()
        self._last_progress: dict[str, float] = {}

    def run(self) -> int:
        self.core.start()
        print(f"LocalNET CLI. Ник: {self.core.get_nickname()}, IP: {self.core.local_ip}, "
              f"сообщения TCP {self.core.messenger.port}, файлы TCP {self.core.files.port}. "
              f"Загрузки: {self.core.config.download_path}. /help — справка.")
        printer = threading.Thread(target=self._event_loop, name="cli-events", daemon=True)
        printer.start()
        try:
            for line in sys.stdin:
                if not self._handle(line.rstrip("\n")):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.core.stop()
        return 0

    # ------------------------------------------------------------------ #

    def _handle(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self._send(line, None)
            return True
        try:
            parts = shlex.split(line, posix=True)
        except ValueError:
            parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd in ("/quit", "/exit", "/q"):
                return False
            if cmd == "/help":
                print(__doc__)
            elif cmd == "/peers":
                self._print_peers()
            elif cmd == "/msg" and len(args) >= 2:
                peer = self._peer(args[0])
                if peer:
                    text = line.split(None, 2)[2] if len(line.split(None, 2)) > 2 else " ".join(args[1:])
                    self._send(text, peer.node_id)
            elif cmd == "/send" and len(args) >= 2:
                peer = self._peer(args[0])
                if peer:
                    t = self.core.send_file(peer.node_id, " ".join(args[1:]))
                    print(f"→ передача {t.transfer_id[:8]}: «{t.name}» ({human_size(t.size)}) → {peer.nickname}")
            elif cmd == "/transfers":
                for t in self.core.files.all():
                    print(f"  {t.transfer_id[:8]} {t.direction.value:7} {t.name} {human_size(t.size)} "
                          f"{t.progress * 100:5.1f}% {t.state.value} {t.error}")
            elif cmd == "/cancel" and args:
                tid = self._match(args[0], [t.transfer_id for t in self.core.files.all()])
                print("Отменено" if tid and self.core.cancel_transfer(tid) else "Передача не найдена")
            elif cmd in ("/accept", "/decline") and args:
                tid = self._match(args[0], list(self.offers))
                offer = self.offers.pop(tid, None) if tid else None
                if offer is None:
                    print("Нет такого предложения")
                else:
                    offer.resolve(cmd == "/accept")
            elif cmd == "/nick" and args:
                self.core.set_nickname(" ".join(args))
                print(f"Ник изменён: {self.core.get_nickname()}")
            else:
                print("Неизвестная команда или не хватает аргументов. /help — справка.")
        except (ValueError, OSError) as exc:
            print(f"Ошибка: {exc}")
        return True

    def _send(self, text: str, to) -> None:
        try:
            self.core.send_message(text, to)
        except ValueError as exc:
            print(f"Ошибка: {exc}")

    def _peer(self, query: str):
        peer = self.core.registry.find(query)
        if peer is None:
            print(f"Пир «{query}» не найден. /peers — список.")
        return peer

    @staticmethod
    def _match(prefix: str, ids: list[str]):
        found = [i for i in ids if i.startswith(prefix)]
        return found[0] if len(found) == 1 else None

    def _print_peers(self) -> None:
        peers = sorted(self.core.registry.all(), key=lambda p: (not p.online, p.nickname.lower()))
        if not peers:
            print("  (пиров пока нет)")
        for p in peers:
            seen = datetime.fromtimestamp(p.last_seen).strftime("%H:%M:%S")
            print(f"  {'●' if p.online else '○'} {p.nickname:24} {p.ip:15} "
                  f"{'online ' if p.online else 'offline'} {seen}  {p.node_id[:8]}")

    # ------------------------------------------------------------------ #

    def _event_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self.core.events.get(timeout=0.2)
            except queue.Empty:
                continue
            d = ev.data
            ts = time.strftime("%H:%M:%S")
            if ev.type == EventType.PEER_UPDATED:
                print(f"[{ts}] + {d.nickname} ({d.ip}) online")
            elif ev.type == EventType.PEER_OFFLINE:
                print(f"[{ts}] - {d.nickname} ({d.ip}) offline")
            elif ev.type == EventType.MESSAGE:
                kind = "ЛС " if d.is_private else ""
                print(f"[{ts}] {kind}<{d.sender_nick}> {d.text}")
            elif ev.type == EventType.MESSAGE_FAILED:
                peer = self.core.registry.get(d[1])
                print(f"[{ts}] ! сообщение не доставлено: {peer.nickname if peer else d[1][:8]}")
            elif ev.type == EventType.FILE_OFFER:
                t = d.transfer
                self.offers[t.transfer_id] = d
                print(f"[{ts}] ? {t.peer_nick} предлагает файл «{t.name}» ({human_size(t.size)}). "
                      f"/accept {t.transfer_id[:8]} или /decline {t.transfer_id[:8]}")
            elif ev.type == EventType.TRANSFER_UPDATED:
                self._print_transfer(d, ts)
            elif ev.type in (EventType.ERROR, EventType.INFO):
                print(f"[{ts}] {d}")

    def _print_transfer(self, t, ts: str) -> None:
        if t.state.finished:
            extra = f" → {t.path}" if t.state == TransferState.DONE and t.path and t.direction.value == "receive" else ""
            print(f"[{ts}] {t.transfer_id[:8]} «{t.name}»: {t.state.value} {t.error}{extra}")
            return
        now = time.monotonic()
        if now - self._last_progress.get(t.transfer_id, 0) < 2:
            return
        self._last_progress[t.transfer_id] = now
        print(f"[{ts}] {t.transfer_id[:8]} «{t.name}»: {t.state.value} {t.progress * 100:5.1f}% "
              f"{human_speed(t.speed) if t.speed else ''}")
