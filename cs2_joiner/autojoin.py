from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from .a2s import A2SClient, ServerAddress, ServerInfo
from .connection import ConsoleLogObserver
from .gsi import GsiConnectionObserver
from .slot_scan import SlotScanner, validate_scan_interval_ms


@dataclass(frozen=True)
class AutoJoinSettings:
    address: ServerAddress
    requests_per_second: int = 3
    threshold_players: int = 63
    timeout_seconds: float = 0.5
    retry_delay_seconds: float = 0.5
    result_timeout_seconds: float = 60.0
    auto_retry_enabled: bool = False
    scan_interval_ms: int | None = None
    gsi_retry_enabled: bool = False
    gsi_result_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        validate_requests_per_second(self.requests_per_second)
        if self.scan_interval_ms is not None:
            validate_scan_interval_ms(self.scan_interval_ms)
        if self.threshold_players < 0:
            raise ValueError("Threshold players must not be negative.")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive.")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("Retry delay must not be negative.")
        if not math.isfinite(self.result_timeout_seconds) or self.result_timeout_seconds <= 0:
            raise ValueError("Result timeout must be positive.")
        if not math.isfinite(self.gsi_result_timeout_seconds) or self.gsi_result_timeout_seconds <= 0:
            raise ValueError("GSI result timeout must be positive.")
        if self.auto_retry_enabled and self.gsi_retry_enabled:
            raise ValueError("Auto retry and Quick auto retry cannot be enabled together.")


@dataclass(frozen=True)
class AutoJoinEvent:
    kind: str
    message: str
    info: ServerInfo | None = None
    error: str = ""
    address: ServerAddress | None = None
    notify_sound: bool = False
    timings: dict[str, float] | None = None


def validate_requests_per_second(value: int) -> int:
    requests_per_second = int(value)
    if requests_per_second not in (1, 2, 3):
        raise ValueError("Requests per second must be 1, 2, or 3.")
    return requests_per_second


