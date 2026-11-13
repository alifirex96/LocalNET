"""Графический интерфейс LocalNET на tkinter в стиле современного корпоративного мессенджера.

Синяя шапка, панель разделов слева, список чатов с аватарами, переписка «пузырями»
на голубом фоне, передачи файлов — карточками прямо в переписке.

Вся сеть работает в фоновых потоках ядра; GUI каждые 100 мс забирает события из
``core.events`` в главном потоке — интерфейс никогда не блокируется сетью/диском.
Используется только стандартный tkinter (Canvas), без сторонних библиотек.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox
from tkinter import font as tkfont
from typing import Callable, Optional

from . import APP_NAME, __version__
from .app import LocalNetCore
from .models import ChatMessage, EventType, FileOffer, Peer, Transfer, TransferDirection, TransferState
from .utils import human_size, human_speed

log = logging.getLogger(__name__)

ALL_CHAT = "__all__"
FILES_VIEW = "__files__"
POLL_MS = 100
MAX_ITEMS_PER_CHAT = 1000


# --------------------------------------------------------------------------- #
# Оформление
# --------------------------------------------------------------------------- #

class C:
    """Палитра."""

    TOP_LEFT = "#2563d9"       # градиент шапки
    TOP_RIGHT = "#34b0f0"
    BG = "#2a6fdc"             # фон под рабочей панелью
    RAIL = "#2a6fdc"
    RAIL_ACTIVE = "#4f8ff0"
    PANEL = "#ffffff"
    LINE = "#edf0f4"
    SEARCH_BG = "#f1f4f8"
    TEXT = "#1f2933"
    MUTED = "#828b95"
    SELECTED = "#1ea2e6"
    ACCENT = "#1fa3ec"
    ACCENT_DARK = "#1386cf"
    LINK = "#1a8ad8"
    CHAT_BG = "#d3e7f6"
    BUBBLE_IN = "#ffffff"
    BUBBLE_OUT = "#e4f6c8"
    PILL = "#9db8cc"
    ONLINE = "#45c46b"
    OK = "#4bb34b"
    ERROR = "#e5534b"
    TRACK = "#e1e9f0"
    BADGE = "#1fa3ec"
    TOAST = "#2f3e4c"


AVATAR_COLORS = ["#55b6e8", "#7bbf3a", "#f0954a", "#9b6bdb", "#e2615f", "#2bb3a3", "#d9a521", "#5c7cfa",
                 "#e05d9d", "#3f9fd8"]

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря"]

# Иконка окна (48×48 PNG): три соединённых узла сети.
ICON_PNG_B64 = (
    "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"
)


def avatar_color(key: str) -> str:
    return AVATAR_COLORS[zlib.crc32(key.encode("utf-8")) % len(AVATAR_COLORS)]


def initials(name: str) -> str:
    words = [w for w in name.replace("@", " ").replace("_", " ").replace(".", " ").split() if w]
    letters = "".join(w[0] for w in words[:2]).upper()
    return letters or "?"


_NON_BMP_OK = True  # уточняется при создании окна по версии Tk


def tk_safe(text: str) -> str:
    """Старые Tk (< 8.6.10) не умеют символы вне BMP (эмодзи) — заменяем, чтобы не было ошибок."""
    if _NON_BMP_OK:
        return text
    return "".join(ch if ord(ch) <= 0xFFFF else "\N{REPLACEMENT CHARACTER}" for ch in text)


def _detect_non_bmp(root: tk.Tk) -> None:
    global _NON_BMP_OK
    try:
        parts = [int(x) for x in str(root.tk.call("info", "patchlevel")).split(".")[:3]]
        _NON_BMP_OK = tuple(parts) >= (8, 6, 10)
    except (tk.TclError, ValueError):
        _NON_BMP_OK = False


def day_label(ts: float) -> str:
    d = datetime.fromtimestamp(ts).date()
    today = date.today()
    if d == today:
        return "Сегодня"
    if d == today - timedelta(days=1):
        return "Вчера"
    label = f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"
    return label if d.year == today.year else f"{label} {d.year}"


def short_time(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    if d.date() == date.today():
        return d.strftime("%H:%M")
    return f"{d.day} {MONTHS_GEN[d.month - 1][:3]}"


def rounded_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float, r: float, **kw) -> int:
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    kw.setdefault("outline", "")
    return cv.create_polygon(pts, smooth=True, **kw)


def fit(text: str, fnt: tkfont.Font, width: int) -> str:
    """Обрезает строку с многоточием под заданную ширину в пикселях."""
    text = text.replace("\n", " ")
    if fnt.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fnt.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


class Fonts:
    def __init__(self, root: tk.Tk) -> None:
        families = set(tkfont.families(root))
        family = next((f for f in ("Segoe UI", "PT Astra Sans", "Noto Sans", "Open Sans", "Ubuntu",
                                   "Cantarell", "DejaVu Sans", "Liberation Sans") if f in families),
                      tkfont.nametofont("TkDefaultFont").actual("family"))
        mk = lambda size, weight="normal": tkfont.Font(root=root, family=family, size=size, weight=weight)  # noqa: E731
        self.family = family
        self.brand = mk(15, "bold")
        self.top = mk(10)
        self.top_bold = mk(10, "bold")
        self.title = mk(13, "bold")
        self.name = mk(11, "bold")
        self.text = mk(11)
        self.small = mk(9)
        self.small_bold = mk(9, "bold")
        self.preview = mk(10)
        self.avatar = mk(13, "bold")
        self.avatar_small = mk(10, "bold")
        self.icon = mk(16, "bold")


# --------------------------------------------------------------------------- #
# Модель переписки в GUI
# --------------------------------------------------------------------------- #

@dataclass
class _ChatEntry:
    msg: ChatMessage
    targets: int = 1
    delivered: set = field(default_factory=set)
    failed: set = field(default_factory=set)

    def status(self) -> tuple[str, str]:
        """(текст статуса, цвет) для исходящих."""
        if not self.msg.outgoing:
            return "", C.MUTED
        if self.msg.to is None:
            if self.targets == 0:
                return "никого нет в сети", C.MUTED
            text = f"✓✓ {len(self.delivered)}/{self.targets}" if self.delivered else f"✓ 0/{self.targets}"
            if self.failed:
                return text + f" · не доставлено: {len(self.failed)}", C.ERROR
            return text, C.ACCENT if self.delivered else C.MUTED
        if self.delivered:
            return "✓✓", C.ACCENT
        if self.failed:
            return "✗ не доставлено", C.ERROR
        return "✓", C.MUTED


@dataclass
class _Item:
    kind: str                     # msg | transfer | system
    ts: float
    entry: Optional[_ChatEntry] = None
    tid: str = ""
    text: str = ""


# --------------------------------------------------------------------------- #
# Главное окно
# --------------------------------------------------------------------------- #

class LocalNetGui:
    def __init__(self, core: LocalNetCore) -> None:
        self.core = core
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {__version__}")
        self.root.geometry("1180x760")
        self.root.minsize(860, 560)
        self.root.configure(bg=C.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            self._icon = tk.PhotoImage(data=ICON_PNG_B64)
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self.f = Fonts(self.root)
        _detect_non_bmp(self.root)

        self.current = ALL_CHAT
        self.items: dict[str, list[_Item]] = {ALL_CHAT: []}
        self.entries_by_id: dict[str, _ChatEntry] = {}
        self.unread: dict[str, int] = {}
        self.preview: dict[str, tuple[float, str]] = {}
        self.transfers: dict[str, Transfer] = {}
        self.offers: dict[str, FileOffer] = {}
        self.search_query = ""

        self._cards: dict[str, dict] = {}        # tid -> элементы карточки на холсте
        self._card_state: dict[str, TransferState] = {}
        self._chat_redraw_pending = False
        self._list_redraw_pending = False
        self._toast_after: Optional[str] = None
        self._top_info = ""

        self._build()

    # ------------------------------------------------------------------ #
    # Построение
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        self.top = tk.Canvas(self.root, height=58, highlightthickness=0, bd=0, bg=C.TOP_LEFT)
        self.top.pack(fill=tk.X, side=tk.TOP)
        self.top.bind("<Configure>", lambda _e: self._draw_top())

        body = tk.Frame(self.root, bg=C.BG)
        body.pack(fill=tk.BOTH, expand=True)

        self.rail = tk.Canvas(body, width=72, highlightthickness=0, bd=0, bg=C.RAIL)
        self.rail.pack(side=tk.LEFT, fill=tk.Y)
        self.rail.bind("<Configure>", lambda _e: self._draw_rail())

        panel = tk.Frame(body, bg=C.PANEL)
        panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10), pady=(0, 10))

        paned = tk.PanedWindow(panel, orient=tk.HORIZONTAL, sashwidth=1, bg=C.LINE, bd=0, sashrelief=tk.FLAT)
        paned.pack(fill=tk.BOTH, expand=True)

        # --- левая колонка: поиск + список чатов
        left = tk.Frame(paned, bg=C.PANEL)
        paned.add(left, minsize=260, width=340)
        search = tk.Canvas(left, height=64, highlightthickness=0, bd=0, bg=C.PANEL)
        search.pack(fill=tk.X)
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(search, textvariable=self.search_var, relief=tk.FLAT, bd=0,
                                     bg=C.SEARCH_BG, fg=C.TEXT, font=self.f.text, insertbackground=C.TEXT,
                                     highlightthickness=0)
        self._placeholder(self.search_entry, self.search_var, "Найти пира или чат")
        self.search_var.trace_add("write", lambda *_a: self._on_search())
        search.bind("<Configure>", lambda e: self._draw_search(search, e.width))
        tk.Frame(left, height=1, bg=C.LINE).pack(fill=tk.X)
        self.list = tk.Canvas(left, highlightthickness=0, bd=0, bg=C.PANEL)
        self.list.pack(fill=tk.BOTH, expand=True)
        self.list.bind("<Configure>", lambda _e: self._schedule_list())

        # --- правая колонка: заголовок, переписка, ввод
        right = tk.Frame(paned, bg=C.CHAT_BG)
        paned.add(right, minsize=420)
        self.header = tk.Canvas(right, height=72, highlightthickness=0, bd=0, bg=C.PANEL)
        self.header.pack(fill=tk.X)
        self.header.bind("<Configure>", lambda _e: self._draw_header())
        tk.Frame(right, height=1, bg=C.LINE).pack(fill=tk.X)

        self.chat_frame = tk.Frame(right, bg=C.CHAT_BG)
        self.chat_frame.pack(fill=tk.BOTH, expand=True)
        self.chat = tk.Canvas(self.chat_frame, highlightthickness=0, bd=0, bg=C.CHAT_BG)
        self.chat.pack(fill=tk.BOTH, expand=True)
        self.chat.bind("<Configure>", lambda _e: self._schedule_chat())

        self.input = tk.Canvas(right, height=76, highlightthickness=0, bd=0, bg=C.CHAT_BG)
        self.input.pack(fill=tk.X)
        self.msg_var = tk.StringVar()
        self.msg_entry = tk.Entry(self.input, textvariable=self.msg_var, relief=tk.FLAT, bd=0, bg="#ffffff",
                                  fg=C.TEXT, font=self.f.text, insertbackground=C.TEXT, highlightthickness=0)
        self._placeholder(self.msg_entry, self.msg_var, "Напишите сообщение…")
        self.msg_entry.bind("<Return>", lambda _e: self.send_message())
        self.input.bind("<Configure>", lambda _e: self._draw_input())

        self.toast = tk.Label(self.chat_frame, bg=C.TOAST, fg="#ffffff", font=self.f.preview, padx=14, pady=6)

        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(seq, self._on_wheel, add="+")

    # ---------------------------------------------------------- placeholder

    def _placeholder(self, entry: tk.Entry, var: tk.StringVar, text: str) -> None:
        entry._ph_active = False  # type: ignore[attr-defined]

        def show(_e=None):
            if not var.get():
                entry._ph_active = True  # type: ignore[attr-defined]
                entry.configure(fg=C.MUTED)
                var.set(text)

        def hide(_e=None):
            if entry._ph_active:  # type: ignore[attr-defined]
                entry._ph_active = False  # type: ignore[attr-defined]
                var.set("")
                entry.configure(fg=C.TEXT)

        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", show, add="+")
        show()

    @staticmethod
    def _entry_value(entry: tk.Entry, var: tk.StringVar) -> str:
        return "" if getattr(entry, "_ph_active", False) else var.get()

    # ---------------------------------------------------------------- шапка

    def _draw_top(self) -> None:
        cv, f = self.top, self.f
        cv.delete("all")
        w, h = max(cv.winfo_width(), 1), int(cv["height"])
        a, b = _hex(C.TOP_LEFT), _hex(C.TOP_RIGHT)
        steps = 64
        for i in range(steps):
            t = i / (steps - 1)
            color = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))
            cv.create_rectangle(w * i / steps, 0, w * (i + 1) / steps + 1, h, fill=color, outline="")
        # логотип: три соединённых узла
        x, y = 30, h / 2
        pts = [(x - 10, y + 7), (x + 10, y + 7), (x, y - 10)]
        for p in pts:
            for q in pts:
                cv.create_line(*p, *q, fill="#ffffff", width=2)
        for px, py in pts:
            cv.create_oval(px - 5, py - 5, px + 5, py + 5, fill="#ffffff", outline="")
        cv.create_text(52, h / 2, text=APP_NAME, font=f.brand, fill="#ffffff", anchor="w")
        cv.create_text(52 + f.brand.measure(APP_NAME) + 18, h / 2, text=self._top_info, font=f.top,
                       fill="#d7e8fb", anchor="w")

        # справа: кнопки-«пилюли» и свой аватар
        x = w - 16
        nick = self.core.get_nickname()
        r = 17
        cv.create_oval(x - 2 * r, h / 2 - r, x, h / 2 + r, fill="#ffffff", outline="", tags=("me",))
        cv.create_oval(x - 2 * r + 2, h / 2 - r + 2, x - 2, h / 2 + r - 2, fill=avatar_color(nick), outline="",
                       tags=("me",))
        cv.create_text(x - r, h / 2, text=initials(tk_safe(nick)), font=f.avatar_small, fill="#ffffff",
                       tags=("me",))
        self._clickable(cv, "me", self.open_settings)
        x -= 2 * r + 12
        for tag, label, cb in (("pill_settings", "Настройки", self.open_settings),
                               ("pill_open", "Загрузки", self.open_download_dir),
                               ("pill_nick", fit(tk_safe(nick), f.top_bold, 180), self.open_settings)):
            tw = f.top_bold.measure(label) + 28
            t = min(1.0, max(0.0, (x - tw / 2) / w))
            under = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))
            rounded_rect(cv, x - tw, h / 2 - 15, x, h / 2 + 15, 15, fill=under, outline="#d5e7fc", width=1,
                         tags=(tag,))
            cv.create_text(x - tw / 2, h / 2, text=label, font=f.top_bold, fill="#ffffff", tags=(tag,))
            self._clickable(cv, tag, cb)
            x -= tw + 10

    # ---------------------------------------------------------------- рейка

    def _draw_rail(self) -> None:
        cv = self.rail
        cv.delete("all")
        h = max(cv.winfo_height(), 1)
        sections = [(ALL_CHAT, 34, "Чаты", self._icon_chats), (FILES_VIEW, 100, "Файлы", self._icon_files)]
        for key, y, _title, drawer in sections:
            active = (key == FILES_VIEW) == (self.current == FILES_VIEW)
            tag = f"rail_{key}"
            if active:
                rounded_rect(cv, 10, y, 62, y + 52, 14, fill=C.RAIL_ACTIVE, tags=(tag,))
            else:
                cv.create_rectangle(10, y, 62, y + 52, fill=C.RAIL, outline="", tags=(tag,))
            drawer(cv, 36, y + 26, tag)
            self._clickable(cv, tag, lambda k=key: self.select(ALL_CHAT if k == ALL_CHAT else FILES_VIEW))
        total = sum(v for k, v in self.unread.items())
        if total:
            self._badge(cv, 56, 40, total, "#e85d5d")
        active = sum(1 for t in self.transfers.values() if not t.state.finished)
        if active:
            self._badge(cv, 56, 106, active, "#f2a33a")
        self._icon_gear(cv, 36, h - 34, "rail_settings")
        self._clickable(cv, "rail_settings", self.open_settings)

    def _icon_chats(self, cv: tk.Canvas, x: float, y: float, tag: str) -> None:
        rounded_rect(cv, x - 13, y - 11, x + 11, y + 7, 6, fill="", outline="#ffffff", width=2, tags=(tag,))
        cv.create_line(x - 7, y + 6, x - 10, y + 13, x - 1, y + 7, fill="#ffffff", width=2, tags=(tag,))
        cv.create_line(x + 5, y - 6, x + 15, y - 6, x + 15, y + 10, fill="#ffffff", width=2, tags=(tag,))

    def _icon_files(self, cv: tk.Canvas, x: float, y: float, tag: str) -> None:
        cv.create_line(x - 10, y - 13, x + 4, y - 13, x + 11, y - 6, x + 11, y + 13, x - 10, y + 13, x - 10, y - 13,
                       fill="#ffffff", width=2, joinstyle=tk.ROUND, tags=(tag,))
        cv.create_line(x, y - 5, x, y + 7, fill="#ffffff", width=2, tags=(tag,))
        cv.create_line(x - 5, y + 2, x, y + 7, x + 5, y + 2, fill="#ffffff", width=2, tags=(tag,))

    def _icon_gear(self, cv: tk.Canvas, x: float, y: float, tag: str) -> None:
        import math
        for i in range(8):
            a = i * math.pi / 4
            cv.create_line(x + 9 * math.cos(a), y + 9 * math.sin(a), x + 14 * math.cos(a), y + 14 * math.sin(a),
                           fill="#ffffff", width=4, capstyle=tk.ROUND, tags=(tag,))
        cv.create_oval(x - 10, y - 10, x + 10, y + 10, outline="#ffffff", width=3, fill=C.RAIL, tags=(tag,))
        cv.create_oval(x - 4, y - 4, x + 4, y + 4, outline="#ffffff", width=2, tags=(tag,))

    def _badge(self, cv: tk.Canvas, x: float, y: float, n: int, color: str) -> None:
        text = str(n) if n < 100 else "99+"
        w = max(18, self.f.small_bold.measure(text) + 10)
        rounded_rect(cv, x - w / 2, y - 9, x + w / 2, y + 9, 9, fill=color)
        cv.create_text(x, y, text=text, font=self.f.small_bold, fill="#ffffff")

    # ---------------------------------------------------------------- поиск

    def _draw_search(self, cv: tk.Canvas, width: int) -> None:
        cv.delete("bg")
        rounded_rect(cv, 14, 12, width - 14, 52, 20, fill=C.SEARCH_BG, tags=("bg",))
        cv.create_oval(30, 25, 42, 37, outline=C.MUTED, width=2, tags=("bg",))
        cv.create_line(40, 35, 46, 41, fill=C.MUTED, width=2, capstyle=tk.ROUND, tags=("bg",))
        cv.delete("win")
        cv.create_window(56, 32, window=self.search_entry, anchor="w", width=max(40, width - 84), tags=("win",))

    def _on_search(self) -> None:
        q = self._entry_value(self.search_entry, self.search_var).strip().lower()
        if q != self.search_query:
            self.search_query = q
            self._schedule_list()

    # ---------------------------------------------------------- список чатов

    def _schedule_list(self) -> None:
        if not self._list_redraw_pending:
            self._list_redraw_pending = True
            self.root.after_idle(self._draw_list)

    def _list_rows(self) -> list[tuple[str, str, Optional[Peer]]]:
        peers = self.core.registry.all()
        peers.sort(key=lambda p: (not p.online, -self.preview.get(p.node_id, (0, ""))[0], p.nickname.lower()))
        rows: list[tuple[str, str, Optional[Peer]]] = [(ALL_CHAT, "Общий чат", None)]
        rows += [(p.node_id, p.nickname, p) for p in peers]
        if self.search_query:
            rows = [r for r in rows if self.search_query in r[1].lower()
                    or (r[2] is not None and self.search_query in r[2].ip)]
        return rows

    def _draw_list(self) -> None:
        self._list_redraw_pending = False
        cv, f = self.list, self.f
        cv.delete("all")
        w = max(cv.winfo_width(), 200)
        row_h = 76
        rows = self._list_rows()
        y = 0
        for key, name, peer in rows:
            selected = key == self.current
            tag = f"row_{key}"
            bg = C.SELECTED if selected else C.PANEL
            cv.create_rectangle(0, y, w, y + row_h, fill=bg, outline="", tags=(tag,))
            fg = "#ffffff" if selected else C.TEXT
            muted = "#e3f3fd" if selected else C.MUTED
            if peer is not None and not peer.online and not selected:
                fg = C.MUTED
            # аватар
            cx, cy, r = 42, y + row_h / 2, 25
            if key == ALL_CHAT:
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#2fa8e6", outline="", tags=(tag,))
                cv.create_text(cx, cy, text="#", font=f.icon, fill="#ffffff", tags=(tag,))
            else:
                color = avatar_color(name) if (peer is None or peer.online) else "#b7c0c8"
                cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="", tags=(tag,))
                cv.create_text(cx, cy, text=initials(tk_safe(name)), font=f.avatar, fill="#ffffff", tags=(tag,))
                if peer is not None and peer.online:
                    cv.create_oval(cx + 11, cy + 11, cx + 25, cy + 25, fill=C.ONLINE, outline=bg, width=3,
                                   tags=(tag,))
            # время и непрочитанные
            ts, prev = self.preview.get(key, (0.0, ""))
            right = w - 16
            time_text = short_time(ts) if ts else ""
            tw = f.small.measure(time_text)
            cv.create_text(right, y + 22, text=time_text, font=f.small, fill=muted, anchor="e", tags=(tag,))
            unread = self.unread.get(key, 0)
            badge_w = 0
            if unread and not selected:
                text = str(unread) if unread < 100 else "99+"
                badge_w = max(22, f.small_bold.measure(text) + 12)
                rounded_rect(cv, right - badge_w, y + 42, right, y + 62, 10, fill=C.BADGE, tags=(tag,))
                cv.create_text(right - badge_w / 2, y + 52, text=text, font=f.small_bold, fill="#ffffff",
                               tags=(tag,))
            text_x = 80
            cv.create_text(text_x, y + 22, text=fit(tk_safe(name), f.name, right - tw - 12 - text_x),
                           font=f.name, fill=fg, anchor="w", tags=(tag,))
            if not prev:
                if key == ALL_CHAT:
                    online = len(self.core.registry.online())
                    prev = f"{online} в сети" if online else "пока никого нет в сети"
                elif peer is not None:
                    prev = f"{peer.ip} · " + ("в сети" if peer.online else f"был(а) в {_hm(peer.last_seen)}")
            cv.create_text(text_x, y + 51, text=fit(tk_safe(prev), f.preview, right - badge_w - 10 - text_x),
                           font=f.preview, fill=muted, anchor="w", tags=(tag,))
            if not selected:
                cv.create_line(text_x, y + row_h - 1, w, y + row_h - 1, fill=C.LINE, tags=(tag,))
            self._clickable(cv, tag, lambda k=key: self.select(k), cursor="")
            y += row_h
        if len(rows) == 1 and not self.search_query:
            cv.create_text(w / 2, y + 40, text="Ищем других участников в сети…\nОни появятся здесь автоматически.",
                           font=f.preview, fill=C.MUTED, justify=tk.CENTER)
        cv.configure(scrollregion=(0, 0, w, max(y, 1)))

    # ------------------------------------------------------------ заголовок

    def _draw_header(self) -> None:
        cv, f = self.header, self.f
        cv.delete("all")
        w = max(cv.winfo_width(), 300)
        cy, r = 36, 25
        key = self.current
        if key == FILES_VIEW:
            cv.create_oval(24, cy - r, 24 + 2 * r, cy + r, fill="#f0954a", outline="")
            self._icon_files(cv, 24 + r, cy, "hdr")
            title = "Передачи файлов"
            active = sum(1 for t in self.transfers.values() if not t.state.finished)
            sub = f"всего {len(self.transfers)}" + (f", активных {active}" if active else "")
        elif key == ALL_CHAT:
            cv.create_oval(24, cy - r, 24 + 2 * r, cy + r, fill="#2fa8e6", outline="")
            cv.create_text(24 + r, cy, text="#", font=f.icon, fill="#ffffff")
            title = "Общий чат"
            online = len(self.core.registry.online())
            sub = f"{online + 1} {_plural(online + 1, 'участник', 'участника', 'участников')} в сети"
        else:
            peer = self.core.registry.get(key)
            name = peer.nickname if peer else "Пир"
            cv.create_oval(24, cy - r, 24 + 2 * r, cy + r, fill=avatar_color(name), outline="")
            cv.create_text(24 + r, cy, text=initials(tk_safe(name)), font=f.avatar, fill="#ffffff")
            title = name
            if peer is None:
                sub = ""
            elif peer.online:
                sub = f"в сети · {peer.ip}"
            else:
                sub = f"не в сети, был(а) в {_hm(peer.last_seen)} · {peer.ip}"
        right_limit = w - 20
        if key not in (ALL_CHAT, FILES_VIEW):
            label = "Отправить файл"
            bw = f.top_bold.measure(label) + 52
            x2 = w - 20
            rounded_rect(cv, x2 - bw, cy - 17, x2, cy + 17, 17, fill=C.ACCENT, tags=("sendfile",))
            self._icon_plus(cv, x2 - bw + 20, cy, "sendfile", color="#ffffff", bg=C.ACCENT, small=True)
            cv.create_text(x2 - bw + 34, cy, text=label, font=f.top_bold, fill="#ffffff", anchor="w",
                           tags=("sendfile",))
            self._clickable(cv, "sendfile", self.send_file)
            right_limit = x2 - bw - 16
        cv.create_text(90, cy - 11, text=fit(tk_safe(title), f.title, right_limit - 90), font=f.title,
                       fill=C.TEXT, anchor="w")
        cv.create_text(90, cy + 13, text=fit(tk_safe(sub), f.preview, right_limit - 90), font=f.preview,
                       fill=C.MUTED, anchor="w")

    # ----------------------------------------------------------------- ввод

    def _draw_input(self) -> None:
        cv = self.input
        cv.delete("all")
        w = max(cv.winfo_width(), 300)
        x1, x2, y1, y2 = 18, w - 84, 14, 62
        rounded_rect(cv, x1, y1, x2, y2, 14, fill="#ffffff")
        self._icon_plus(cv, x1 + 24, (y1 + y2) / 2, "attach", color=C.MUTED, bg="#ffffff")
        self._clickable(cv, "attach", self.send_file)
        cv.create_window(x1 + 48, (y1 + y2) / 2, window=self.msg_entry, anchor="w", width=max(60, x2 - x1 - 62))
        # круглая кнопка отправки с «бумажным самолётиком»
        cx, cy, r = w - 46, (y1 + y2) / 2, 26
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=C.ACCENT, outline="", tags=("send",))
        cv.create_polygon(cx - 10, cy - 11, cx + 13, cy, cx - 10, cy + 11, cx - 6, cy, fill="#ffffff",
                          outline="", tags=("send",))
        self._clickable(cv, "send", self.send_message)

    def _icon_plus(self, cv: tk.Canvas, x: float, y: float, tag: str, color: str, bg: str,
                   small: bool = False) -> None:
        r = 8 if small else 11
        cv.create_oval(x - r, y - r, x + r, y + r, outline=color, width=2, fill=bg, tags=(tag,))
        d = r - 4
        cv.create_line(x - d, y, x + d, y, fill=color, width=2, tags=(tag,))
        cv.create_line(x, y - d, x, y + d, fill=color, width=2, tags=(tag,))

    # --------------------------------------------------------------- общее

    def _clickable(self, cv: tk.Canvas, tag: str, cb: Callable[[], None], cursor: str = "hand2") -> None:
        cv.tag_bind(tag, "<Button-1>", lambda _e: cb())
        if cursor:
            cv.tag_bind(tag, "<Enter>", lambda _e: cv.configure(cursor=cursor))
            cv.tag_bind(tag, "<Leave>", lambda _e: cv.configure(cursor=""))

    def _on_wheel(self, event) -> None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget not in (self.chat, self.list):
            widget = widget.master
        if widget is None:
            return
        if event.num == 4:
            delta = -3
        elif event.num == 5:
            delta = 3
        else:
            delta = -1 * int(event.delta / 40) if abs(event.delta) >= 40 else (-1 if event.delta > 0 else 1)
        top, bottom = widget.yview()
        if (delta < 0 and top <= 0) or (delta > 0 and bottom >= 1):
            return
        widget.yview_scroll(delta, "units")

    def show_toast(self, text: str, ms: int = 4000) -> None:
        self.toast.configure(text=tk_safe(text))
        self.toast.place(relx=0.5, rely=1.0, y=-12, anchor="s")
        self.toast.lift()
        if self._toast_after:
            self.root.after_cancel(self._toast_after)
        self._toast_after = self.root.after(ms, self.toast.place_forget)

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
        self._top_info = (f"IP {self.core.local_ip} · TCP {self.core.messenger.port}/{self.core.files.port}"
                          f" · UDP {self.core.config.discovery_port}")
        self._draw_top()
        self.msg_entry.focus_set()
        self.root.after(POLL_MS, self._poll)
        self.root.after(1000, self._periodic)
        self.root.mainloop()
        return 0

    def on_close(self) -> None:
        active = [t for t in self.transfers.values() if not t.state.finished]
        if active and not messagebox.askyesno(APP_NAME, f"Идёт передач: {len(active)}. Прервать их и выйти?"):
            return
        for offer in list(self.offers.values()):
            offer.resolve(False)
        try:
            self.core.stop()
        finally:
            self.root.destroy()

    def _periodic(self) -> None:
        self._schedule_list()
        self._draw_header()
        self.root.after(1000, self._periodic)

    # ------------------------------------------------------------------ #
    # События ядра
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
                except Exception:  # noqa: BLE001 - ошибка отрисовки не должна ронять GUI
                    log.exception("Ошибка обработки события %s", ev.type)
        finally:
            self.root.after(POLL_MS, self._poll)

    def _handle_event(self, etype: EventType, data) -> None:
        if etype == EventType.PEER_UPDATED:
            self._schedule_list()
        elif etype == EventType.PEER_OFFLINE:
            self._add_item(data.node_id, _Item("system", time.time(), text=f"{data.nickname} вышел из сети"),
                           preview=False)
            self._schedule_list()
        elif etype == EventType.PEER_REMOVED:
            self._schedule_list()
        elif etype == EventType.MESSAGE:
            self._add_message(data)
        elif etype in (EventType.MESSAGE_DELIVERED, EventType.MESSAGE_FAILED):
            msg_id, peer_id = data
            entry = self.entries_by_id.get(msg_id)
            if entry is not None:
                (entry.delivered if etype == EventType.MESSAGE_DELIVERED else entry.failed).add(peer_id)
                key = ALL_CHAT if entry.msg.to is None else entry.msg.to
                if key == self.current:
                    self._schedule_chat()
        elif etype == EventType.TRANSFER_UPDATED:
            self._on_transfer(data)
        elif etype == EventType.FILE_OFFER:
            self._on_offer(data)
        elif etype == EventType.ERROR:
            self.show_toast(str(data), 8000)
            messagebox.showwarning(APP_NAME, str(data))
        elif etype == EventType.INFO:
            self.show_toast(str(data))

    def _add_item(self, key: str, item: _Item, preview: bool = True, preview_text: str = "") -> None:
        lst = self.items.setdefault(key, [])
        lst.append(item)
        if len(lst) > MAX_ITEMS_PER_CHAT:
            removed = lst.pop(0)
            if removed.entry is not None:
                self.entries_by_id.pop(removed.entry.msg.msg_id, None)
        if preview:
            self.preview[key] = (item.ts, preview_text)
        if key == self.current:
            self._schedule_chat()
        self._schedule_list()

    def _add_message(self, msg: ChatMessage, targets: int = 1) -> None:
        key = ALL_CHAT if msg.to is None else (msg.to if msg.outgoing else msg.sender_id)
        entry = _ChatEntry(msg, targets=targets)
        self.entries_by_id[msg.msg_id] = entry
        if msg.outgoing:
            prev = f"Вы: {msg.text}"
        elif msg.to is None:
            prev = f"{msg.sender_nick}: {msg.text}"
        else:
            prev = msg.text
        self._add_item(key, _Item("msg", msg.ts, entry=entry), preview_text=prev)
        if not msg.outgoing and key != self.current:
            self.unread[key] = self.unread.get(key, 0) + 1
            self._draw_rail()
            if msg.is_private:
                self.root.bell()
                self.show_toast(f"Новое сообщение от {msg.sender_nick}")

    def _on_transfer(self, t: Transfer) -> None:
        first = t.transfer_id not in self.transfers
        self.transfers[t.transfer_id] = t
        if first:
            arrow = "↑" if t.direction == TransferDirection.SEND else "↓"
            self._add_item(t.peer_id, _Item("transfer", t.started, tid=t.transfer_id), preview_text=f"{arrow} {t.name}")
            self._draw_rail()
        if t.state != TransferState.WAITING_ACCEPT:
            self.offers.pop(t.transfer_id, None)
            prev = self.preview.get(t.peer_id)
            if prev and prev[1].startswith("↓ хочет отправить"):
                self.preview[t.peer_id] = (prev[0], f"↓ {t.name}")
                self._schedule_list()
        if t.state.finished and self._card_state.get(t.transfer_id) != t.state:
            if t.state == TransferState.DONE and t.direction == TransferDirection.RECEIVE:
                self.show_toast(f"Получен файл «{t.name}» от {t.peer_nick}")
            elif t.state == TransferState.FAILED:
                self.show_toast(f"Ошибка передачи «{t.name}»: {t.error}", 6000)
            self._draw_rail()
        visible = self.current in (FILES_VIEW, t.peer_id)
        if not visible:
            self._card_state[t.transfer_id] = t.state
            return
        card = self._cards.get(t.transfer_id)
        if card is not None and self._card_state.get(t.transfer_id) == t.state and not first:
            self._update_card(t, card)
        else:
            self._schedule_chat()
        if self.current == FILES_VIEW:
            self._draw_header()

    def _on_offer(self, offer: FileOffer) -> None:
        t = offer.transfer
        if t.cancel_event.is_set():
            offer.resolve(False)
            return
        self.offers[t.transfer_id] = offer
        key = t.peer_id
        if key != self.current:
            self.unread[key] = self.unread.get(key, 0) + 1
            self._draw_rail()
        self.preview[key] = (time.time(), f"↓ хочет отправить файл: {t.name}")
        self._schedule_list()
        if self.current in (key, FILES_VIEW):
            self._schedule_chat()
        self.root.bell()
        self.show_toast(f"{t.peer_nick} хочет отправить файл «{t.name}» ({human_size(t.size)})", 8000)
        try:
            self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # Переписка
    # ------------------------------------------------------------------ #

    def select(self, key: str) -> None:
        self.current = key
        self.unread.pop(key, None)
        self._draw_rail()
        self._draw_header()
        self._schedule_list()
        self._render_chat(stick=True)
        if key == FILES_VIEW:
            self.input.pack_forget()
        else:
            if not self.input.winfo_ismapped():
                self.input.pack(fill=tk.X)
            self.msg_entry.focus_set()

    def _schedule_chat(self) -> None:
        if not self._chat_redraw_pending:
            self._chat_redraw_pending = True
            self.root.after_idle(self._render_chat)

    def _view_items(self) -> list[_Item]:
        if self.current == FILES_VIEW:
            return [_Item("transfer", t.started, tid=t.transfer_id)
                    for t in sorted(self.transfers.values(), key=lambda t: t.started)]
        return self.items.get(self.current, [])

    def _render_chat(self, stick: Optional[bool] = None) -> None:
        self._chat_redraw_pending = False
        cv = self.chat
        if stick is None:
            stick = cv.yview()[1] >= 0.999
        cv.delete("all")
        self._cards.clear()
        w = cv.winfo_width()
        if w < 100:
            w = 700
        y = 16.0
        last_day = None
        items = self._view_items()
        for item in items:
            day = datetime.fromtimestamp(item.ts).date()
            if day != last_day:
                y = self._draw_pill(day_label(item.ts), y, w)
                last_day = day
            if item.kind == "msg" and item.entry is not None:
                y = self._draw_message(item.entry, y, w)
            elif item.kind == "transfer":
                t = self.transfers.get(item.tid)
                if t is not None:
                    y = self._draw_card(t, y, w)
            else:
                y = self._draw_pill(item.text, y, w, small=True)
        if not items:
            text = ("Здесь появятся передачи файлов" if self.current == FILES_VIEW else
                    "Сообщений пока нет — напишите первым!" if self.current == ALL_CHAT else
                    "Начните переписку или отправьте файл")
            self._draw_pill(text, max(40.0, cv.winfo_height() / 2 - 20), w)
        cv.configure(scrollregion=(0, 0, w, max(y + 10, cv.winfo_height())))
        if stick:
            cv.yview_moveto(1.0)

    def _draw_pill(self, text: str, y: float, w: int, small: bool = False) -> float:
        cv, fnt = self.chat, (self.f.small if small else self.f.preview)
        text = fit(tk_safe(text), fnt, w - 80)
        tw = fnt.measure(text) + 28
        h = 26 if small else 30
        rounded_rect(self.chat, w / 2 - tw / 2, y, w / 2 + tw / 2, y + h, h / 2, fill=C.PILL)
        cv.create_text(w / 2, y + h / 2, text=text, font=fnt, fill="#ffffff")
        return y + h + 12

    def _draw_message(self, entry: _ChatEntry, y: float, w: int) -> float:
        cv, f = self.chat, self.f
        m = entry.msg
        out = m.outgoing
        pad = 13
        maxw = int(min(560, max(240, w * 0.66)))
        show_name = (not out) and m.to is None
        text_id = cv.create_text(0, 0, text=tk_safe(m.text), font=f.text, fill=C.TEXT, anchor="nw",
                                 width=maxw - 2 * pad)
        x1b, y1b, x2b, y2b = cv.bbox(text_id)
        tw, th = x2b - x1b, y2b - y1b
        name_h = 0
        name_w = 0
        if show_name:
            name = fit(tk_safe(m.sender_nick), f.small_bold, maxw - 2 * pad)
            name_w = f.small_bold.measure(name)
            name_h = 20
        status, status_color = entry.status()
        time_text = datetime.fromtimestamp(m.ts).strftime("%H:%M")
        meta_w = f.small.measure(time_text) + (f.small.measure("  " + status) if status else 0)
        bw = max(tw, name_w, meta_w) + 2 * pad
        bh = pad + name_h + th + 6 + 16 + 8
        avatar_space = 0 if out else 50
        x1 = (w - 18 - bw) if out else (18 + avatar_space)
        x2, y2 = x1 + bw, y + bh
        bubble = rounded_rect(cv, x1, y, x2, y2, 16, fill=C.BUBBLE_OUT if out else C.BUBBLE_IN)
        cv.tag_lower(bubble, text_id)
        cv.coords(text_id, x1 + pad, y + pad + name_h)
        if show_name:
            cv.create_text(x1 + pad, y + pad - 1, text=name, font=f.small_bold, fill=avatar_color(m.sender_nick),
                           anchor="nw")
        mx = x2 - pad
        if status:
            cv.create_text(mx, y2 - 9, text=status, font=f.small, fill=status_color, anchor="se")
            mx -= f.small.measure(status) + 6
        cv.create_text(mx, y2 - 9, text=time_text, font=f.small, fill=C.MUTED, anchor="se")
        if not out:
            r = 19
            cv.create_oval(18, y2 - 2 * r, 18 + 2 * r, y2, fill=avatar_color(m.sender_nick), outline="#ffffff",
                           width=2)
            cv.create_text(18 + r, y2 - r, text=initials(tk_safe(m.sender_nick)), font=f.avatar_small,
                           fill="#ffffff")
        # правый клик — копировать текст
        for item in (bubble, text_id):
            cv.tag_bind(item, "<Button-3>", lambda e, t=m.text: self._copy_menu(e, t))
        return y2 + 10

    def _copy_menu(self, event, text: str) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Копировать текст", command=lambda: (self.root.clipboard_clear(),
                                                                    self.root.clipboard_append(text)))
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------- карточки файлов

    def _card_texts(self, t: Transfer) -> tuple[str, str]:
        """(строка состояния, цвет полосы прогресса)."""
        size = human_size(t.size)
        incoming = t.direction == TransferDirection.RECEIVE
        s = t.state
        if s == TransferState.WAITING_ACCEPT:
            return (f"{size} · хочет отправить вам файл" if incoming else f"{size} · ждём согласия получателя",
                    C.ACCENT)
        if s == TransferState.HASHING:
            return f"{size} · подготовка {t.progress * 100:.0f}%", C.MUTED
        if s == TransferState.TRANSFERRING:
            speed = f" · {human_speed(t.speed)}" if t.speed else ""
            return f"{human_size(t.done)} из {size} · {t.progress * 100:.0f}%{speed}", C.ACCENT
        if s == TransferState.VERIFYING:
            return f"{size} · проверка целостности", C.ACCENT
        if s == TransferState.DONE:
            speed = f" · ≈{human_speed(t.speed)}" if t.speed else ""
            return (f"{size} · получен, хеш совпал{speed}" if incoming else f"{size} · доставлен{speed}"), C.OK
        if s == TransferState.FAILED:
            return f"{size} · ошибка: {t.error}", C.ERROR
        if s == TransferState.DECLINED:
            return f"{size} · отклонено", C.MUTED
        if s == TransferState.CANCELLED:
            return f"{size} · отменено", C.MUTED
        return size, C.ACCENT

    def _draw_card(self, t: Transfer, y: float, w: int) -> float:
        cv, f = self.chat, self.f
        out = t.direction == TransferDirection.SEND
        files_view = self.current == FILES_VIEW
        cw, ch = min(400, w - 100), 104
        if files_view:
            caption = f"Вы → {t.peer_nick}" if out else f"{t.peer_nick} → вам"
            cv.create_text(w - 18 if out else 68, y, text=tk_safe(caption), font=f.small_bold, fill="#5d7486",
                           anchor="ne" if out else "nw")
            y += 18
        x1 = (w - 18 - cw) if out else 68
        x2, y2 = x1 + cw, y + ch
        rounded_rect(cv, x1, y, x2, y2, 16, fill=C.BUBBLE_OUT if out else C.BUBBLE_IN)
        # круглая иконка со стрелкой
        cx, cy, r = x1 + 36, y + 38, 22
        icon_color = C.OK if t.state == TransferState.DONE else (
            "#b4bec8" if t.state.finished else C.ACCENT)
        icon = cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=icon_color, outline="")
        cv.create_line(cx, cy - 9, cx, cy + 9, fill="#ffffff", width=3, capstyle=tk.ROUND)
        tip, base = (cy - 9, cy - 2) if out else (cy + 9, cy + 2)
        cv.create_line(cx - 7, base, cx, tip, cx + 7, base, fill="#ffffff", width=3, capstyle=tk.ROUND,
                       joinstyle=tk.ROUND)
        tx = x1 + 70
        inner = x2 - 14 - tx
        cv.create_text(tx, y + 14, text=fit(tk_safe(t.name), f.name, inner), font=f.name, fill=C.TEXT, anchor="nw")
        sub_text, bar_color = self._card_texts(t)
        sub = cv.create_text(tx, y + 38, text=fit(tk_safe(sub_text), f.small, inner), font=f.small, fill=C.MUTED,
                             anchor="nw")
        by = y + 64
        cv.create_line(tx, by, x2 - 16, by, fill=C.TRACK, width=6, capstyle=tk.ROUND)
        fill_x = tx + (x2 - 16 - tx) * t.progress
        bar = cv.create_line(tx, by, max(tx + 0.1, fill_x), by, fill=bar_color, width=6, capstyle=tk.ROUND)
        if t.progress <= 0:
            cv.itemconfigure(bar, state=tk.HIDDEN)
        # действия
        actions: list[tuple[str, Callable[[], None], bool]] = []
        if t.transfer_id in self.offers and t.state == TransferState.WAITING_ACCEPT:
            actions = [("Принять", lambda: self._resolve_offer(t.transfer_id, True), True),
                       ("Отклонить", lambda: self._resolve_offer(t.transfer_id, False), False)]
        elif not t.state.finished:
            actions = [("Отменить", lambda: self.core.cancel_transfer(t.transfer_id), False)]
        elif t.state == TransferState.DONE and t.path:
            actions = [("Открыть папку", lambda: _open_path(os.path.dirname(t.path)), True)]
        elif out and t.state in (TransferState.FAILED, TransferState.CANCELLED, TransferState.DECLINED):
            actions = [("Повторить", lambda: self._retry(t), True)]
        ax = tx
        for i, (label, cb, primary) in enumerate(actions):
            tag = f"act_{t.transfer_id}_{i}"
            cv.create_text(ax, y2 - 12, text=label, font=f.small_bold if primary else f.small,
                           fill=C.LINK if primary else "#6b7b88", anchor="sw", tags=(tag,))
            self._clickable(cv, tag, cb)
            ax += f.small_bold.measure(label) + 18
        cv.create_text(x2 - 14, y2 - 12, text=datetime.fromtimestamp(t.started).strftime("%H:%M"), font=f.small,
                       fill=C.MUTED, anchor="se")
        if not out:
            r2 = 19
            cv.create_oval(18, y2 - 2 * r2, 18 + 2 * r2, y2, fill=avatar_color(t.peer_nick), outline="#ffffff", width=2)
            cv.create_text(18 + r2, y2 - r2, text=initials(tk_safe(t.peer_nick)), font=f.avatar_small,
                           fill="#ffffff")
        self._cards[t.transfer_id] = {"sub": sub, "bar": bar, "x0": tx, "x1": x2 - 16, "y": by, "inner": inner,
                                      "icon": icon}
        self._card_state[t.transfer_id] = t.state
        return y2 + 12

    def _update_card(self, t: Transfer, card: dict) -> None:
        sub_text, bar_color = self._card_texts(t)
        self.chat.itemconfigure(card["sub"], text=fit(tk_safe(sub_text), self.f.small, card["inner"]))
        fill_x = card["x0"] + (card["x1"] - card["x0"]) * t.progress
        self.chat.coords(card["bar"], card["x0"], card["y"], max(card["x0"] + 0.1, fill_x), card["y"])
        self.chat.itemconfigure(card["bar"], fill=bar_color, state=tk.NORMAL if t.progress > 0 else tk.HIDDEN)

    def _resolve_offer(self, tid: str, accepted: bool) -> None:
        offer = self.offers.pop(tid, None)
        if offer is not None:
            offer.resolve(accepted)
        self._schedule_chat()

    def _retry(self, t: Transfer) -> None:
        peer = self.core.registry.get(t.peer_id)
        if peer is None or not peer.online:
            self.show_toast(f"{t.peer_nick} сейчас не в сети")
            return
        try:
            self.core.send_file(t.peer_id, t.path)
        except (OSError, ValueError) as exc:
            self.show_toast(str(exc))

    # ------------------------------------------------------------------ #
    # Действия
    # ------------------------------------------------------------------ #

    def send_message(self) -> None:
        if self.current == FILES_VIEW:
            self.select(ALL_CHAT)
            return
        text = self._entry_value(self.msg_entry, self.msg_var).strip()
        if not text:
            return
        to = None if self.current == ALL_CHAT else self.current
        if to is not None:
            peer = self.core.registry.get(to)
            if peer is None or not peer.online:
                self.show_toast("Пир сейчас не в сети — сообщение не будет доставлено")
                return
        targets = len(self.core.registry.online()) if to is None else 1
        try:
            msg = self.core.send_message(text, to)
        except ValueError as exc:
            self.show_toast(str(exc))
            return
        self.msg_var.set("")
        self._add_message(msg, targets=targets)
        self.chat.yview_moveto(1.0)

    def send_file(self) -> None:
        if self.current in (ALL_CHAT, FILES_VIEW):
            self.show_toast("Выберите получателя в списке слева")
            return
        peer = self.core.registry.get(self.current)
        if peer is None or not peer.online:
            self.show_toast("Пир сейчас не в сети")
            return
        paths = filedialog.askopenfilenames(parent=self.root, title=f"Файлы для {peer.nickname}")
        for path in paths:
            try:
                self.core.send_file(peer.node_id, path)
            except (OSError, ValueError) as exc:
                messagebox.showerror(APP_NAME, f"{os.path.basename(path)}: {exc}")

    def open_download_dir(self) -> None:
        path = self.core.config.download_path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        _open_path(str(path))

    def open_settings(self) -> None:
        SettingsDialog(self)


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

class SettingsDialog:
    def __init__(self, gui: LocalNetGui) -> None:
        self.gui = gui
        core, f = gui.core, gui.f
        top = self.top = tk.Toplevel(gui.root, bg=C.PANEL)
        top.title("Настройки")
        top.transient(gui.root)
        top.resizable(False, False)
        top.grab_set()

        tk.Label(top, text="Настройки", font=f.title, bg=C.PANEL, fg=C.TEXT).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=20, pady=(18, 10))

        self.nick = tk.StringVar(value=core.get_nickname())
        self.folder = tk.StringVar(value=str(core.config.download_path))
        self.auto = tk.BooleanVar(value=core.config.auto_accept)

        self._row(1, "Ваше имя", self._entry(self.nick))
        folder_row = tk.Frame(top, bg=C.PANEL)
        self._entry(self.folder, parent=folder_row).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(folder_row, "Выбрать…", self.choose_folder, primary=False).pack(side=tk.LEFT, padx=(8, 0))
        self._row(2, "Папка для файлов", folder_row)
        tk.Checkbutton(top, text="Принимать файлы без подтверждения", variable=self.auto, bg=C.PANEL, fg=C.TEXT,
                       font=f.preview, activebackground=C.PANEL, selectcolor="#ffffff", highlightthickness=0,
                       bd=0).grid(row=3, column=1, sticky="w", padx=(0, 20), pady=6)

        info = (f"IP: {core.local_ip}\n"
                f"Порты: сообщения TCP {core.messenger.port}, файлы TCP {core.files.port}, "
                f"обнаружение UDP {core.config.discovery_port}\n"
                f"Транспорт сообщений: {getattr(core.messenger, 'transport_name', '?')}\n"
                f"Файл настроек: {core.config.path}")
        tk.Label(top, text=info, justify=tk.LEFT, font=f.small, bg=C.PANEL, fg=C.MUTED, wraplength=520).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=20, pady=(12, 4))

        buttons = tk.Frame(top, bg=C.PANEL)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", padx=20, pady=(10, 18))
        self._button(buttons, "Отмена", top.destroy, primary=False).pack(side=tk.RIGHT, padx=(8, 0))
        self._button(buttons, "Сохранить", self.save, primary=True).pack(side=tk.RIGHT)
        top.bind("<Escape>", lambda _e: top.destroy())
        # по центру главного окна
        top.update_idletasks()
        root = gui.root
        x = root.winfo_rootx() + (root.winfo_width() - top.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.bind("<Return>", lambda _e: self.save())

    def _entry(self, var: tk.StringVar, parent: Optional[tk.Widget] = None) -> tk.Entry:
        return tk.Entry(parent or self.top, textvariable=var, font=self.gui.f.text, relief=tk.FLAT, bg=C.SEARCH_BG,
                        fg=C.TEXT, insertbackground=C.TEXT, highlightthickness=1, highlightbackground=C.LINE,
                        highlightcolor=C.ACCENT, width=40)

    def _row(self, row: int, label: str, widget: tk.Widget) -> None:
        tk.Label(self.top, text=label, font=self.gui.f.preview, bg=C.PANEL, fg=C.MUTED).grid(
            row=row, column=0, sticky="w", padx=(20, 12), pady=6)
        widget.grid(row=row, column=1, sticky="we", padx=(0, 20), pady=6, ipady=4)

    def _button(self, parent: tk.Widget, text: str, cb, primary: bool) -> tk.Label:
        b = tk.Label(parent, text=text, font=self.gui.f.top_bold, padx=16, pady=6, cursor="hand2",
                     bg=C.ACCENT if primary else C.SEARCH_BG, fg="#ffffff" if primary else C.TEXT)
        b.bind("<Button-1>", lambda _e: cb())
        return b

    def choose_folder(self) -> None:
        path = filedialog.askdirectory(parent=self.top, initialdir=self.folder.get(), title="Папка для входящих файлов")
        if path:
            self.folder.set(path)

    def save(self) -> None:
        core = self.gui.core
        try:
            if self.nick.get().strip() and self.nick.get().strip() != core.get_nickname():
                core.set_nickname(self.nick.get())
            if self.folder.get().strip() and self.folder.get().strip() != str(core.config.download_path):
                core.set_download_dir(self.folder.get().strip())
            if self.auto.get() != core.config.auto_accept:
                core.config.auto_accept = self.auto.get()
                core.save_config()
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self.top)
            return
        self.top.destroy()
        self.gui._draw_top()
        self.gui.show_toast("Настройки сохранены")


# --------------------------------------------------------------------------- #

def _hex(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hm(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == date.today() else d.strftime("%d.%m %H:%M")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
