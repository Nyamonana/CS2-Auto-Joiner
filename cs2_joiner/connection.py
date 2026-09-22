from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re

from .a2s import ServerAddress


@dataclass(frozen=True)
class ConnectionResult:
    kind: str
    message: str


class ConnectionLogParser:
    """Recognize endpoint-scoped CS2 client events, never UI or loopback state."""

    def __init__(self, address: ServerAddress):
        self.address = str(address).casefold()
        self.started = False
        self.finished = False

    def feed(self, line: str) -> ConnectionResult | None:
        if self.finished:
            return None
        request = re.search(r"\[HostStateManager\] CHostStateMgr::QueueNewRequest\( Remote Connect \(([^)]+)\)", line)
        if request:
            if request[1].casefold() != self.address:
                return self._finish("cancelled", "A different server connection was requested. Auto join stopped.")
            self.started = True
        failure = re.search(r"\[Client\] Disconnected from server: (NETWORK_DISCONNECT_[A-Z_]+) \(Failed to connect to ([^ ]+)\. ", line)
        if failure and self.started and failure[2].casefold() == self.address:
            kind = "full" if failure[1] == "NETWORK_DISCONNECT_REJECT_SERVERFULL" else "failed"
            return self._finish(kind, failure[1])
        success = re.search(r"\[HostStateManager\] Host activate: Remote Connect \(([^)]+)\)", line)
        if success and self.started and success[1].casefold() == self.address:
            return self._finish("joined", f"Joined {self.address} (confirmed by CS2).")
        if self.started and "[Client] Disconnecting from server: NETWORK_DISCONNECT_DISCONNECT_BY_USER" in line:
            return self._finish("cancelled", "Connection cancelled in CS2.")
        return None

    def _finish(self, kind: str, message: str) -> ConnectionResult:
        self.finished = True
        return ConnectionResult(kind, message)


class ConsoleLogObserver:
    def __init__(self, path: Path):
        self.path = path
        self._offset = 0
        self._identity = None
        self._buffer = b""
        self._parser: ConnectionLogParser | None = None
        self._skip_partial = False

    def begin(self, address: ServerAddress) -> None:
        # Bookmark before launching Steam; old attempts must never trigger a retry.
        with self.path.open("rb") as handle:
            handle.seek(0, 2)
            self._offset = handle.tell()
            self._skip_partial = False
            if self._offset:
                handle.seek(-1, 2)
                self._skip_partial = handle.read(1) != b"\n"
            stat = os.fstat(handle.fileno())
        self._identity = (stat.st_dev, stat.st_ino)
        self._buffer = b""
        self._parser = ConnectionLogParser(address)

    def poll(self) -> ConnectionResult | None:
        if self._parser is None:
            return None
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                if (stat.st_dev, stat.st_ino) != self._identity or stat.st_size < self._offset:
                    self._parser = None
                    return ConnectionResult("unconfirmed", "CS2 log was replaced or truncated; automatic retry is paused.")
                handle.seek(self._offset)
                data = handle.read(65536)
                self._offset = handle.tell()
        except OSError as exc:
            self._parser = None
            return ConnectionResult("unconfirmed", f"CS2 log unavailable; automatic retry is paused: {exc}")
        self._buffer += data
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        for line in lines:
            if self._skip_partial:
                self._skip_partial = False
                continue
            result = self._parser.feed(line.decode("utf-8", errors="replace"))
            if result is not None:
                return result
        if len(self._buffer) > 65536:
            self._buffer = b""
            self._skip_partial = True
        return None
