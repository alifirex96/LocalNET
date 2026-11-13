"""Графический интерфейс LocalNET на tkinter.

Вся сеть работает в фоновых потоках ядра; GUI каждые 100 мс забирает события из
``core.events`` в главном потоке — интерфейс никогда не блокируется сетью/диском.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

from . import APP_NAME, __version__
from .app import LocalNetCore
from .models import ChatMessage, EventType, FileOffer, Peer, Transfer, TransferDirection, TransferState
from .utils import human_size, human_speed

ALL_CHAT = "__all__"
POLL_MS = 100
MAX_LINES_PER_CHAT = 1000


@dataclass
class _ChatEntry:
    msg: ChatMessage
    targets: int = 1
    delivered: set = field(default_factory=set)
    failed: set = field(default_factory=set)

    def status(self) -> str:
        if not self.msg.outgoing:
            return ""
        if self.msg.to is None:
            if self.targets == 0:
                return "  (никого нет в сети)"
            mark = f"  ✓ {len(self.delivered)}/{self.targets}"
            return mark + (f", не доставлено: {len(self.failed)}" if self.failed else "")
        if self.delivered:
            return "  ✓"
        if self.failed:
            return "  ✗ не доставлено"
        return "  …"


class LocalNetGui:
    def __init__(self, core: LocalNetCore) -> None:
        self.core = core
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("1000x700")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.current_chat = ALL_CHAT
        self.chats: dict[str, list[_ChatEntry]] = {ALL_CHAT: []}
        self.entries_by_id: dict[str, _ChatEntry] = {}
        self.unread: dict[str, int] = {}
        self.transfers: dict[str, Transfer] = {}

        self._build_ui()

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names() and sys.platform != "win32":
            style.theme_use("clam")

        # Верхняя панель
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill=tk.X)
        self.nick_var = tk.StringVar()
        ttk.Label(top, text="Ник:").pack(side=tk.LEFT)
        ttk.Label(top, textvariable=self.nick_var, font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Изменить…", command=self.change_nick).pack(side=tk.LEFT, padx=4)
        # Кнопки справа пакуются раньше подписи, чтобы не вытесняться при узком окне.
        ttk.Button(top, text="Папка загрузок…", command=self.choose_download_dir).pack(side=tk.RIGHT)
        ttk.Button(top, text="Открыть загрузки", command=self.open_download_dir).pack(side=tk.RIGHT, padx=4)
        self.info_var = tk.StringVar()
        ttk.Label(top, textvariable=self.info_var, foreground="#555").pack(side=tk.LEFT, padx=12)

        vpaned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        vpaned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        hpaned = ttk.PanedWindow(vpaned, orient=tk.HORIZONTAL)
        vpaned.add(hpaned, weight=3)

        # Список пиров
        left = ttk.Frame(hpaned)
        hpaned.add(left, weight=2)
        ttk.Label(left, text="Пиры в сети").pack(anchor=tk.W)
        cols = ("ip", "status", "seen")
        self.peer_tree = ttk.Treeview(left, columns=cols, selectmode="browse", height=12)
        self.peer_tree.heading("#0", text="Ник")
        self.peer_tree.heading("ip", text="IP")
        self.peer_tree.heading("status", text="Статус")
        self.peer_tree.heading("seen", text="Был")
        self.peer_tree.column("#0", width=130, minwidth=80)
        self.peer_tree.column("ip", width=95, minwidth=60)
        self.peer_tree.column("status", width=60, minwidth=40, anchor=tk.CENTER)
        self.peer_tree.column("seen", width=75, minwidth=40, anchor=tk.CENTER)
        self.peer_tree.tag_configure("offline", foreground="#999")
        self.peer_tree.tag_configure("unread", font=("TkDefaultFont", 9, "bold"))
        self.peer_tree.insert("", 0, iid=ALL_CHAT, text="💬 Общий чат", values=("", "", ""))
        self.peer_tree.pack(fill=tk.BOTH, expand=True)
        self.peer_tree.bind("<<TreeviewSelect>>", self._on_peer_select)
        self.peer_tree.selection_set(ALL_CHAT)

        # Чат
        right = ttk.Frame(hpaned)
        hpaned.add(right, weight=3)
        self.chat_title = tk.StringVar(value="Общий чат")
        ttk.Label(right, textvariable=self.chat_title, font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        text_frame = ttk.Frame(right)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.chat_text = tk.Text(text_frame, wrap=tk.WORD, state=tk.DISABLED, height=15,
                                 font=("TkDefaultFont", 10), padx=6, pady=4)
        scroll = ttk.Scrollbar(text_frame, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.chat_text.tag_configure("time", foreground="#888")
        self.chat_text.tag_configure("me", foreground="#1a5fb4", font=("TkDefaultFont", 10, "bold"))
        self.chat_text.tag_configure("them", foreground="#26a269", font=("TkDefaultFont", 10, "bold"))
        self.chat_text.tag_configure("status", foreground="#888")
        self.chat_text.tag_configure("system", foreground="#a51d2d")

        entry_frame = ttk.Frame(right)
        entry_frame.pack(fill=tk.X, pady=(4, 0))
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(entry_frame, textvariable=self.msg_var)
        self.msg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.msg_entry.bind("<Return>", lambda _e: self.send_message())
        ttk.Button(entry_frame, text="Отправить", command=self.send_message).pack(side=tk.LEFT, padx=4)
        ttk.Button(entry_frame, text="Файл…", command=self.send_file).pack(side=tk.LEFT)

        # Передачи
        bottom = ttk.Frame(vpaned)
        vpaned.add(bottom, weight=1)
        bar = ttk.Frame(bottom)
        bar.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(bar, text="Передачи файлов").pack(side=tk.LEFT)
        ttk.Button(bar, text="Очистить завершённые", command=self.clear_finished).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Открыть", command=self.open_selected_transfer).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bar, text="Отменить", command=self.cancel_transfer).pack(side=tk.RIGHT)
        tcols = ("dir", "peer", "size", "progress", "speed", "state")
        self.transfer_tree = ttk.Treeview(bottom, columns=tcols, selectmode="browse", height=5)
        for col, title, width in (
            ("#0", "Файл", 240), ("dir", "", 30), ("peer", "Пир", 130), ("size", "Размер", 80),
            ("progress", "Прогресс", 70), ("speed", "Скорость", 90), ("state", "Состояние", 200),
        ):
            self.transfer_tree.heading(col, text=title)
            self.transfer_tree.column(col, width=width, anchor=tk.W if col in ("#0", "state", "peer") else tk.CENTER)
        self.transfer_tree.pack(fill=tk.BOTH, expand=True)
        self.transfer_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_progressbar())
        self.progress = ttk.Progressbar(bottom, maximum=1000)
        self.progress.pack(fill=tk.X, pady=(2, 0))

        self.status_var = tk.StringVar(value="Запуск…")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W,
                  padding=(6, 2)).pack(fill=tk.X, side=tk.BOTTOM)

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        try:
            self.core.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Не удалось запустить сетевую часть:\n{exc}")
            self.root.destroy()
            return 1
        self.nick_var.set(self.core.get_nickname())
        self.info_var.set(f"IP {self.core.local_ip} · TCP {self.core.messenger.port}/{self.core.files.port}"
                          f" · UDP {self.core.config.discovery_port}")
        self.status_var.set(f"Загрузки: {self.core.config.download_path}")
        self.msg_entry.focus_set()
        self.root.after(POLL_MS, self._poll)
        self.root.after(1000, self._refresh_peers_periodic)
        self.root.mainloop()
        return 0

    def on_close(self) -> None:
        active = [t for t in self.transfers.values() if not t.state.finished]
        if active and not messagebox.askyesno(
                APP_NAME, f"Идёт передач: {len(active)}. Прервать их и выйти?"):
            return
        self.status_var.set("Завершение…")
        self.root.update_idletasks()
        try:
            self.core.stop()
        finally:
            self.root.destroy()

    # ------------------------------------------------------------------ #
    # Обработка событий ядра
    # ------------------------------------------------------------------ #

    def _poll(self) -> None:
        try:
            for _ in range(500):
                try:
                    ev = self.core.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(ev.type, ev.data)
                except Exception as exc:  # noqa: BLE001 - ошибка отрисовки не должна ронять GUI
                    import logging
                    logging.getLogger(__name__).exception("Ошибка обработки события %s: %s", ev.type, exc)
        finally:
            self.root.after(POLL_MS, self._poll)

    def _handle_event(self, etype: EventType, data) -> None:
        if etype in (EventType.PEER_UPDATED, EventType.PEER_OFFLINE):
            self._upsert_peer(data)
            if etype == EventType.PEER_OFFLINE:
                self._system_line(data.node_id, f"{data.nickname} вышел из сети")
        elif etype == EventType.PEER_REMOVED:
            if self.peer_tree.exists(data.node_id):
                self.peer_tree.delete(data.node_id)
        elif etype == EventType.MESSAGE:
            self._add_message(data)
        elif etype in (EventType.MESSAGE_DELIVERED, EventType.MESSAGE_FAILED):
            msg_id, peer_id = data
            entry = self.entries_by_id.get(msg_id)
            if entry is not None:
                (entry.delivered if etype == EventType.MESSAGE_DELIVERED else entry.failed).add(peer_id)
                key = ALL_CHAT if entry.msg.to is None else entry.msg.to
                if key == self.current_chat:
                    self._render_chat()
        elif etype == EventType.TRANSFER_UPDATED:
            self._upsert_transfer(data)
        elif etype == EventType.FILE_OFFER:
            self._ask_offer(data)
        elif etype == EventType.ERROR:
            self.status_var.set(str(data))
            messagebox.showwarning(APP_NAME, str(data))
        elif etype == EventType.INFO:
            self.status_var.set(str(data))

    # --------------------------- пиры --------------------------------- #

    def _upsert_peer(self, peer: Peer) -> None:
        name = peer.nickname
        unread = self.unread.get(peer.node_id, 0)
        if unread:
            name += f"  ({unread})"
        values = (peer.ip, "online" if peer.online else "offline", self._seen_text(peer))
        tags = ("offline",) if not peer.online else (("unread",) if unread else ())
        if self.peer_tree.exists(peer.node_id):
            self.peer_tree.item(peer.node_id, text=name, values=values, tags=tags)
        else:
            self.peer_tree.insert("", tk.END, iid=peer.node_id, text=name, values=values, tags=tags)
        if peer.node_id == self.current_chat:
            self.chat_title.set(self._chat_title(peer.node_id))

    @staticmethod
    def _seen_text(peer: Peer) -> str:
        if peer.online and time.time() - peer.last_seen < 5:
            return "сейчас"
        return datetime.fromtimestamp(peer.last_seen).strftime("%H:%M:%S")

    def _refresh_peers_periodic(self) -> None:
        for peer in self.core.registry.all():
            self._upsert_peer(peer)
        online = len(self.core.registry.online())
        total_unread = self.unread.get(ALL_CHAT, 0)
        self.peer_tree.item(ALL_CHAT, text="💬 Общий чат" + (f"  ({total_unread})" if total_unread else ""),
                            values=("", f"{online} в сети", ""))
        self.root.after(1000, self._refresh_peers_periodic)

    def _on_peer_select(self, _event=None) -> None:
        sel = self.peer_tree.selection()
        if not sel:
            return
        self.current_chat = sel[0]
        self.unread.pop(self.current_chat, None)
        self.chat_title.set(self._chat_title(self.current_chat))
        peer = self.core.registry.get(self.current_chat)
        if peer is not None:
            self._upsert_peer(peer)
        self._render_chat()

    def _chat_title(self, key: str) -> str:
        if key == ALL_CHAT:
            return "Общий чат (сообщение получат все online-пиры)"
        peer = self.core.registry.get(key)
        if peer is None:
            return "Личный чат"
        return f"Личный чат: {peer.nickname} ({peer.ip}) — {'online' if peer.online else 'offline'}"

    # --------------------------- чат ---------------------------------- #

    def send_message(self) -> None:
        text = self.msg_var.get().strip()
        if not text:
            return
        to = None if self.current_chat == ALL_CHAT else self.current_chat
        if to is not None:
            peer = self.core.registry.get(to)
            if peer is None or not peer.online:
                messagebox.showinfo(APP_NAME, "Пир сейчас не в сети — сообщение не будет доставлено.")
                return
        targets = len(self.core.registry.online()) if to is None else 1
        try:
            msg = self.core.send_message(text, to)
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return
        self.msg_var.set("")
        self._add_message(msg, targets=targets)

    def _add_message(self, msg: ChatMessage, targets: int = 1) -> None:
        key = ALL_CHAT if msg.to is None else (msg.to if msg.outgoing else msg.sender_id)
        entry = _ChatEntry(msg, targets=targets)
        chat = self.chats.setdefault(key, [])
        chat.append(entry)
        if len(chat) > MAX_LINES_PER_CHAT:
            removed = chat.pop(0)
            self.entries_by_id.pop(removed.msg.msg_id, None)
        self.entries_by_id[msg.msg_id] = entry
        if key == self.current_chat:
            self._append_entry(entry)
        elif not msg.outgoing:
            self.unread[key] = self.unread.get(key, 0) + 1
            peer = self.core.registry.get(key)
            if peer is not None:
                self._upsert_peer(peer)
            if msg.is_private:
                self.status_var.set(f"Новое личное сообщение от {msg.sender_nick}")
                self.root.bell()

    def _system_line(self, key: str, text: str) -> None:
        if key == self.current_chat:
            self.chat_text.configure(state=tk.NORMAL)
            self.chat_text.insert(tk.END, f"— {text} —\n", "system")
            self.chat_text.configure(state=tk.DISABLED)
            self.chat_text.see(tk.END)

    def _render_chat(self) -> None:
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.configure(state=tk.DISABLED)
        for entry in self.chats.get(self.current_chat, []):
            self._append_entry(entry)

    def _append_entry(self, entry: _ChatEntry) -> None:
        m = entry.msg
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, datetime.fromtimestamp(m.ts).strftime("[%H:%M:%S] "), "time")
        nick = "Я" if m.outgoing else m.sender_nick
        self.chat_text.insert(tk.END, f"{nick}: ", "me" if m.outgoing else "them")
        self.chat_text.insert(tk.END, m.text)
        self.chat_text.insert(tk.END, entry.status() + "\n", "status")
        self.chat_text.configure(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def change_nick(self) -> None:
        new = simpledialog.askstring(APP_NAME, "Новый ник:", initialvalue=self.core.get_nickname(),
                                     parent=self.root)
        if new and new.strip():
            try:
                self.core.set_nickname(new)
            except ValueError as exc:
                messagebox.showwarning(APP_NAME, str(exc))
                return
            self.nick_var.set(self.core.get_nickname())

    # -------------------------- файлы --------------------------------- #

    def send_file(self) -> None:
        if self.current_chat == ALL_CHAT:
            messagebox.showinfo(APP_NAME, "Выберите получателя в списке пиров слева.")
            return
        peer = self.core.registry.get(self.current_chat)
        if peer is None or not peer.online:
            messagebox.showinfo(APP_NAME, "Пир сейчас не в сети.")
            return
        paths = filedialog.askopenfilenames(parent=self.root, title=f"Файлы для {peer.nickname}")
        for path in paths:
            try:
                self.core.send_file(peer.node_id, path)
            except (OSError, ValueError) as exc:
                messagebox.showerror(APP_NAME, f"{os.path.basename(path)}: {exc}")

    def _ask_offer(self, offer: FileOffer) -> None:
        t = offer.transfer
        if t.cancel_event.is_set():
            offer.resolve(False)
            return
        self.root.deiconify()
        self.root.lift()
        accepted = messagebox.askyesno(
            APP_NAME,
            f"{t.peer_nick} ({t.peer_ip}) хочет отправить файл:\n\n«{t.name}»\n"
            f"Размер: {human_size(t.size)}\n\nСохранить в {self.core.config.download_path}?",
            parent=self.root,
        )
        offer.resolve(accepted)

    def _upsert_transfer(self, t: Transfer) -> None:
        self.transfers[t.transfer_id] = t
        arrow = "↑" if t.direction == TransferDirection.SEND else "↓"
        state = t.state.value
        if t.state == TransferState.DONE and t.direction == TransferDirection.RECEIVE:
            state = "готово, хеш совпал"
        elif t.error:
            state = f"{state}: {t.error}"
        speed = human_speed(t.speed) if t.state in (TransferState.TRANSFERRING, TransferState.HASHING) and t.speed else ""
        if t.state == TransferState.DONE and t.speed:
            speed = f"≈{human_speed(t.speed)}"  # средняя скорость самой передачи (без хеширования)
        values = (arrow, t.peer_nick, human_size(t.size), f"{t.progress * 100:.1f}%", speed, state)
        if self.transfer_tree.exists(t.transfer_id):
            self.transfer_tree.item(t.transfer_id, values=values)
        else:
            self.transfer_tree.insert("", 0, iid=t.transfer_id, text=t.name, values=values)
            self.transfer_tree.selection_set(t.transfer_id)
        self._update_progressbar()
        if t.state == TransferState.DONE and t.direction == TransferDirection.RECEIVE:
            self.status_var.set(f"Получен файл «{t.name}» от {t.peer_nick}: {t.path}")
        elif t.state == TransferState.FAILED:
            self.status_var.set(f"Ошибка передачи «{t.name}»: {t.error}")

    def _selected_transfer(self) -> Optional[Transfer]:
        sel = self.transfer_tree.selection()
        return self.transfers.get(sel[0]) if sel else None

    def _update_progressbar(self) -> None:
        t = self._selected_transfer()
        self.progress["value"] = int((t.progress if t else 0) * 1000)

    def cancel_transfer(self) -> None:
        t = self._selected_transfer()
        if t is None or t.state.finished:
            return
        self.core.cancel_transfer(t.transfer_id)

    def clear_finished(self) -> None:
        for tid, t in list(self.transfers.items()):
            if t.state.finished:
                del self.transfers[tid]
                if self.transfer_tree.exists(tid):
                    self.transfer_tree.delete(tid)
        self._update_progressbar()

    def open_selected_transfer(self) -> None:
        t = self._selected_transfer()
        if t is None:
            return
        if t.direction == TransferDirection.RECEIVE and t.state == TransferState.DONE and t.path:
            _open_path(os.path.dirname(t.path))
        elif t.direction == TransferDirection.SEND and t.path:
            _open_path(os.path.dirname(t.path))

    def choose_download_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.root, initialdir=str(self.core.config.download_path),
                                       title="Папка для входящих файлов")
        if path:
            self.core.set_download_dir(path)
            self.status_var.set(f"Загрузки: {self.core.config.download_path}")

    def open_download_dir(self) -> None:
        path = self.core.config.download_path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        _open_path(str(path))


def _open_path(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"Не удалось открыть {path}: {exc}")
