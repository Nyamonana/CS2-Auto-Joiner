from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
import time

from .a2s import A2SClient, ServerAddress, ServerInfo


SCAN_INTERVALS_MS = (100, 150, 200)


def validate_scan_interval_ms(value: int) -> int:
    if value not in SCAN_INTERVALS_MS:
        raise ValueError("Scan interval must be 100, 150, or 200 ms.")
    return int(value)


class SlotScanner:
    """Pace queries across two workers; only the coordinator can request a join."""

    def __init__(self, client: A2SClient, address: ServerAddress, interval_ms: int,
                 timeout: float, stop_event: threading.Event):
        self._client = client
        self._address = address
        self._interval = validate_scan_interval_ms(interval_ms) / 1000.0
        self._timeout = timeout
        self._stop_event = stop_event
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cs2-slot-scan")
        self._pending: dict[Future, int] = {}
        self._next_query_at = 0.0
        self._sequence = 0
        self._latest_result = 0
        self._closed = False

    def next_result(self) -> tuple[float, float, ServerInfo] | None:
        while not self._closed and not self._stop_event.is_set():
            completed = [future for future in self._pending if future.done()]
            if completed:
                future = max(completed, key=self._pending.__getitem__)
                sequence = self._pending.pop(future)
                # A delayed older OPEN must not override a newer FULL response.
                if sequence <= self._latest_result:
                    continue
                self._latest_result = sequence
                return future.result()

            now = time.perf_counter()
            if now >= self._next_query_at and len(self._pending) < 2:
                self._sequence += 1
                self._pending[self._executor.submit(self._query)] = self._sequence
                # Skip missed slots instead of sending a catch-up burst.
                self._next_query_at = now + self._interval
                continue

            delay = 0.02
            if len(self._pending) < 2:
                delay = min(delay, max(0.0, self._next_query_at - now))
            if self._pending:
                wait(self._pending, timeout=delay, return_when=FIRST_COMPLETED)
            else:
                self._stop_event.wait(delay)
        return None

    def _query(self) -> tuple[float, float, ServerInfo]:
        started_at = time.perf_counter()
        info = self._client.query_info(self._address, timeout=self._timeout)
        return started_at, time.perf_counter(), info

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._pending.clear()