def should_join(info: ServerInfo, threshold_players: int = 63) -> bool:
    return 0 <= info.players < info.max_players and info.players <= threshold_players


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
    timeout_seconds: float = 0.5,
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
        kind="connect_requested",
        message=f"Steam connect opened for {address}: {info.players}/{info.max_players}.",
        info=info,
        notify_sound=True,
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
        connection_observer: ConsoleLogObserver | None = None,
        connection_ready: Callable[[], bool] | None = None,
        gsi_observer: GsiConnectionObserver | None = None,
    ):
        self._client = client
        self._join_server = join_server
        self._settings = settings
        self._on_event = on_event
        self._wait_for_next = wait_for_next
        self._connection_observer = connection_observer
        self._connection_ready = connection_ready
        self._gsi_observer = gsi_observer
        self._stop_event = threading.Event()
        self._retry_enabled = threading.Event()
        self.set_auto_retry_enabled(settings.auto_retry_enabled)
        self._gsi_retry_enabled = threading.Event()
        self.set_gsi_retry_enabled(settings.gsi_retry_enabled)
        self._retry_pending = False
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> ServerAddress:
        return self._settings.address

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("Auto join worker is already running.")
        self._stop_event.clear()
        self._retry_pending = False
        self._thread = threading.Thread(target=self._run, name="cs2-auto-join", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def set_auto_retry_enabled(self, enabled: bool) -> None:
        if enabled:
            self._retry_enabled.set()
        else:
            self._retry_enabled.clear()

    def _retry_was_disabled(self) -> bool:
        if self._retry_pending and not (self._retry_enabled.is_set() or self._gsi_retry_enabled.is_set()):
            self._emit("full", "Server full. Auto retry is OFF; no further connection will be sent.")
            return True
        return False

    def set_gsi_retry_enabled(self, enabled: bool) -> None:
        if enabled:
            self._gsi_retry_enabled.set()
        else:
            self._gsi_retry_enabled.clear()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.is_running

    def _run(self) -> None:
        scan_rate = (f"{self._settings.scan_interval_ms} ms scan interval"
                     if self._settings.scan_interval_ms is not None
                     else f"{self._settings.requests_per_second} req/sec")
        self._emit(
            "started",
            f"Auto join started for {self._settings.address} "
            f"({scan_rate}; auto retry {'ON' if self._gsi_retry_enabled.is_set() else 'OFF'}; "
            f"quick auto retry {'ON' if self._retry_enabled.is_set() else 'OFF'}).",
        )
        scanner = None
        try:
            while not self._stop_event.is_set():
                if not self._check_connection_ready():
                    break
                if self._retry_was_disabled():
                    break
                started_at = time.perf_counter()
                try:
                    if self._settings.scan_interval_ms is not None:
                        if scanner is None:
                            scanner = SlotScanner(self._client, self.address, self._settings.scan_interval_ms,
                                                  self._settings.timeout_seconds, self._stop_event)
                        result = scanner.next_result()
                        if result is None:
                            break
                        started_at, received_at, info = result
                    else:
                        info = self._client.query_info(self.address, timeout=self._settings.timeout_seconds)
                        received_at = time.perf_counter()
                except Exception as exc:  # Network errors should not kill the loop.
                    self._emit("error", "A2S query failed.", error=str(exc))
                else:
                    if self._stop_event.is_set():
                        break
                    if self._retry_was_disabled():
                        break
                    self._emit(
                        "info",
                        format_server_scan_message(info, self._settings.threshold_players),
                        info=info,
                        timings={"scan_ms": round((received_at - started_at) * 1000, 3)},
                    )
                    if should_join(info, threshold_players=self._settings.threshold_players):
                        if scanner is not None:
                            scanner.close()
                            scanner = None
                        self._emit("joining", f"Slot detected: {info.players}/{info.max_players}.", info=info)
                        if self._stop_event.is_set() or self._retry_was_disabled():
                            break
                        if not self._check_connection_ready():
                            break
                        observer = self._connection_observer if self._retry_enabled.is_set() else None
                        if observer is None and self._retry_enabled.is_set():
                            self._emit("unconfirmed", "CS2 connection log is unavailable. No connection was sent.")
                            break
                        if observer is not None:
                            try:
                                observer.begin(self.address)
                            except OSError as exc:
                                self._emit("tracking_error", "CS2 log could not be opened.", error=str(exc))
                                if self._retry_enabled.is_set():
                                    self._emit("unconfirmed", "CS2 logging could not be verified. No connection was sent.")
                                    break
                                observer = None
                        if self._stop_event.is_set() or self._retry_was_disabled():
                            break
                        gsi_tracking = self._gsi_retry_enabled.is_set()
                        if gsi_tracking:
                            if self._gsi_observer is None:
                                self._emit("unconfirmed", "GSI receiver is unavailable. No connection was sent.")
                                break
                            self._gsi_observer.begin(info.map_name)
                        if self._stop_event.is_set() or self._retry_was_disabled():
                            break
                        track_result = self._retry_enabled.is_set() or gsi_tracking
                        dispatch_at = time.perf_counter()
                        try:
                            self._join_server(self._settings.address)
                        except Exception as exc:
                            self._emit("launch_error", "Steam connect could not be opened.", error=str(exc))
                            break
                        detail = "waiting for CS2 confirmation." if track_result else "auto retry is OFF; retry manually if needed."
                        self._emit("connect_requested", f"Steam connect opened for {self.address}; {detail}",
                                   info=info, notify_sound=not track_result,
                                   timings={"scan_ms": round((received_at - started_at) * 1000, 3),
                                            "slot_to_dispatch_ms": round((dispatch_at - received_at) * 1000, 3),
                                            "dispatch_ms": round((time.perf_counter() - dispatch_at) * 1000, 3)})
                        if not track_result:
                            break
                        if gsi_tracking:
                            retry = self._await_gsi_connection(info, dispatch_at)
                        else:
                            retry = self._await_connection(observer, info)
                        if not retry:
                            break

                if self._settings.scan_interval_ms is None:
                    sleep_seconds = compute_sleep_seconds(self._settings.requests_per_second, started_at)
                    wait_for_next = self._wait_for_next or self._stop_event.wait
                    if wait_for_next(sleep_seconds):
                        break
        finally:
            if scanner is not None:
                scanner.close()
            self._emit("stopped", "Auto join stopped.")

    def _check_connection_ready(self) -> bool:
        if not self._retry_enabled.is_set() or self._connection_ready is None:
            return True
        try:
            ready = self._connection_ready()
        except Exception as exc:
            if not self._retry_enabled.is_set():
                return True
            self._emit("unconfirmed", "CS2 log output check failed; no connection was sent.", error=str(exc))
            return False
        if not self._retry_enabled.is_set():
            return True
        if not ready:
            self._emit("unconfirmed", "CS2 log output is paused or unknown. Run the resume command from Log output, then press JOIN again.")
        return ready

    def _await_gsi_connection(self, info: ServerInfo, dispatch_at: float) -> bool:
        deadline = dispatch_at + self._settings.gsi_result_timeout_seconds
        self._emit("tracking", f"Waiting up to {self._settings.gsi_result_timeout_seconds:g} seconds "
                   f"for fresh in-game GSI on {info.map_name}.")
        while not self._stop_event.is_set() and self._gsi_retry_enabled.is_set():
            if self._gsi_observer.poll(deadline):
                if self._stop_event.is_set() or not self._gsi_retry_enabled.is_set():
                    break
                self._emit("joined", "Fresh in-game GSI received for the target map. Auto join finished.",
                           info=info, notify_sound=True)
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                self._retry_pending = True
                if self._stop_event.is_set() or self._retry_was_disabled():
                    break
                self._emit("retrying", f"No matching in-game GSI within {self._settings.gsi_result_timeout_seconds:g} seconds; assuming rejection "
                           "and scanning for a fresh slot again.")
                return True
            self._stop_event.wait(min(0.05, remaining))
        if not self._stop_event.is_set() and not self._gsi_retry_enabled.is_set():
            self._emit("tracking_disabled", "Auto retry is OFF. No further connection will be sent.")
        return False

    def _await_connection(self, observer: ConsoleLogObserver | None, info: ServerInfo) -> bool:
        if observer is None:
            self._emit("unconfirmed", "Result unavailable: select CS2 console.log and start CS2 with -condebug. No automatic retry.")
            return False
        deadline = time.monotonic() + self._settings.result_timeout_seconds
        timed_out = False
        while not self._stop_event.is_set():
            if not self._retry_enabled.is_set():
                break
            try:
                result = observer.poll()
            except Exception as exc:
                if not self._retry_enabled.is_set():
                    break
                self._emit("unconfirmed", "Connection tracking failed. Automatic retry is paused.", error=str(exc))
                return False
            if self._stop_event.is_set():
                return False
            if not self._retry_enabled.is_set():
                break
            if result is not None:
                if result.kind == "full":
                    self._retry_pending = True
                    if self._retry_was_disabled():
                        return False
                    self._emit("retrying", "CS2 rejected the connection: server full. Waiting for a fresh slot.")
                    return not self._stop_event.wait(self._settings.retry_delay_seconds)
                self._emit(result.kind, result.message, info=info if result.kind == "joined" else None,
                           notify_sound=result.kind == "joined")
                return False
            if not timed_out and time.monotonic() >= deadline:
                self._emit("unconfirmed", "Connection result not yet confirmed. Still observing; no connection will be resent.")
                timed_out = True
            self._stop_event.wait(0.05)
        if not self._stop_event.is_set() and not self._retry_enabled.is_set():
            self._emit("tracking_disabled", "Quick auto retry is OFF. Connection confirmation stopped; retry manually if needed.")
        return False

    def _emit(self, kind: str, message: str, info: ServerInfo | None = None, error: str = "",
              *, notify_sound: bool = False, timings: dict[str, float] | None = None) -> None:
        self._on_event(AutoJoinEvent(kind=kind, message=message, info=info, error=error,
                                    address=self.address, notify_sound=notify_sound, timings=timings))
