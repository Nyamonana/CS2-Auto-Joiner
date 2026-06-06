from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from .a2s import A2SClient, ServerAddress, ServerInfo


@dataclass(frozen=True)
class ServerMonitorSettings:
    address: ServerAddress
    interval_seconds: float = 10.0
    timeout_seconds: float = 0.15

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("Interval must be positive.")
        if self.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive.")


@dataclass(frozen=True)
class ServerMonitorEvent:
    kind: str
    message: str
    info: ServerInfo | None = None
    error: str = ""


def format_monitor_message(info: ServerInfo) -> str:
    return f"Monitor: {info.players}/{info.max_players} on {info.map_name} ({info.ping_ms:.1f} ms)"


class ServerMonitorWorker:
    def __init__(
        self,
        client: A2SClient,
        settings: ServerMonitorSettings,
        on_event: Callable[[ServerMonitorEvent], None],
        wait_for_next: Callable[[float], bool] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._on_event = on_event
        self._wait_for_next = wait_for_next
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> ServerAddress:
        return self._settings.address

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("Server monitor worker is already running.")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="server-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        self._emit("started", f"Server monitor started for {self._settings.address}.")
        try:
            while not self._stop_event.is_set():
                started_at = time.perf_counter()
                try:
                    info = self._client.query_info(
                        self._settings.address,
                        timeout=self._settings.timeout_seconds,
                    )
                except Exception as exc:
                    self._emit("error", "Server monitor A2S query failed.", error=str(exc))
                else:
                    self._emit("info", format_monitor_message(info), info=info)

                sleep_seconds = compute_sleep_seconds(self._settings.interval_seconds, started_at)
                wait_for_next = self._wait_for_next or self._stop_event.wait
                if wait_for_next(sleep_seconds):
                    break
        finally:
            self._emit("stopped", "Server monitor stopped.")

    def _emit(self, kind: str, message: str, info: ServerInfo | None = None, error: str = "") -> None:
        self._on_event(ServerMonitorEvent(kind=kind, message=message, info=info, error=error))


def compute_sleep_seconds(interval_seconds: float, started_at: float, now: float | None = None) -> float:
    current = time.perf_counter() if now is None else now
    elapsed = current - started_at
    return max(0.0, interval_seconds - elapsed)
