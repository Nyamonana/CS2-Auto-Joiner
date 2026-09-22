from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import uuid
from typing import Callable


MAX_LOG_BYTES = 50 * 1024 * 1024
KEEP_LOG_BYTES = 5 * 1024 * 1024
QUIET_SECONDS = 30.0


@dataclass(frozen=True)
class CleanupResult:
    kind: str
    message: str = ""
    original_bytes: int = 0
    retained_bytes: int = 0


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def _kernel32():
    if os.name != "nt":
        raise OSError("Automatic log cleanup requires Windows process and file-lock checks.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def is_cs2_running() -> bool:
    kernel = _kernel32()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        function.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            if entry.szExeFile.casefold() == "cs2.exe":
                return True
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES is the only successful end.
                    raise ctypes.WinError(error)
                return False
    finally:
        kernel.CloseHandle(handle)


def validated_log_path(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("Linked console logs cannot be cleaned automatically.")
    resolved = path.resolve(strict=True)
    game = resolved.parent.parent
    if (resolved.name.casefold() != "console.log"
            or resolved.parent.name.casefold() != "csgo" or game.name.casefold() != "game"
            or not (game / "bin" / "win64" / "cs2.exe").is_file()):
        raise ValueError("Auto clean only accepts a CS2 game/csgo/console.log beside a valid game/bin/win64/cs2.exe path.")
    _validate_stat(resolved.stat())
    return resolved


def _validate_stat(value: os.stat_result) -> None:
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or getattr(value, "st_file_attributes", 0) & 0x400):
        raise ValueError("Auto clean requires a regular, unlinked console.log file.")


def _signature(value: os.stat_result) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


@contextmanager
def _open_without_writers(path: Path):
    import msvcrt

    kernel = _kernel32()
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    # Deny write sharing, allow read/delete so atomic replacement can commit
    # while this handle still protects the original from concurrent writers.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x5, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    try:
        stream = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def _replace_log(path: Path, replacement: Path) -> str:
    kernel = _kernel32()
    kernel.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    kernel.ReplaceFileW.restype = wintypes.BOOL
    backup = path.with_name(f".zejoiner-log-{uuid.uuid4().hex}.bak")
    # ReplaceFile accepts the read/delete sharing of our protected source.
    # Keep recovery files on failure, including partially completed replacement.
    if not kernel.ReplaceFileW(str(path), str(replacement), str(backup), 0, None, None):
        error = ctypes.WinError(ctypes.get_last_error())
        raise OSError(f"{error}; recovery files, if created, are retained at {backup} and {replacement}")
    try:
        backup.unlink(missing_ok=True)
    except OSError:
        return f" Old log backup could not be removed: {backup}"
    return ""


class LogCleanupManager:
    """Called by one background job at a time, only while JOIN is stopped."""

    def __init__(self, process_running: Callable[[], bool] = is_cs2_running,
                 clock: Callable[[], float] = time.monotonic,
                 max_bytes: int = MAX_LOG_BYTES, keep_bytes: int = KEEP_LOG_BYTES,
                 quiet_seconds: float = QUIET_SECONDS):
        if not 0 < keep_bytes < max_bytes or quiet_seconds < 0:
            raise ValueError("Invalid log cleanup limits.")
        self.process_running = process_running
        self.clock = clock
        self.max_bytes = max_bytes
        self.keep_bytes = keep_bytes
        self.quiet_seconds = quiet_seconds
        self._candidate = None
        self._blocked_candidate = None
        self._stable_since = 0.0

    def check(self, path: Path, cancelled: threading.Event, generation: int = 0) -> CleanupResult:
        try:
            if cancelled.is_set():
                self._candidate = None
                return CleanupResult("cancelled")
            if self.process_running() is not False:
                self._candidate = None
                return CleanupResult("running", "Auto clean deferred: CS2 is running or its state is unknown.")
            path = validated_log_path(path)
            status = path.stat()
            if status.st_size <= self.max_bytes:
                self._candidate = None
                return CleanupResult("small")
            signature = _signature(status)
            candidate = path, signature, generation
            if candidate == self._blocked_candidate:
                return CleanupResult("blocked")
            now = self.clock()
            if candidate != self._candidate:
                self._candidate = candidate
                self._stable_since = now
            if now - self._stable_since < self.quiet_seconds:
                return CleanupResult("waiting", "Auto clean waiting for CS2 to stay stopped and the log to remain unchanged for 30 seconds.")
            result = self._compact(path, signature, cancelled)
            self._candidate = None
            return result
        except (OSError, ValueError) as exc:
            self._blocked_candidate = self._candidate
            self._candidate = None
            return CleanupResult("error", f"Auto clean skipped: {exc}")

    def _compact(self, path: Path, expected: tuple, cancelled: threading.Event) -> CleanupResult:
        temporary = None
        try:
            with _open_without_writers(path) as source:
                status = os.fstat(source.fileno())
                _validate_stat(status)
                if _signature(status) != expected:
                    return CleanupResult("changed", "Auto clean deferred: log changed during preparation.")
                if self.process_running() is not False or cancelled.is_set():
                    return CleanupResult("cancelled")
                start = status.st_size - self.keep_bytes
                source.seek(start - 1)
                boundary = source.read(1) == b"\n"
                data = source.read(self.keep_bytes)
                if not boundary:
                    newline = data.find(b"\n")
                    if newline < 0:
                        return CleanupResult("no_lines", "Auto clean skipped: no complete line boundary within the retained tail.")
                    data = data[newline + 1:]
                if not data:
                    return CleanupResult("no_lines", "Auto clean skipped: retained tail would be empty.")
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".zejoiner-log-", suffix=".tmp", delete=False) as target:
                    temporary = Path(target.name)
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                if self.process_running() is not False or cancelled.is_set():
                    return CleanupResult("cancelled")
                if validated_log_path(path) != path or _signature(path.stat()) != expected:
                    return CleanupResult("changed", "Auto clean deferred: log was replaced during preparation.")
                replacement = temporary
                temporary = None
                warning = _replace_log(path, replacement)
                return CleanupResult("cleaned", f"CS2 log cleaned: {status.st_size / 1048576:.1f} MiB -> {len(data) / 1048576:.1f} MiB.{warning}", status.st_size, len(data))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
