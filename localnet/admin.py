"""Админ-панель LocalNET: блокировка и разблокировка пользователей.

Запуск: ``LocalNET-Admin`` (рядом с программой), ``localnet --admin`` или
``python -m localnet.admin``.

Как это работает без сервера:

* при первом запуске создаётся пара ключей администратора (Ed25519). Закрытый ключ
  хранится только здесь: ``<профиль>/localnet/admin_key.json``;
* клиенты принимают списки блокировок, только подписанные этим ключом. Открытый ключ
  раздаётся клиентам файлом ``localnet-admin.pub`` (кнопка «Сохранить ключ для
  клиентов») — его кладут рядом с программой или в папку настроек пользователя;
  либо его вписывают в настройки (``admin_public_key``);
* панель слушает анонсы присутствия (сама в сети не видна) и рассылает подписанный
  список всем, у кого он старее; клиенты передают его друг другу дальше.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

try:  # окно нужно только AdminApp; логика (AdminCore) работает и без tkinter
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]

from . import APP_NAME, __version__, ed25519
from .bans import PUB_FILE_NAME, Ban, BanList, load_or_create_admin_secret
from .config import Config, load_config
from .discovery import Discovery, PeerRegistry
from .messaging import Messenger
from .models import Peer
from .utils import user_config_dir

log = logging.getLogger(__name__)

KEY_FILE = "admin_key.json"
STATE_FILE = "admin_state.json"
REPUBLISH_INTERVAL = 5.0

DURATIONS = [
    ("15 минут", 15 * 60),
    ("1 час", 3600),
    ("1 день", 86400),
    ("7 дней", 7 * 86400),
    ("30 дней", 30 * 86400),
    ("Навсегда", 0),
]


class AdminCore:
    """Логика админ-панели без интерфейса (удобно тестировать)."""

    def __init__(self, config: Config, data_dir: Optional[Path] = None) -> None:
        self.config = config
        self.data_dir = data_dir or user_config_dir()
        self.secret = load_or_create_admin_secret(self.data_dir / KEY_FILE)
        self.public = ed25519.public_key(self.secret).hex()
        self.state_path = self.data_dir / STATE_FILE
        self.seen: dict[str, dict] = {}
        self.banlist = BanList()
        self._lock = threading.RLock()
        self._load_state()

        self.node_id = "admin" + uuid.uuid4().hex[:27]
        self.registry = PeerRegistry()
        self.messenger = Messenger(self.node_id, lambda: "admin", self.registry, 0, secret=config.secret,
                                   transport=config.transport)
        self.discovery: Optional[Discovery] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_publish = (0.0, 0)  # (время, сколько узлам)

    # ------------------------------------------------------------------ состояние

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.seen = data.get("seen", {}) if isinstance(data.get("seen"), dict) else {}
        bl = BanList.from_wire({"list": {"version": data.get("version", 0), "bans": data.get("bans", [])},
                                "pub": self.public, "esig": "00"}) if data else None
        if bl is not None:
            self.banlist = bl
            self.banlist.sign(self.secret)

    def _save_state(self) -> None:
        data = {"version": self.banlist.version, "bans": self.banlist.payload()["bans"], "seen": self.seen}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ сеть

    def start(self) -> None:
        self.messenger.start()
        self.discovery = Discovery(
            node_id=self.node_id, get_nickname=lambda: "admin", msg_port=self.messenger.port, file_port=1,
            registry=self.registry, discovery_port=self.config.discovery_port,
            broadcast_addresses=self.config.broadcast_addresses, multicast_group=self.config.multicast_group,
            static_peers=self.config.static_peers, secret=self.config.secret,
            interval=self.config.announce_interval, timeout=self.config.peer_timeout,
            on_peer_updated=self._on_peer, announce=False,
        )
        self.discovery.start()
        self._thread = threading.Thread(target=self._loop, name="admin-publish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.discovery is not None:
            self.discovery.stop()
        self.messenger.stop()
        with self._lock:
            self._save_state()

    def _on_peer(self, peer: Peer) -> None:
        with self._lock:
            # Тот же человек под новым ID узла — старую запись убираем (как и клиенты).
            if peer.ident:
                online = {p.node_id for p in self.registry.online()}
                for nid, info in list(self.seen.items()):
                    if nid != peer.node_id and nid not in online and info.get("ident") == peer.ident:
                        del self.seen[nid]
            self.seen[peer.node_id] = {"nick": peer.nickname, "ip": peer.ip, "ident": peer.ident,
                                       "last_seen": peer.last_seen}

    def _loop(self) -> None:
        while not self._stop.wait(REPUBLISH_INTERVAL):
            try:
                self.publish(only_outdated=True)
                with self._lock:
                    self._save_state()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка рассылки списка блокировок")

    def publish(self, only_outdated: bool = False) -> int:
        """Отправить подписанный список узлам в сети (всем или только со старой версией)."""
        with self._lock:
            if not self.banlist.version:
                return 0
            wire = self.banlist.to_wire()
            version = self.banlist.version
        peers = [p for p in self.registry.online() if not only_outdated or p.ban_version < version]
        if peers:
            self.messenger.send_bans(wire, [p.node_id for p in peers])
            self.last_publish = (time.time(), len(peers))
        return len(peers)

    # ------------------------------------------------------------------ блокировки

    def _commit(self, bans: list[Ban]) -> int:
        with self._lock:
            version = max(self.banlist.version + 1, int(time.time() * 1000))
            self.banlist = BanList(version=version, bans=bans)
            self.banlist.sign(self.secret)
            self._save_state()
        return self.publish()

    def ban(self, node_id: str, seconds: int, reason: str = "") -> int:
        info = self.user_info(node_id)
        until = 0.0 if seconds == 0 else time.time() + seconds
        with self._lock:
            bans = [b for b in self.banlist.bans if b.node_id != node_id]
            bans.append(Ban(node_id=node_id, ident=info.get("ident", ""), nick=info.get("nick", ""), until=until,
                            reason=reason.strip()[:300]))
        return self._commit(bans)

    def unban(self, node_id: str) -> int:
        with self._lock:
            bans = [b for b in self.banlist.bans if b.node_id != node_id]
        return self._commit(bans)

    def clear_expired(self) -> int:
        with self._lock:
            bans = [b for b in self.banlist.bans if b.active()]
            if len(bans) == len(self.banlist.bans):
                return 0
        return self._commit(bans)

    def ban_of(self, node_id: str) -> Optional[Ban]:
        with self._lock:
            return next((b for b in self.banlist.bans if b.node_id == node_id), None)

    def user_info(self, node_id: str) -> dict:
        peer = self.registry.get(node_id)
        if peer is not None:
            return {"nick": peer.nickname, "ip": peer.ip, "ident": peer.ident, "last_seen": peer.last_seen,
                    "online": peer.online}
        with self._lock:
            info = dict(self.seen.get(node_id, {}))
            ban = self.ban_of(node_id)
        if not info and ban is not None:
            info = {"nick": ban.nick, "ip": "", "ident": ban.ident, "last_seen": 0}
        info["online"] = False
        return info

    def users(self) -> list[tuple[str, dict]]:
        with self._lock:
            ids = set(self.seen) | {b.node_id for b in self.banlist.bans}
        ids |= {p.node_id for p in self.registry.all()}
        rows = [(nid, self.user_info(nid)) for nid in ids]
        rows.sort(key=lambda r: (not r[1].get("online"), str(r[1].get("nick", "")).lower()))
        return rows

    # ------------------------------------------------------------------ ключ для клиентов

    def export_public_key(self, path: Path) -> None:
        path.write_text(self.public + "\n", encoding="utf-8")

    def trust_locally(self) -> Optional[Path]:
        """Чтобы клиент LocalNET на этом же компьютере сразу доверял администратору."""
        target = user_config_dir() / PUB_FILE_NAME
        try:
            if not target.exists() or target.read_text(encoding="utf-8").strip() != self.public:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.export_public_key(target)
            return target
        except OSError:
            return None


# =========================================================================== #
# Окно
# =========================================================================== #

class AdminApp:
    def __init__(self, core: AdminCore) -> None:
        self.core = core
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} — администрирование")
        self.root.geometry("980x620")
        self.root.minsize(760, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=30)
        style.configure("Accent.TButton", foreground="#ffffff", background="#1fa3ec")
        style.map("Accent.TButton", background=[("active", "#1386cf")])
        style.configure("Danger.TButton", foreground="#ffffff", background="#e5534b")
        style.map("Danger.TButton", background=[("active", "#c8433c")])

        head = tk.Frame(self.root, bg="#2563d9", padx=16, pady=12)
        head.pack(fill=tk.X)
        tk.Label(head, text="Администрирование LocalNET", bg="#2563d9", fg="#ffffff",
                 font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        self.key_var = tk.StringVar()
        tk.Label(head, textvariable=self.key_var, bg="#2563d9", fg="#d7e8fb").pack(anchor="w")

        bar = ttk.Frame(self.root, padding=(12, 10))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Заблокировать…", style="Danger.TButton", command=self.ban).pack(side=tk.LEFT)
        ttk.Button(bar, text="Разблокировать", style="Accent.TButton", command=self.unban).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Убрать истёкшие", command=self.clear_expired).pack(side=tk.LEFT)
        ttk.Button(bar, text="Разослать заново", command=self.republish).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text="Сохранить ключ для клиентов…", command=self.export_key).pack(side=tk.RIGHT)
        ttk.Button(bar, text="Копировать ключ", command=self.copy_key).pack(side=tk.RIGHT, padx=6)

        self.filter_var = tk.StringVar()
        frow = ttk.Frame(self.root, padding=(12, 0))
        frow.pack(fill=tk.X)
        ttk.Label(frow, text="Поиск:").pack(side=tk.LEFT)
        ttk.Entry(frow, textvariable=self.filter_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.filter_var.trace_add("write", lambda *_a: self.refresh())

        cols = ("ip", "online", "ban", "reason")
        self.tree = ttk.Treeview(self.root, columns=cols, selectmode="browse")
        for col, title, width in (("#0", "Пользователь", 240), ("ip", "IP", 120), ("online", "В сети", 140),
                                  ("ban", "Блокировка", 190), ("reason", "Причина", 240)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=tk.W)
        self.tree.tag_configure("banned", foreground="#c8433c")
        self.tree.tag_configure("expired", foreground="#8a97a3")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self.tree.bind("<Double-1>", lambda _e: self.ban())

        self.status = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status, padding=(12, 4), foreground="#5d7486").pack(fill=tk.X)

    # ------------------------------------------------------------------ жизненный цикл

    def run(self) -> int:
        try:
            self.core.start()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Не удалось открыть сетевые порты: {exc}")
            return 1
        local = self.core.trust_locally()
        key = self.core.public
        self.key_var.set(f"Ключ администратора: {key[:8]}…{key[-8:]}"
                         + ("  ·  клиент LocalNET на этом компьютере доверяет ключу" if local else ""))
        self.refresh()
        self.root.after(2000, self._tick)
        self.root.mainloop()
        return 0

    def close(self) -> None:
        try:
            self.core.stop()
        finally:
            self.root.destroy()

    def _tick(self) -> None:
        self.refresh()
        self.root.after(2000, self._tick)

    # ------------------------------------------------------------------ список

    def refresh(self) -> None:
        q = self.filter_var.get().strip().lower()
        selected = self._selected()
        self.tree.delete(*self.tree.get_children())
        now = time.time()
        for nid, info in self.core.users():
            nick = str(info.get("nick") or nid[:8])
            ip = str(info.get("ip") or "")
            if q and q not in nick.lower() and q not in ip:
                continue
            ban = self.core.ban_of(nid)
            if info.get("online"):
                online = "в сети"
            elif info.get("last_seen"):
                online = "был(а) " + time.strftime("%d.%m %H:%M", time.localtime(info["last_seen"]))
            else:
                online = "—"
            if ban is None:
                bantext, reason, tags = "—", "", ()
            elif ban.active(now):
                bantext, reason, tags = ban.describe(), ban.reason, ("banned",)
            else:
                bantext, reason, tags = "истекла", ban.reason, ("expired",)
            self.tree.insert("", tk.END, iid=nid, text=nick, values=(ip, online, bantext, reason), tags=tags)
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        bl = self.core.banlist
        active = sum(1 for b in bl.bans if b.active(now))
        when, count = self.core.last_publish
        sent = f", последняя рассылка: {time.strftime('%H:%M:%S', time.localtime(when))} ({count} узлам)" if when else ""
        ver = time.strftime("%d.%m %H:%M:%S", time.localtime(bl.version / 1000)) if bl.version else "—"
        self.status.set(f"В сети: {len(self.core.registry.online())} · заблокировано: {active} · "
                        f"список от {ver}{sent}")

    def _selected(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    # ------------------------------------------------------------------ действия

    def ban(self) -> None:
        nid = self._selected()
        if nid is None:
            messagebox.showinfo(APP_NAME, "Выберите пользователя в списке", parent=self.root)
            return
        info = self.core.user_info(nid)
        dialog = BanDialog(self.root, str(info.get("nick") or nid[:8]))
        if dialog.result is None:
            return
        seconds, reason = dialog.result
        sent = self.core.ban(nid, seconds, reason)
        self.status.set(f"Блокировка выдана, отправлено {sent} узлам в сети")
        self.refresh()

    def unban(self) -> None:
        nid = self._selected()
        if nid is None or self.core.ban_of(nid) is None:
            messagebox.showinfo(APP_NAME, "Выберите заблокированного пользователя", parent=self.root)
            return
        sent = self.core.unban(nid)
        self.status.set(f"Пользователь разблокирован, отправлено {sent} узлам в сети")
        self.refresh()

    def clear_expired(self) -> None:
        self.core.clear_expired()
        self.refresh()

    def republish(self) -> None:
        sent = self.core.publish()
        self.status.set(f"Список блокировок отправлен {sent} узлам в сети")

    def copy_key(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.core.public)
        self.status.set("Открытый ключ администратора скопирован")

    def export_key(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.root, initialfile=PUB_FILE_NAME,
                                            title="Файл ключа для клиентов",
                                            filetypes=[("Ключ LocalNET", "*.pub"), ("Все файлы", "*")])
        if not path:
            return
        self.core.export_public_key(Path(path))
        messagebox.showinfo(APP_NAME, "Ключ сохранён.\n\nПоложите этот файл на каждом компьютере рядом с "
                                      "программой LocalNET или в папку настроек пользователя "
                                      "(Linux: ~/.config/localnet, Windows: %APPDATA%\\LocalNET) и "
                                      "перезапустите LocalNET. Без ключа клиенты блокировки не применяют.",
                            parent=self.root)


class BanDialog:
    def __init__(self, parent: tk.Tk, nick: str) -> None:
        self.result: Optional[tuple[int, str]] = None
        top = self.top = tk.Toplevel(parent)
        top.title("Блокировка")
        top.transient(parent)
        top.resizable(False, False)
        frm = ttk.Frame(top, padding=16)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=f"Заблокировать «{nick}»", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.duration = tk.IntVar(value=3600)
        for i, (label, secs) in enumerate(DURATIONS):
            ttk.Radiobutton(frm, text=label, value=secs, variable=self.duration).grid(
                row=1 + i // 3, column=i % 3, sticky="w", padx=(0, 16), pady=2)
        ttk.Radiobutton(frm, text="Другое, часов:", value=-1, variable=self.duration).grid(
            row=3, column=0, sticky="w", pady=2)
        self.hours = tk.StringVar(value="2")
        ttk.Entry(frm, textvariable=self.hours, width=8).grid(row=3, column=1, sticky="w")
        ttk.Label(frm, text="Причина (увидит пользователь):").grid(row=4, column=0, columnspan=3, sticky="w",
                                                                    pady=(10, 2))
        self.reason = tk.StringVar()
        ttk.Entry(frm, textvariable=self.reason, width=50).grid(row=5, column=0, columnspan=3, sticky="we")
        buttons = ttk.Frame(frm)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Отмена", command=top.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Заблокировать", style="Danger.TButton", command=self._ok).pack(side=tk.RIGHT,
                                                                                                padx=6)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.bind("<Return>", lambda _e: self._ok())
        top.update_idletasks()
        top.after(100, lambda: _safe_grab(top))
        parent.wait_window(top)

    def _ok(self) -> None:
        secs = self.duration.get()
        if secs == -1:
            try:
                hours = float(self.hours.get().replace(",", "."))
                if hours <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(APP_NAME, "Укажите число часов больше нуля", parent=self.top)
                return
            secs = int(hours * 3600)
        self.result = (secs, self.reason.get())
        self.top.destroy()


def _safe_grab(top: tk.Toplevel) -> None:
    try:
        if top.winfo_exists() and top.winfo_viewable():
            top.grab_set()
    except tk.TclError:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    from .app import setup_logging

    p = argparse.ArgumentParser(prog="localnet-admin", description=f"{APP_NAME} — администрирование")
    p.add_argument("--config", help="файл настроек LocalNET (порты, общий секрет)")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = p.parse_args(argv)
    config = load_config(args.config)
    setup_logging(config.log_level, user_config_dir() / "logs-admin")
    if tk is None:
        print("Для админ-панели нужен tkinter (пакет python3-tk)", file=sys.stderr)
        return 2
    try:
        return AdminApp(AdminCore(config)).run()
    except tk.TclError as exc:
        print(f"Не удалось открыть окно: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
