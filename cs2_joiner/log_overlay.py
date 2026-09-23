"""Non-activating, click-through log status display above the CS2 window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import PureWindowsPath
import tkinter as tk

from .config_io import read_config, write_config


POSITIONS = ("Top right", "Top left", "Bottom right", "Bottom left")
MODE_STYLES = {
    "BYPASSED": ("NOT USED", "#9da8a2"),
    "NOT CONFIGURED": ("SETUP NEEDED", "#9da8a2"),
    "UNKNOWN": ("UNCONFIRMED", "#9da8a2"),
    "SETUP ERROR": ("SETUP ERROR", "#ef9292"),
    "PAUSED FLAGS": ("SUPPRESSED", "#e2c58c"),
    "RECORDING": ("RECORDING", "#8ce7bd"),
    "WAITING FOR CS2": ("WAITING FOR CS2", "#8ed2ee"),
    "RESUMING": ("RESUMING", "#8ed2ee"),
}


def mode_style(state: str) -> tuple[str, str]:
    return MODE_STYLES.get(state, MODE_STYLES["UNKNOWN"])


@dataclass(frozen=True)
class OverlaySettings:
    enabled: bool = True
    position: str = "Top right"


def load_overlay_settings(path) -> OverlaySettings:
    parser = read_config(path)
    try:
        enabled = parser.getboolean("LogOverlay", "Enabled", fallback=True)
    except ValueError:
        enabled = True
    position = parser.get("LogOverlay", "Position", fallback="Top right")
    return OverlaySettings(enabled, position if position in POSITIONS else "Top right")


def save_overlay_settings(path, settings: OverlaySettings) -> None:
    if settings.position not in POSITIONS:
        raise ValueError("Invalid overlay position.")
    parser = read_config(path)
    if not parser.has_section("LogOverlay"):
        parser.add_section("LogOverlay")
    parser.set("LogOverlay", "Enabled", "1" if settings.enabled else "0")
    parser.set("LogOverlay", "Position", settings.position)
    write_config(path, parser)


@dataclass(frozen=True)
class GameWindow:
    hwnd: int
    pid: int
    rect: tuple[int, int, int, int]


def overlay_position(rect: tuple[int, int, int, int], width: int, height: int,
                     position: str) -> tuple[int, int]:
    left, top, right, bottom = rect
    x = right - width - 16 if position.endswith("right") else left + 16
    y = bottom - height - 64 if position.startswith("Bottom") else top + 48
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


class GameWindows:
    """Only queries process identity and configures our own overlay window."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("The CS2 overlay requires Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user, kernel = self.user32, self.kernel32
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.IsIconic.argtypes = [wintypes.HWND]
        user.IsIconic.restype = wintypes.BOOL
        user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetClientRect.restype = wintypes.BOOL
        user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user.ClientToScreen.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        self._get_style = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
        self._set_style = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
        self._get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self._get_style.restype = ctypes.c_ssize_t
        self._set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_style.restype = ctypes.c_ssize_t
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos.restype = wintypes.BOOL

    def foreground(self) -> GameWindow | None:
        user, kernel = self.user32, self.kernel32
        hwnd = user.GetForegroundWindow()
        if not hwnd or user.IsIconic(hwnd):
            return None
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                return None
            if PureWindowsPath(path.value).name.casefold() != "cs2.exe":
                return None
        finally:
            kernel.CloseHandle(handle)
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        if rect.right <= 0 or rect.bottom <= 0 or user.GetForegroundWindow() != hwnd:
            return None
        return GameWindow(hwnd, pid.value, (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom))

    def prepare_overlay(self, widget_id: int) -> int:
        hwnd = self.user32.GetAncestor(widget_id, 2) or widget_id
        # Layered + transparent windows pass mouse input through; NOACTIVATE keeps game focus.
        style = self._get_style(hwnd, -20)
        ctypes.set_last_error(0)
        result = self._set_style(hwnd, -20, (style | 0x08000000 | 0x00080000 | 0x20 | 0x80) & ~0x40000)
        if result == 0 and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd

    def place_overlay(self, hwnd: int, x: int, y: int, width: int, height: int) -> None:
        if not self.user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0010 | 0x0040):
            raise ctypes.WinError(ctypes.get_last_error())


class LogStatusOverlay:
    def __init__(self, root: tk.Tk, windows: GameWindows):
        self.windows = windows
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg="#111414")
        self.window.attributes("-alpha", 0.90)
        self.bar = tk.Frame(self.window, bg="#9da8a2", width=3)
        self.bar.pack(side="left", fill="y")
        content = tk.Frame(self.window, bg="#111414", padx=12, pady=8)
        content.pack(fill="both", expand=True)
        self.label = tk.Label(content, text="LOG  UNCONFIRMED", bg="#111414", fg="#9da8a2",
                              font=("Consolas", 11, "bold"), padx=0, pady=0, width=24, anchor="w")
        self.label.pack(fill="x")
        self.note = tk.Label(content, text="CS2 JOINER", bg="#111414", fg="#9da8a2",
                             font=("Segoe UI", 9), padx=0, pady=0, anchor="w")
        self.note.pack(fill="x", pady=(3, 0))
        self.window.update_idletasks()
        self.hwnd = windows.prepare_overlay(self.window.winfo_id())
        self._visible = False
        self._last_layout = None

    def update(self, game: GameWindow | None, state: str, position: str, enabled: bool, note: str = "") -> None:
        if not enabled or game is None:
            self.hide()
            return
        text, color = mode_style(state)
        self.label.configure(text=f"LOG  {text}", fg=color)
        self.bar.configure(bg=color)
        self.note.configure(text=note or "CS2 JOINER")
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        left, top, right, bottom = game.rect
        if right - left < width or bottom - top < height:
            self.hide()
            return
        x, y = overlay_position(game.rect, width, height, position)
        layout = (game.hwnd, x, y, width, height)
        if not self._visible:
            self.window.deiconify()
        if not self._visible or layout != self._last_layout:
            self.windows.place_overlay(self.hwnd, x, y, width, height)
        self._visible = True
        self._last_layout = layout

    def hide(self) -> None:
        if self._visible:
            self.window.withdraw()
            self._visible = False
            self._last_layout = None

    def destroy(self) -> None:
        self.window.destroy()
