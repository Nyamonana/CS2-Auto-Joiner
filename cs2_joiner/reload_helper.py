from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
import ctypes
from ctypes import wintypes
import sys
import threading
import time
from typing import Protocol


VK_RBUTTON = 0x02
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
DEFAULT_RELOAD_HELPER_CONFIG = """[Input]
ReloadKey=F

[Target]
ProcessName=cs2.exe
WindowTitle=
UsePidFilter=0
Pid=0

[Timing]
DelayBeforeFMs=50
FHoldMs=8
DelayAfterFMs=10

[Behavior]
EnabledByDefault=1
SuppressAfterRestoreMs=250
"""


@dataclass(frozen=True)
class KeyBinding:
    name: str
    vk_code: int


@dataclass(frozen=True)
class ReloadHelperSettings:
    reload_key: KeyBinding
    target_process_name: str = "cs2.exe"
    target_window_title: str = ""
    use_pid_filter: bool = False
    pid: int = 0
    delay_before_key_ms: int = 50
    key_hold_ms: int = 8
    delay_after_key_ms: int = 10
    suppress_after_restore_ms: int = 250
    enabled: bool = True


def ensure_reload_helper_config(path: str | Path) -> Path:
    config_path = Path(path)
    if config_path.exists():
        return config_path

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(DEFAULT_RELOAD_HELPER_CONFIG, encoding="utf-8")
    return config_path


def load_reload_helper_settings(
    path: str | Path,
    reload_key_override: str | None = None,
) -> ReloadHelperSettings:
    config_path = ensure_reload_helper_config(path)
    parser = _read_config(config_path)

    reload_key_text = reload_key_override or _read_string(parser, "Input", "ReloadKey", "F")
    return ReloadHelperSettings(
        reload_key=parse_key_binding(reload_key_text),
        target_process_name=_read_string(parser, "Target", "ProcessName", "cs2.exe"),
        target_window_title=_read_string(parser, "Target", "WindowTitle", ""),
        use_pid_filter=_read_bool(parser, "Target", "UsePidFilter", False),
        pid=_read_int(parser, "Target", "Pid", 0, 0, 999999),
        delay_before_key_ms=_read_int(parser, "Timing", "DelayBeforeFMs", 50, 0, 1000),
        key_hold_ms=_read_int(parser, "Timing", "FHoldMs", 8, 1, 1000),
        delay_after_key_ms=_read_int(parser, "Timing", "DelayAfterFMs", 10, 0, 1000),
        suppress_after_restore_ms=_read_int(parser, "Behavior", "SuppressAfterRestoreMs", 250, 0, 1000),
        enabled=_read_bool(parser, "Behavior", "EnabledByDefault", True),
    )


def save_reload_helper_key(path: str | Path, key: KeyBinding) -> None:
    config_path = ensure_reload_helper_config(path)
    parser = _read_config(config_path)
    if not parser.has_section("Input"):
        parser.add_section("Input")
    parser.set("Input", "ReloadKey", key.name)
    with config_path.open("w", encoding="utf-8") as file:
        parser.write(file)


