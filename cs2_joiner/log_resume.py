"""A single foreground-only resume key press, followed by log confirmation."""

import ctypes
from ctypes import wintypes
import time

from .log_output import LOG_KEYS, LogOutputMonitor
from .log_overlay import GameWindow, GameWindows


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


class ResumeKeySender:
    def __init__(self, windows: GameWindows):
        self.windows = windows
        user = windows.user32
        user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user.GetAsyncKeyState.restype = ctypes.c_short
        user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        user.SendInput.restype = wintypes.UINT

    def send(self, key: str, target: GameWindow, before_send=lambda: None) -> bool:
        if key not in LOG_KEYS:
            raise ValueError("Invalid resume key.")
        vk = 0x70 + int(key[1:]) - 1
        user = self.windows.user32
        # Let the user's Alt+Tab/modifier/key release finish before sending anything.
        if any(user.GetAsyncKeyState(code) & 0x8000 for code in (0x10, 0x11, 0x12, 0x5B, 0x5C, vk)):
            return False
        current = self.windows.foreground()
        if current is None or (current.hwnd, current.pid) != (target.hwnd, target.pid):
            return False
        events = (_Input * 2)()
        for index, flags in enumerate((0, 0x0002)):
            events[index].type = 1
            events[index].value.ki = _KeyboardInput(vk, 0, flags, 0, 0)
        before_send()
        if user.GetForegroundWindow() != target.hwnd:
            return False
        sent = user.SendInput(2, events, ctypes.sizeof(_Input))
        if sent != 2:
            if sent == 1:
                # Release our own key if Windows accepted only the down event.
                release = (_Input * 1)(events[1])
                user.SendInput(1, release, ctypes.sizeof(_Input))
            raise OSError("Windows did not accept the resume key. Resume logging manually in CS2.")
        return True


class ResumeRequest:
    def __init__(self, monitor: LogOutputMonitor, key: str, clock=time.monotonic):
        self.monitor = monitor
        self.key = key
        self.clock = clock
        self.sent = False
        self.deadline = 0.0
        self.failed = False
        self.error = ""

    def step(self, game: GameWindow | None, sender: ResumeKeySender | None) -> str:
        if self.monitor.recording_ready():
            return "ready"
        if self.failed or not self.key or not self.monitor.resume_key_verified(self.key):
            return "manual"
        if self.sent:
            return "manual" if self.clock() >= self.deadline else "confirming"
        if game is None or sender is None:
            return "foreground"
        try:
            sent = sender.send(self.key, game, self.monitor.expect_command)
        except OSError as exc:
            self.failed = True
            self.error = str(exc)
            return "manual"
        if not sent:
            return "foreground"
        self.sent = True
        self.deadline = self.clock() + 10.0
        return "confirming"
