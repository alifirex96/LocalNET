"""Чтение двоичных изображений из буфера обмена Windows и X11."""

from __future__ import annotations

import ctypes as C
import ctypes.util
import io
import os
import select
import sys
import time

MAX_CLIPBOARD = 48 * 1024 * 1024
MIME = (("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/webp", ".webp"),
        ("image/gif", ".gif"), ("image/bmp", ".bmp"), ("image/x-bmp", ".bmp"))


class _Selection(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("synthetic", C.c_int), ("display", C.c_void_p),
                *[(name, C.c_ulong) for name in ("requestor", "selection", "target", "property", "time")]]


class _Property(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("synthetic", C.c_int), ("display", C.c_void_p),
                ("window", C.c_ulong), ("atom", C.c_ulong), ("time", C.c_ulong), ("state", C.c_int)]


class _Event(C.Union):
    _fields_ = [("type", C.c_int), ("selection", _Selection), ("property", _Property),
                ("storage", C.c_long * 24)]


def read_image(targets: tuple[str, ...]) -> tuple[bytes, str] | None:
    if sys.platform == "win32":
        try:
            from PIL import Image, ImageGrab
        except ImportError:
            return None
        contents = ImageGrab.grabclipboard()
        if isinstance(contents, Image.Image) and 0 < contents.width * contents.height <= 25_000_000:
            output = io.BytesIO()
            contents.save(output, "PNG")
            if output.tell() <= MAX_CLIPBOARD:
                return output.getvalue(), ".png"
        return None
    if not os.environ.get("DISPLAY"):
        return None
    try:
        return _read_x11(targets)
    except (OSError, TimeoutError):
        return None


def _read_x11(targets: tuple[str, ...]) -> tuple[bytes, str] | None:
    lib = C.CDLL(C.util.find_library("X11") or "libX11.so.6")
    ptr, atom, integer, long = C.c_void_p, C.c_ulong, C.c_int, C.c_long
    for name, result, arguments in (
        ("XOpenDisplay", ptr, [C.c_char_p]),
        ("XCloseDisplay", integer, [ptr]),
        ("XDefaultRootWindow", atom, [ptr]),
        ("XConnectionNumber", integer, [ptr]),
        ("XCreateSimpleWindow", atom, [ptr, atom, integer, integer, C.c_uint, C.c_uint, C.c_uint, atom, atom]),
        ("XInternAtom", atom, [ptr, C.c_char_p, integer]),
        ("XSelectInput", integer, [ptr, atom, long]),
        ("XConvertSelection", integer, [ptr, atom, atom, atom, atom, atom]),
        ("XPending", integer, [ptr]),
        ("XNextEvent", integer, [ptr, C.POINTER(_Event)]),
        ("XFlush", integer, [ptr]),
        ("XFree", integer, [ptr]),
        ("XGetWindowProperty", integer, [ptr, atom, atom, long, long, integer, atom, C.POINTER(atom),
                                        C.POINTER(integer), C.POINTER(atom), C.POINTER(atom), C.POINTER(ptr)]),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, arguments
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    try:
        root = lib.XDefaultRootWindow(display)
        window = lib.XCreateSimpleWindow(display, root, 0, 0, 1, 1, 0, 0, 0)
        lib.XSelectInput(display, window, 1 << 22)  # PropertyChangeMask
        selection = lib.XInternAtom(display, b"CLIPBOARD", 0)
        property_atom = lib.XInternAtom(display, b"LOCALNET_CLIPBOARD_IMAGE", 0)
        incr = lib.XInternAtom(display, b"INCR", 0)
        fd = lib.XConnectionNumber(display)
        deadline = time.monotonic() + 5
        event = _Event()

        def next_event() -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            while not lib.XPending(display):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise TimeoutError()
            lib.XNextEvent(display, C.byref(event))

        def read_property():
            kind, bits, count, extra, data = atom(), integer(), atom(), atom(), ptr()
            status = lib.XGetWindowProperty(display, window, property_atom, 0, MAX_CLIPBOARD // 4,
                                            1, 0, C.byref(kind), C.byref(bits),
                                            C.byref(count), C.byref(extra), C.byref(data))
            try:
                if status or extra.value or (kind.value != incr and bits.value != 8):
                    return None, None
                binary = C.string_at(data, count.value) if bits.value == 8 and count.value else b""
                return kind.value, binary
            finally:
                if data:
                    lib.XFree(data)
                lib.XFlush(display)

        for target, extension in MIME:
            if target not in targets:
                continue
            target_atom = lib.XInternAtom(display, target.encode("ascii"), 0)
            lib.XConvertSelection(display, selection, target_atom, property_atom, window, 0)
            lib.XFlush(display)
            while True:
                next_event()
                if event.type == 31 and event.selection.selection == selection:  # SelectionNotify
                    break
            if event.selection.property == 0:
                continue
            kind, data = read_property()
            if kind == incr:
                chunks = bytearray()
                while True:
                    next_event()
                    if event.type != 28 or event.property.atom != property_atom or event.property.state != 0:
                        continue
                    _, piece = read_property()
                    if piece is None or len(chunks) + len(piece) > MAX_CLIPBOARD:
                        return None
                    if not piece:
                        break
                    chunks.extend(piece)
                data = bytes(chunks)
            if data:
                return data, extension
    finally:
        lib.XCloseDisplay(display)
    return None