def _read_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def _read_string(parser: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    try:
        return parser.get(section, key, fallback=default).strip()
    except (configparser.Error, AttributeError):
        return default


def _read_int(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    text = _read_string(parser, section, key, str(default))
    try:
        value = int(text)
    except ValueError:
        value = default
    return min(max(value, min_value), max_value)


def _read_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    text = _read_string(parser, section, key, "1" if default else "0").lower()
    return text in {"1", "true", "yes", "on"}


class PhysicalInputState:
    def __init__(self):
        self._right_button_down = False
        self._keys_down: set[int] = set()
        self._lock = threading.RLock()

    def update_right_button(self, is_down: bool, injected: bool) -> None:
        if injected:
            return
        with self._lock:
            self._right_button_down = is_down

    def set_right_button_snapshot(self, is_down: bool) -> None:
        with self._lock:
            self._right_button_down = is_down

    def is_right_button_down(self) -> bool:
        with self._lock:
            return self._right_button_down

    def update_key(self, vk_code: int, is_down: bool, injected: bool) -> None:
        if injected:
            return
        with self._lock:
            if is_down:
                self._keys_down.add(vk_code)
            else:
                self._keys_down.discard(vk_code)

    def set_key_snapshot(self, vk_code: int, is_down: bool) -> None:
        with self._lock:
            if is_down:
                self._keys_down.add(vk_code)
            else:
                self._keys_down.discard(vk_code)

    def is_key_down(self, key: KeyBinding) -> bool:
        with self._lock:
            return key.vk_code in self._keys_down


class ReloadHelperDriver(Protocol):
    def is_target_active(self) -> bool:
        ...

    def is_physical_right_down(self) -> bool:
        ...

    def is_physical_key_down(self, key: KeyBinding) -> bool:
        ...

    def send_right_up(self) -> None:
        ...

    def send_right_down(self) -> None:
        ...

    def send_key_down(self, key: KeyBinding) -> None:
        ...

    def send_key_up(self, key: KeyBinding) -> None:
        ...

    def sleep_ms(self, milliseconds: int) -> None:
        ...

    def monotonic_ms(self) -> int:
        ...


class ReloadHelperController:
    def __init__(self, settings: ReloadHelperSettings, driver: ReloadHelperDriver, run_async: bool = False):
        self.settings = settings
        self.driver = driver
        self.run_async = run_async
        self.is_enabled = settings.enabled
        self.is_sequence_running = False
        self.is_reload_physically_down = False
        self.script_restored_right_button_down = False
        self.last_right_restore_tick = 0
        self._sequence_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def handle_reload_key_down(self, vk_code: int) -> bool:
        if not self._should_handle(vk_code):
            return False

        with self._lock:
            if self.is_reload_physically_down:
                return True

            self.is_reload_physically_down = True
            if self.is_sequence_running or self._should_suppress_rapid_press():
                return True

            self._start_sequence_locked()
            return True

    def handle_reload_key_up(self, vk_code: int) -> bool:
        if not self._should_handle(vk_code):
            return False

        with self._lock:
            self.is_reload_physically_down = False
            return True

    def watch_physical_right_release(self) -> None:
        with self._lock:
            if not self.script_restored_right_button_down:
                return
            if self.driver.is_physical_right_down():
                return

            self.driver.send_right_up()
            self._mark_script_right_button_released()

    def watch_reload_key_release(self) -> None:
        with self._lock:
            if not self.is_reload_physically_down:
                return
            if self.driver.is_physical_key_down(self.settings.reload_key):
                return
            self.is_reload_physically_down = False

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.is_enabled = enabled
            if not enabled:
                self._mark_script_right_button_released()

    def _should_handle(self, vk_code: int) -> bool:
        return (
            self.is_enabled
            and vk_code == self.settings.reload_key.vk_code
            and self.driver.is_target_active()
        )

    def _start_sequence_locked(self) -> None:
        if self.is_sequence_running:
            return

        self.is_sequence_running = True
        if self.run_async:
            self._sequence_thread = threading.Thread(
                target=self._run_sequence_worker,
                name="cs2-reload-helper-sequence",
                daemon=True,
            )
            self._sequence_thread.start()
            return

        try:
            self._run_one_sequence()
        finally:
            self.is_sequence_running = False

    def _run_sequence_worker(self) -> None:
        try:
            self._run_one_sequence()
        finally:
            with self._lock:
                self.is_sequence_running = False

    def _run_one_sequence(self) -> None:
        if not self.driver.is_target_active():
            return

        if self.driver.is_physical_right_down():
            self.driver.send_right_up()
            self._mark_script_right_button_released()
            self.driver.sleep_ms(self.settings.delay_before_key_ms)

            if not self.driver.is_target_active():
                return

            self._send_reload_key()
            self.driver.sleep_ms(self.settings.delay_after_key_ms)
            self._restore_right_button_if_still_physical_down()
            return

        self._send_reload_key()

    def _send_reload_key(self) -> None:
        if not self.driver.is_target_active():
            return

        self.driver.send_key_down(self.settings.reload_key)
        self.driver.sleep_ms(self.settings.key_hold_ms)
        self.driver.send_key_up(self.settings.reload_key)

    def _restore_right_button_if_still_physical_down(self) -> None:
        if not self.driver.is_target_active() or not self.driver.is_physical_right_down():
            return

        self.driver.send_right_down()
        self.script_restored_right_button_down = True
        self.last_right_restore_tick = self.driver.monotonic_ms()

    def _should_suppress_rapid_press(self) -> bool:
        return self.driver.is_physical_right_down() and self._right_restore_cooldown_active()

    def _right_restore_cooldown_active(self) -> bool:
        if self.settings.suppress_after_restore_ms <= 0 or self.last_right_restore_tick <= 0:
            return False
        return self.driver.monotonic_ms() - self.last_right_restore_tick < self.settings.suppress_after_restore_ms

    def _mark_script_right_button_released(self) -> None:
        self.script_restored_right_button_down = False


def parse_key_binding(value: str) -> KeyBinding:
    text = value.strip()
    if not text:
        raise ValueError("Reload key is empty.")

    lowered = text.lower()
    if len(text) == 1 and text.isalnum():
        name = text.upper()
        return KeyBinding(name=name, vk_code=ord(name))

    if lowered.startswith("f") and lowered[1:].isdigit():
        number = int(lowered[1:])
        if 1 <= number <= 24:
            return KeyBinding(name=f"F{number}", vk_code=0x70 + number - 1)

    aliases = {
        "backspace": ("BACKSPACE", 0x08),
        "tab": ("TAB", 0x09),
        "enter": ("ENTER", 0x0D),
        "return": ("ENTER", 0x0D),
        "shift": ("SHIFT", 0x10),
        "ctrl": ("CTRL", 0x11),
        "control": ("CTRL", 0x11),
        "alt": ("ALT", 0x12),
        "pause": ("PAUSE", 0x13),
        "capslock": ("CAPSLOCK", 0x14),
        "caps lock": ("CAPSLOCK", 0x14),
        "esc": ("ESC", 0x1B),
        "escape": ("ESC", 0x1B),
        "space": ("SPACE", 0x20),
        "pageup": ("PAGEUP", 0x21),
        "page up": ("PAGEUP", 0x21),
        "pagedown": ("PAGEDOWN", 0x22),
        "page down": ("PAGEDOWN", 0x22),
        "end": ("END", 0x23),
        "home": ("HOME", 0x24),
        "left": ("LEFT", 0x25),
        "up": ("UP", 0x26),
        "right": ("RIGHT", 0x27),
        "down": ("DOWN", 0x28),
        "insert": ("INSERT", 0x2D),
        "delete": ("DELETE", 0x2E),
        "del": ("DELETE", 0x2E),
    }
    if lowered in aliases:
        name, vk_code = aliases[lowered]
        return KeyBinding(name=name, vk_code=vk_code)

    if lowered.startswith("vk") and len(lowered) > 2:
        try:
            vk_code = int(lowered[2:], 16)
        except ValueError as exc:
            raise ValueError(f"Unsupported reload key: {value}") from exc
        if 1 <= vk_code <= 0xFE:
            return KeyBinding(name=f"VK{vk_code:02X}", vk_code=vk_code)

    raise ValueError(f"Unsupported reload key: {value}")


class WindowsInputDriver:
    def __init__(self, settings: ReloadHelperSettings, physical_state: PhysicalInputState):
        if not sys.platform.startswith("win"):
            raise RuntimeError("Right click reload helper is only available on Windows.")
        self.settings = settings
        self.physical_state = physical_state
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self._configure_api()

    def _configure_api(self) -> None:
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def is_target_active(self) -> bool:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return False

        if self.settings.target_window_title:
            if self.settings.target_window_title.lower() not in self._window_title(hwnd).lower():
                return False

        pid = self._window_pid(hwnd)
        if self.settings.use_pid_filter and self.settings.pid > 0 and pid != self.settings.pid:
            return False

        if self.settings.target_process_name:
            process_name = self._process_name(pid)
            if process_name.lower() != self.settings.target_process_name.lower():
                return False

        return True

    def is_physical_right_down(self) -> bool:
        return self.physical_state.is_right_button_down()

    def is_physical_key_down(self, key: KeyBinding) -> bool:
        return self.physical_state.is_key_down(key)

    def sync_physical_snapshot(self) -> None:
        self.physical_state.set_right_button_snapshot(bool(self.user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000))
        self.physical_state.set_key_snapshot(
            self.settings.reload_key.vk_code,
            bool(self.user32.GetAsyncKeyState(self.settings.reload_key.vk_code) & 0x8000),
        )

    def send_right_up(self) -> None:
        self._send_mouse(MOUSEEVENTF_RIGHTUP)

    def send_right_down(self) -> None:
        self._send_mouse(MOUSEEVENTF_RIGHTDOWN)

    def send_key_down(self, key: KeyBinding) -> None:
        self._send_key(key.vk_code, 0)

    def send_key_up(self, key: KeyBinding) -> None:
        self._send_key(key.vk_code, KEYEVENTF_KEYUP)

    def sleep_ms(self, milliseconds: int) -> None:
        if milliseconds > 0:
            time.sleep(milliseconds / 1000.0)

    def monotonic_ms(self) -> int:
        return int(time.perf_counter() * 1000)

    def _window_title(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _window_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _process_name(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            if not ok:
                return ""
            return Path(buffer.value).name
        finally:
            self.kernel32.CloseHandle(handle)

    def _send_key(self, vk_code: int, flags: int) -> None:
        input_data = INPUT()
        input_data.type = INPUT_KEYBOARD
        input_data.union.ki = KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        self._send_input(input_data)

    def _send_mouse(self, flags: int) -> None:
        input_data = INPUT()
        input_data.type = INPUT_MOUSE
        input_data.union.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
        self._send_input(input_data)

    def _send_input(self, input_data) -> None:
        sent = self.user32.SendInput(1, ctypes.byref(input_data), ctypes.sizeof(INPUT))
        if sent != 1:
            raise ctypes.WinError()


class WindowsReloadHelperService:
    def __init__(self, settings: ReloadHelperSettings):
        self.settings = settings
        self.physical_state = PhysicalInputState()
        self.driver = WindowsInputDriver(settings, self.physical_state)
        self.controller = ReloadHelperController(settings, self.driver, run_async=True)
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._winmm = ctypes.windll.winmm
        self._configure_api()
        self._running = threading.Event()
        self._started = threading.Event()
        self._hook_thread_id = 0
        self._keyboard_hook_handle = None
        self._mouse_hook_handle = None
        self._keyboard_hook_proc = None
        self._mouse_hook_proc = None
        self._hook_thread: threading.Thread | None = None
        self._watch_thread: threading.Thread | None = None
        self._startup_error = ""

    def _configure_api(self) -> None:
        self._user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HookProc, wintypes.HINSTANCE, wintypes.DWORD]
        self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.CallNextHookEx.restype = LRESULT
        self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        self._user32.TranslateMessage.restype = wintypes.BOOL
        self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        self._user32.DispatchMessageW.restype = LRESULT
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
        self._winmm.timeEndPeriod.argtypes = [wintypes.UINT]

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        if self.is_running:
            return
        self._running.set()
        self._started.clear()
        self._startup_error = ""
        self.driver.sync_physical_snapshot()
        self._winmm.timeBeginPeriod(1)
        self._hook_thread = threading.Thread(target=self._input_hook_loop, name="cs2-reload-helper-hook", daemon=True)
        self._watch_thread = threading.Thread(target=self._right_release_watch_loop, name="cs2-reload-helper-watch", daemon=True)
        self._hook_thread.start()
        self._watch_thread.start()
        if not self._started.wait(timeout=2.0):
            self.stop()
            raise RuntimeError("Failed to start input hooks.")
        if self._startup_error:
            self._winmm.timeEndPeriod(1)
            raise RuntimeError(self._startup_error)

    def stop(self) -> None:
        if not self.is_running:
            return
        self._running.clear()
        self.controller.set_enabled(False)
        if self._hook_thread_id:
            self._user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        if self._hook_thread is not None:
            self._hook_thread.join(timeout=1.0)
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.0)
        self._winmm.timeEndPeriod(1)
        self._hook_thread = None
        self._watch_thread = None

    def _input_hook_loop(self) -> None:
        self._hook_thread_id = self._kernel32.GetCurrentThreadId()
        self._keyboard_hook_proc = HookProc(self._keyboard_hook_callback)
        self._mouse_hook_proc = HookProc(self._mouse_hook_callback)
        self._keyboard_hook_handle = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_hook_proc, None, 0)
        self._mouse_hook_handle = self._user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_hook_proc, None, 0)
        if not self._keyboard_hook_handle or not self._mouse_hook_handle:
            self._startup_error = "Failed to start keyboard/mouse hooks."
            if self._keyboard_hook_handle:
                self._user32.UnhookWindowsHookEx(self._keyboard_hook_handle)
                self._keyboard_hook_handle = None
            if self._mouse_hook_handle:
                self._user32.UnhookWindowsHookEx(self._mouse_hook_handle)
                self._mouse_hook_handle = None
            self._running.clear()
            self._started.set()
            return

        self._started.set()
        message = MSG()
        try:
            while self._running.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self._keyboard_hook_handle:
                self._user32.UnhookWindowsHookEx(self._keyboard_hook_handle)
                self._keyboard_hook_handle = None
            if self._mouse_hook_handle:
                self._user32.UnhookWindowsHookEx(self._mouse_hook_handle)
                self._mouse_hook_handle = None

    def _keyboard_hook_callback(self, n_code, w_param, l_param):
        if n_code < 0:
            return self._user32.CallNextHookEx(self._keyboard_hook_handle, n_code, w_param, l_param)

        event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        injected = bool(event.flags & LLKHF_INJECTED)
        if injected:
            return self._user32.CallNextHookEx(self._keyboard_hook_handle, n_code, w_param, l_param)

        handled = False
        if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self.physical_state.update_key(event.vkCode, is_down=True, injected=injected)
            handled = self.controller.handle_reload_key_down(event.vkCode)
        elif w_param in (WM_KEYUP, WM_SYSKEYUP):
            self.physical_state.update_key(event.vkCode, is_down=False, injected=injected)
            handled = self.controller.handle_reload_key_up(event.vkCode)

        if handled:
            return 1
        return self._user32.CallNextHookEx(self._keyboard_hook_handle, n_code, w_param, l_param)

    def _mouse_hook_callback(self, n_code, w_param, l_param):
        if n_code < 0:
            return self._user32.CallNextHookEx(self._mouse_hook_handle, n_code, w_param, l_param)

        event = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        injected = bool(event.flags & LLMHF_INJECTED)
        if w_param == WM_RBUTTONDOWN:
            self.physical_state.update_right_button(is_down=True, injected=injected)
        elif w_param == WM_RBUTTONUP:
            self.physical_state.update_right_button(is_down=False, injected=injected)

        return self._user32.CallNextHookEx(self._mouse_hook_handle, n_code, w_param, l_param)

    def _right_release_watch_loop(self) -> None:
        while self._running.is_set():
            self.controller.watch_reload_key_release()
            self.controller.watch_physical_right_release()
            time.sleep(0.01)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("union", INPUTUNION),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", POINT),
    ]


HookProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
