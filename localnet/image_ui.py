"""Предпросмотр, галерея в переписке и полноэкранный просмотр изображений."""

from __future__ import annotations

import time
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .attachments import MAX_CAPTION, pil_modules, system_codec
from .image_previews import ImagePreviews
from .models import Transfer, TransferDirection, TransferState
from .utils import human_size


class ImageComposer:
    def __init__(self, root, previews, paths, draft, on_send):
        self.paths = list(paths)
        self.previews = previews
        self.on_send = on_send
        self.draft = draft
        self.page = 0
        self.window = tk.Toplevel(root)
        self.window.title("Отправить изображения")
        self.window.transient(root)
        self.window.minsize(470, 380)
        self.window.geometry("670x480")
        self.window.bind("<Escape>", lambda _e: self.window.destroy())
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        top = ttk.Frame(self.window, padding=(16, 12, 16, 4))
        top.grid(row=0, column=0, sticky="ew")
        self.heading = ttk.Label(top)
        self.heading.pack(side="left")
        photo_available = bool(pil_modules() or system_codec())
        self.mode = tk.StringVar(value="photo" if photo_available else "file")
        ttk.Radiobutton(top, text="Как файл", variable=self.mode, value="file").pack(side="right")
        photo_mode = ttk.Radiobutton(top, text="Как фото", variable=self.mode, value="photo")
        photo_mode.pack(side="right", padx=(0, 8))
        if not photo_available:
            photo_mode.state(["disabled"])
        self.grid = ttk.Frame(self.window, padding=(12, 8))
        self.grid.grid(row=1, column=0, sticky="nsew")
        footer = ttk.Frame(self.window, padding=(16, 4, 16, 12))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, text="Подпись к альбому").pack(anchor="w")
        self.caption = tk.Text(footer, height=3, wrap="word", font="TkDefaultFont")
        self.caption.pack(fill="x", pady=(4, 10))
        self.caption.insert("1.0", draft)
        self.separate = tk.BooleanVar(value=False)
        if draft.strip():
            ttk.Checkbutton(footer, text="Отправить текст из поля чата отдельным сообщением",
                            variable=self.separate, command=self._toggle_separate).pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(footer)
        row.pack(fill="x")
        self.back = ttk.Button(row, text="‹", width=3, command=lambda: self._change_page(-1))
        self.back.pack(side="left")
        self.forward = ttk.Button(row, text="›", width=3, command=lambda: self._change_page(1))
        self.forward.pack(side="left", padx=(4, 0))
        self.position = ttk.Label(row, padding=(8, 0))
        self.position.pack(side="left")
        ttk.Button(row, text="Отмена", command=self.window.destroy).pack(side="right", padx=(8, 0))
        self.send = ttk.Button(row, text="Отправить", command=self._send)
        self.send.pack(side="right")
        self.window.bind("<Control-Return>", lambda _e: self._send())
        self.window.bind("<Map>", self._grab, add="+")
        self.caption.focus_set()
        self._render()

    def _grab(self, event):
        try:
            if event.widget is self.window and self.window.winfo_viewable():
                self.window.grab_set()
        except tk.TclError:
            pass

    def _change_page(self, amount):
        self.page += amount
        self._render()

    def _render(self):
        page_size = 6
        pages = max(1, (len(self.paths) + page_size - 1) // page_size)
        self.page = min(max(0, self.page), pages - 1)
        self.heading.configure(text=f"Изображения: {len(self.paths)}")
        self.position.configure(text=f"{self.page + 1} / {pages}")
        self.back.state(["!disabled" if self.page > 0 else "disabled"])
        self.forward.state(["!disabled" if self.page + 1 < pages else "disabled"])
        self.send.state(["!disabled" if self.paths else "disabled"])
        for widget in self.grid.winfo_children():
            widget.destroy()
        for column in range(3):
            self.grid.columnconfigure(column, weight=1, uniform="photo")
        for local_index, path in enumerate(self.paths[self.page * page_size:(self.page + 1) * page_size]):
            index = self.page * page_size + local_index
            tile = ttk.Frame(self.grid, padding=4)
            tile.grid(row=local_index // 3, column=local_index % 3, sticky="nsew", padx=4, pady=3)
            preview = tk.Canvas(tile, height=87, width=134, bg="#e8eff7", highlightthickness=0)
            preview.pack(fill="x")
            preview.bind("<Configure>", lambda _e, canvas=preview: self._place_preview(canvas))
            preview.create_text(67, 42, text="Загрузка…", fill="#455a70", tags=("placeholder",))
            ttk.Label(tile, text=Path(path).name[:23]).pack(side="left", fill="x", expand=True)
            ttk.Button(tile, text="×", width=3, command=lambda i=index: self._remove(i)).pack(side="right")
            self.previews.request(path, lambda result, canvas=preview: self._ready(canvas, result), edge=136)

    def _ready(self, canvas, result):
        if not canvas.winfo_exists():
            return
        if result is None:
            canvas.itemconfigure("placeholder", text="Превью недоступно")
            return
        canvas.photo = result.photo(canvas)
        stride = max(1, (canvas.photo.height() + 83) // 84)
        if stride > 1:
            canvas.photo = canvas.photo.subsample(stride)
        canvas.delete("placeholder")
        canvas.create_image(0, 0, image=canvas.photo, tags=("preview",))
        self._place_preview(canvas)

    @staticmethod
    def _place_preview(canvas):
        canvas.coords("placeholder", canvas.winfo_width() / 2, 43)
        canvas.coords("preview", canvas.winfo_width() / 2, 43)

    def _remove(self, index):
        del self.paths[index]
        self._render()

    def _toggle_separate(self):
        caption = self.caption.get("1.0", "end-1c")
        if self.separate.get() and caption == self.draft:
            self.caption.delete("1.0", tk.END)
        elif not self.separate.get() and not caption:
            self.caption.insert("1.0", self.draft)

    def _send(self):
        caption = self.caption.get("1.0", "end-1c").strip()
        if not self.paths:
            return
        if len(caption) > MAX_CAPTION:
            messagebox.showwarning("Подпись", f"Подпись может содержать до {MAX_CAPTION} символов.",
                                   parent=self.window)
            return
        try:
            self.on_send(list(self.paths), self.mode.get(), caption, self.separate.get())
        except (ValueError, OSError) as exc:
            messagebox.showwarning("Изображения", str(exc), parent=self.window)
            return
        self.window.destroy()


class ImageViewer:
    def __init__(self, gui, transfers, index=0):
        self.gui = gui
        self.images = transfers
        self.index = index
        self.zoom = 1.0
        self.serial = 0
        self.advance = None
        self.frame_after = None
        self.window = tk.Toplevel(gui.root, bg="#111b2d")
        self.window.title("Просмотр изображения")
        self.window.geometry(f"{gui.root.winfo_screenwidth()}x{gui.root.winfo_screenheight()}+0+0")
        self.window.attributes("-fullscreen", True)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.bind("<Escape>", lambda _e: self._close())
        self.window.bind("<Left>", lambda _e: self._move(-1))
        self.window.bind("<Right>", lambda _e: self._move(1))
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.window.bind(sequence, self._wheel)

        toolbar = ttk.Frame(self.window, padding=10)
        toolbar.pack(fill="x")
        self.title = ttk.Label(toolbar)
        self.title.pack(side="left", padx=5)
        for text, callback in (("Закрыть", self._close), ("Папка", self._folder), ("Копировать", self._copy),
                               ("Открыть оригинал", self._open), ("Сохранить", self._save)):
            ttk.Button(toolbar, text=text, command=callback).pack(side="right", padx=3)
        ttk.Button(toolbar, text="‹", width=3, command=lambda: self._move(-1)).pack(side="right")
        ttk.Button(toolbar, text="›", width=3, command=lambda: self._move(1)).pack(side="right", padx=3)
        status = ttk.Frame(self.window, padding=(10, 5))
        status.pack(side="bottom", fill="x")
        self.caption = ttk.Label(status)
        self.caption.pack()
        self.canvas = tk.Canvas(self.window, bg="#111b2d", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        self.window.update_idletasks()
        self.window.focus_set()
        self._load()

    def _wheel(self, event):
        direction = 1 if event.num == 4 or event.delta > 0 else -1
        self.zoom = min(3.0, max(0.3, self.zoom * 1.22 ** direction))
        if self.advance is not None:
            self.window.after_cancel(self.advance)
        self.advance = self.window.after(100, self._load)
        return "break"

    def _move(self, delta):
        self.index = (self.index + delta) % len(self.images)
        self.zoom = 1.0
        self._load()

    def _load(self):
        self.advance = None
        self._cancel_animation()
        self.serial += 1
        serial = self.serial
        transfer = self.images[self.index]
        self.title.configure(text=f"{self.index + 1} / {len(self.images)}   {transfer.name[:54]}")
        self.caption.configure(text=transfer.image.caption if transfer.image else "")
        self.canvas.delete("all")
        self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                                text="Загрузка…", fill="#ffffff")
        width = max(240, self.canvas.winfo_width() - 30)
        height = max(240, self.canvas.winfo_height() - 30)
        edge = min(2400, max(120, int(max(width, height) * self.zoom)))
        self.gui.image_panel.previews.request(
            transfer.path, lambda preview: self._ready(preview, serial, width, height),
            edge=edge, animate=Path(transfer.name).suffix.lower() in (".gif", ".webp", ".png"))

    def _ready(self, preview, serial, width, height):
        if not self.window.winfo_exists() or serial != self.serial:
            return
        if preview is None:
            item = self.canvas.find_all()
            if item:
                self.canvas.itemconfigure(item[-1], text="Просмотр недоступен. Можно сохранить или открыть файл.")
            return
        photo = preview.photo(self.canvas)
        stride = max(1, (photo.width() + int(width * self.zoom) - 1) // int(width * self.zoom),
                     (photo.height() + int(height * self.zoom) - 1) // int(height * self.zoom))
        self._draw_frame(preview, serial, 0, stride)

    def _draw_frame(self, preview, serial, index, stride):
        self.frame_after = None
        if serial != self.serial:
            return
        photo = preview.photo(self.canvas, index)
        self.canvas.photo = photo.subsample(stride) if stride > 1 else photo
        if index == 0 and not self.canvas.find_withtag("photo"):
            self.canvas.delete("all")
            w = max(self.canvas.winfo_width(), self.canvas.photo.width())
            h = max(self.canvas.winfo_height(), self.canvas.photo.height())
            self.canvas.configure(scrollregion=(0, 0, w, h))
            self.canvas.create_image(w / 2, h / 2, image=self.canvas.photo, tags=("photo",))
            self.canvas.xview_moveto((w - self.canvas.winfo_width()) / (2 * w))
            self.canvas.yview_moveto((h - self.canvas.winfo_height()) / (2 * h))
        else:
            self.canvas.itemconfigure("photo", image=self.canvas.photo)
        delay = preview.frames[index][1]
        if index + 1 < len(preview.frames):
            self.frame_after = self.window.after(delay, self._draw_frame, preview, serial, index + 1, stride)
        elif preview.next_frame or preview.first_frame:
            self.frame_after = self.window.after(delay, self._next_frames, preview, serial, stride)
        elif len(preview.frames) > 1:
            self.frame_after = self.window.after(delay, self._draw_frame, preview, serial, 0, stride)

    def _next_frames(self, previous, serial, stride):
        self.frame_after = None
        if serial != self.serial:
            return

        def ready(preview):
            if preview and preview.frames and self.window.winfo_exists() and serial == self.serial:
                self._draw_frame(preview, serial, 0, stride)

        self.gui.image_panel.previews.request(
            self.images[self.index].path, ready, edge=previous.edge, animate=True,
            start_frame=previous.next_frame)

    def _cancel_animation(self):
        if self.frame_after is not None:
            self.window.after_cancel(self.frame_after)
            self.frame_after = None

    def _close(self):
        if self.advance is not None:
            self.window.after_cancel(self.advance)
        self._cancel_animation()
        self.window.destroy()

    def _open(self):
        self.gui.open_attachment(self.images[self.index].path)

    def _folder(self):
        self.gui.open_attachment(str(Path(self.images[self.index].path).parent))

    def _copy(self):
        self.gui.copy_attachment(self.images[self.index].path)

    def _save(self):
        transfer = self.images[self.index]
        destination = filedialog.asksaveasfilename(parent=self.window, initialfile=transfer.name)
        if not destination:
            return
        self.gui.save_attachment(transfer.path, destination)


def album_key(transfer: Transfer) -> tuple:
    return (transfer.conversation, transfer.direction,
            transfer.peer_id if transfer.direction == TransferDirection.RECEIVE else "", transfer.image.album)


class ImagePanel:
    def __init__(self, gui, colors, rounded_rect, safe_text):
        self.gui = gui
        self.colors = colors
        self.rounded_rect = rounded_rect
        self.safe_text = safe_text
        self.previews = ImagePreviews(gui.root, gui.core.images.root / "thumbnails")
        self.decisions = OrderedDict()
        self.slots = {}
        self.visible_after = None
        self.generation = 0
        self.photos = {}
        self.loading = set()
        self.images = OrderedDict()
        self.progress = {}

    def start_draw(self):
        self.generation += 1
        self.slots.clear()
        self.photos.clear()
        self.loading.clear()
        self.progress.clear()

    def consume_decision(self, offer):
        choice = self.decisions.get(album_key(offer.transfer))
        if choice is None:
            return False
        timestamp, decision, model = choice
        image = offer.transfer.image
        if time.monotonic() - timestamp > 240 or (image.count, image.mode, image.caption) != model:
            return False
        offer.resolve(decision)
        return True

    def _resolve(self, transfer, accepted):
        key = album_key(transfer)
        image = transfer.image
        model = (image.count, image.mode, image.caption)
        self.decisions[key] = (time.monotonic(), accepted, model)
        self.decisions.move_to_end(key)
        if len(self.decisions) > 120:
            self.decisions.popitem(last=False)
        for offer in list(self.gui.offers.values()):
            if offer.transfer.image and album_key(offer.transfer) == key:
                self.consume_decision(offer)

    def draw(self, transfers, y, width):
        cv, font, color = self.gui.chat, self.gui.f, self.colors
        first = transfers[0]
        meta = first.image
        transfers = [t for t in transfers if
                     (t.image.count, t.image.mode, t.image.caption) == (meta.count, meta.mode, meta.caption)]
        outgoing = first.direction == TransferDirection.SEND
        latest = {}
        for t in sorted(transfers, key=lambda t: t.started):
            latest[(t.peer_id, t.image.index)] = t
        peers = {t.peer_id for t in latest.values()}
        caption = self.safe_text(meta.caption)
        columns = min(meta.count, 3)
        rows = (meta.count + columns - 1) // columns
        margin = 13
        box_width = min(width - 100, 460 if columns > 1 else 362)
        cell_height = 134 if columns > 1 else 190
        cell_width = (box_width - 2 * margin - 5 * (columns - 1)) / columns
        x = width - box_width - 18 if outgoing and self.gui.own_right else 68
        cover = f"album_caption_{first.transfer_id}"
        y_grid = y + 36
        y_footer = y_grid + rows * (cell_height + 5)
        caption_id = None
        if caption:
            caption_id = cv.create_text(x + margin, y_footer + 3, text=caption, anchor="nw",
                                        font=font.text, fill=color.TEXT, width=box_width - 2 * margin, tags=(cover,))
            y_footer = cv.bbox(caption_id)[3] + 8
        bottom = y_footer + 41
        self.rounded_rect(cv, x, y + 2, x + box_width, bottom + 2, 16, fill=color.SHADOW)
        self.rounded_rect(cv, x, y, x + box_width, bottom, 16,
                          fill=color.BUBBLE_OUT if outgoing else color.BUBBLE_IN)
        label = "Фото" if meta.mode == "photo" else "Изображения без сжатия"
        if not outgoing and first.group:
            label = f"{first.peer_nick} · {label}"
        cv.create_text(x + margin, y + 17, text=self.safe_text(label), font=font.small_bold,
                       fill=color.TEXT, anchor="w")
        if caption_id is not None:
            cv.tag_raise(caption_id)
        choices = {}
        for t in latest.values():
            if t.state == TransferState.DONE and t.path:
                choices.setdefault(t.image.index, t)
        viewable = [t for _, t in sorted(choices.items())]
        for index in range(meta.count):
            column, row = index % columns, index // columns
            x0 = x + margin + column * (cell_width + 5)
            y0 = y_grid + row * (cell_height + 5)
            tag = f"image_{first.transfer_id}_{index}"
            cv.create_rectangle(x0, y0, x0 + cell_width, y0 + cell_height, fill=color.SEARCH_BG,
                                outline="", tags=(tag,))
            center = (x0 + cell_width / 2, y0 + cell_height / 2)
            image = choices.get(index)
            attempts = [latest[(peer, index)] for peer in peers if (peer, index) in latest]
            if image is not None:
                cv.create_text(*center, text="Открыть изображение", fill=color.MUTED, font=font.small,
                               width=cell_width - 12, tags=(tag, f"{tag}_label"))
                self.slots[tag] = (image, *center, int(cell_width - 8), cell_height - 8)
                self.gui._clickable(cv, tag, lambda i=viewable.index(image): ImageViewer(self.gui, viewable, i))
            elif attempts:
                active = next((t for t in attempts if not t.state.finished), attempts[-1])
                state = f"{active.progress * 100:.0f}%" if active.state == TransferState.TRANSFERRING else active.state.value
                name = self.safe_text(active.name)
                label = cv.create_text(*center, text=f"{name}\n{state}", fill=color.MUTED, font=font.small,
                                       width=cell_width - 12, justify="center", tags=(tag,))
                if active.state == TransferState.TRANSFERRING:
                    cv.create_line(x0 + 8, y0 + cell_height - 8, x0 + cell_width - 8, y0 + cell_height - 8,
                                   width=3, fill=color.TRACK)
                    width = cell_width - 16
                    progress_y = y0 + cell_height - 8
                    bar = cv.create_line(x0 + 8, progress_y, x0 + 8 + width * active.progress,
                                         progress_y, width=3, fill=color.ACCENT)
                    self.progress[active.transfer_id] = label, name, bar, x0 + 8, progress_y, width
            else:
                cv.create_text(*center, text="Ожидается", fill=color.MUTED, font=font.small, tags=(tag,))

        actual = sum(t.state == TransferState.DONE for t in latest.values())
        expected = meta.count * len(peers)
        problems = [t for t in latest.values() if t.state in
                    (TransferState.CANCELLED, TransferState.DECLINED, TransferState.FAILED)]
        status = f"{'Доставлено' if outgoing else 'Получено'} {actual}/{expected}"
        if problems:
            status += f" · не получено: {len(problems)}"
        cv.create_text(x + margin, y_footer + 20, text=status, font=font.small,
                       fill=color.ERROR if problems else color.MUTED, anchor="w")
        offers = [t for t in latest.values() if t.transfer_id in self.gui.offers]
        if offers:
            self._action(first, "Отклонить", x + box_width - margin, y_footer + 20,
                         lambda: self._resolve(first, False))
            self._action(first, "Принять", x + box_width - margin - 77, y_footer + 20,
                         lambda: self._resolve(first, True))
        elif outgoing and problems:
            self._action(first, "Повторить", x + box_width - margin, y_footer + 20,
                         lambda: self._retry(problems))
        elif outgoing and any(not t.state.finished for t in latest.values()):
            self._action(first, "Отменить", x + box_width - margin, y_footer + 20,
                         lambda: self._cancel(latest.values()))
        return bottom + 12

    def _retry(self, transfers):
        for transfer in transfers:
            self.gui._retry(transfer)

    def _cancel(self, transfers):
        for transfer in transfers:
            if not transfer.state.finished:
                self.gui.core.cancel_transfer(transfer.transfer_id)

    def _action(self, transfer, text, x, y, callback):
        tag = f"album_action_{transfer.transfer_id}_{text}"
        self.gui.chat.create_text(x, y, text=text, font=self.gui.f.small_bold,
                                  fill=self.colors.LINK, anchor="e", tags=(tag,))
        self.gui._clickable(self.gui.chat, tag, callback)

    def schedule_visible(self):
        if self.visible_after is None:
            self.visible_after = self.gui.root.after_idle(self._visible)

    def update_progress(self, t):
        widgets = self.progress.get(t.transfer_id)
        if widgets is not None:
            label, name, bar, x, y, width = widgets
            self.gui.chat.itemconfigure(label, text=f"{name}\n{t.progress * 100:.0f}%")
            self.gui.chat.coords(bar, x, y, x + width * t.progress, y)

    def _visible(self):
        self.visible_after = None
        cv, generation = self.gui.chat, self.generation
        top = cv.canvasy(0) - 180
        bottom = top + cv.winfo_height() + 360
        for tag in list(self.photos):
            if tag in self.slots and not top <= self.slots[tag][2] <= bottom:
                cv.delete(f"{tag}_preview")
                cv.itemconfigure(f"{tag}_label", state=tk.NORMAL)
                del self.photos[tag]
        for tag, (image, cx, cy, w, h) in self.slots.items():
            if top <= cy <= bottom and tag not in self.photos and tag not in self.loading:
                key = (image.path, image.sha256, w, h)
                existing = self.images.get(key)
                if existing is not None:
                    self.images.move_to_end(key)
                    self._display(tag, cx, cy, existing)
                    continue
                self.loading.add(tag)
                self.previews.request(image.path, lambda preview, t=tag:
                                      self._received(t, generation, preview), digest=image.sha256)

    def _received(self, tag, generation, preview):
        if generation != self.generation:
            return
        if preview is None:
            return
        self.loading.discard(tag)
        cv = self.gui.chat
        image, cx, cy, w, h = self.slots[tag]
        if not cv.canvasy(-180) <= cy <= cv.canvasy(cv.winfo_height() + 180):
            return
        photo = preview.photo(cv)
        stride = max(1, (photo.width() + w - 1) // w, (photo.height() + h - 1) // h)
        photo = photo.subsample(stride) if stride > 1 else photo
        self.images[(image.path, image.sha256, w, h)] = photo
        if len(self.images) > 54:
            self.images.popitem(last=False)
        self._display(tag, cx, cy, photo)

    def _display(self, tag, cx, cy, photo):
        self.photos[tag] = photo
        self.gui.chat.itemconfigure(f"{tag}_label", state=tk.HIDDEN)
        self.gui.chat.create_image(cx, cy, image=photo, tags=(tag, f"{tag}_preview"))
