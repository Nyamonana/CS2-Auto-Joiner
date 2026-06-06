from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from .a2s import A2SClient, ServerAddress, ServerInfo


@dataclass(frozen=True)
class AutoJoinSettings:
    address: ServerAddress
    requests_per_second: int
    threshold_players: int = 63
    timeout_seconds: float = 0.15

    def __post_init__(self) -> None:
        validate_requests_per_second(self.requests_per_second)
        if self.threshold_players < 0:
            raise ValueError("Threshold players must not be negative.")
        if self.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive.")


@dataclass(frozen=True)
class AutoJoinEvent:
    kind: str
    message: str
    info: ServerInfo | None = None
    error: str = ""


def validate_requests_per_second(value: int) -> int:
    requests_per_second = int(value)
    if requests_per_second not in (1, 2, 3):
        raise ValueError("Requests per second must be 1, 2, or 3.")
    return requests_per_second


def should_join(info: ServerInfo, threshold_players: int = 63) -> bool:
    return info.players <= threshold_players


def format_server_scan_message(info: ServerInfo, threshold_players: int = 63) -> str:
    if info.players >= info.max_players:
        status = "FULL"
    elif should_join(info, threshold_players=threshold_players):
        status = "OPEN"
    else:
        status = "WAIT"

    return (
        f"Scan {status}: {info.players}/{info.max_players} "
        f"on {info.map_name} ({info.ping_ms:.1f} ms)"
    )


def safe_join_once(
    client: A2SClient,
    join_server: Callable[[ServerAddress], None],
    address: ServerAddress,
    threshold_players: int = 63,
    timeout_seconds: float = 0.15,
) -> AutoJoinEvent:
    info = client.query_info(address, timeout=timeout_seconds)
    if not should_join(info, threshold_players=threshold_players):
        return AutoJoinEvent(
            kind="full",
            message=f"Server is full: {info.players}/{info.max_players}. Join skipped.",
            info=info,
        )

    join_server(address)
    return AutoJoinEvent(
        kind="joined",
        message=f"Steam connect opened for {address}: {info.players}/{info.max_players}.",
        info=info,
    )


def compute_sleep_seconds(requests_per_second: int, started_at: float, now: float | None = None) -> float:
    current = time.perf_counter() if now is None else now
    elapsed = current - started_at
    interval = 1.0 / float(validate_requests_per_second(requests_per_second))
    return max(0.0, interval - elapsed)


class AutoJoinWorker:
    def __init__(
        self,
        client: A2SClient,
        join_server: Callable[[ServerAddress], None],
        settings: AutoJoinSettings,
        on_event: Callable[[AutoJoinEvent], None],
        wait_for_next: Callable[[float], bool] | None = None,
    ):
        self._client = client
        self._join_server = join_server
        self._settings = settings
        self._on_event = on_event
        self._wait_for_next = wait_for_next
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("Auto join worker is already running.")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="cs2-auto-join", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        self._emit(
            "started",
            f"Auto join started for {self._settings.address} "
            f"({self._settings.requests_per_second} req/sec).",
        )
        try:
            while not self._stop_event.is_set():
                started_at = time.perf_counter()
                try:
                    info = self._client.query_info(
                        self._settings.address,
                        timeout=self._settings.timeout_seconds,
                    )
                except Exception as exc:  # Network errors should not kill the loop.
                    self._emit("error", "A2S query failed.", error=str(exc))
                else:
                    self._emit(
                        "info",
                        format_server_scan_message(info, self._settings.threshold_players),
                        info=info,
                    )
                    if should_join(info, threshold_players=self._settings.threshold_players):
                        self._emit("joining", f"Slot detected: {info.players}/{info.max_players}.", info=info)
                        self._join_server(self._settings.address)
                        self._emit("joined", f"Steam connect opened for {self._settings.address}.", info=info)
                        break

                sleep_seconds = compute_sleep_seconds(self._settings.requests_per_second, started_at)
                wait_for_next = self._wait_for_next or self._stop_event.wait
                if wait_for_next(sleep_seconds):
                    break
        finally:
            self._emit("stopped", "Auto join stopped.")

    def _emit(self, kind: str, message: str, info: ServerInfo | None = None, error: str = "") -> None:
        self._on_event(AutoJoinEvent(kind=kind, message=message, info=info, error=error))
