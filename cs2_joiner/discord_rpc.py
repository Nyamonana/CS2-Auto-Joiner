from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import struct
import threading
from typing import Callable, Iterable
from uuid import uuid4

from .a2s import ServerInfo
from .config_io import read_config, write_config
from .debug_log import DebugLogger, mask_client_id
from .gsi import GsiSnapshot


OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4
DISCORD_CONFIG_SECTION = "Discord"
DISCORD_ACTIVITY_TEXT_LIMIT = 128


@dataclass(frozen=True)
class DiscordRpcConfig:
    client_id: str = ""
    enabled: bool = False
    large_image: str = ""
    small_image: str = ""


class DiscordRpcError(RuntimeError):
    pass


class DiscordRpcRejected(DiscordRpcError):
    pass


class WindowsDiscordIpcPipe:
    """Overlapped pipe IO permits concurrent reads/writes without CRT fd locks."""

    def __init__(self, path: str, write_timeout_seconds: float = 2.0):
        import _winapi
        self._api = _winapi
        self._handle = _winapi.CreateFile(path, 0xC0000000, 0, 0, 3, 0x40000000, 0)
        self._lock = threading.Lock()
        self._operations = []
        self._closed = False
        self._write_timeout_ms = max(1, int(write_timeout_seconds * 1000))

    def read(self, size: int) -> bytes:
        operation, count = self._perform(self._api.ReadFile, size, None)
        return operation.getbuffer()[:count]

    def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            _, count = self._perform(self._api.WriteFile, data[offset:], self._write_timeout_ms)
            if not count:
                raise OSError("Discord IPC write returned zero bytes.")
            offset += count

    def _perform(self, function, value, timeout_ms):
        with self._lock:
            if self._closed:
                raise OSError("Discord IPC pipe is closed.")
            operation, error = function(self._handle, value, overlapped=True)
            self._operations.append(operation)
        try:
            if error == self._api.ERROR_IO_PENDING:
                result = self._api.WaitForMultipleObjects(
                    [operation.event], False, self._api.INFINITE if timeout_ms is None else timeout_ms)
                if result == self._api.WAIT_TIMEOUT:
                    operation.cancel()
                    operation.GetOverlappedResult(True)
                    raise TimeoutError("Discord IPC write timed out.")
            count, error = operation.GetOverlappedResult(True)
            if error:
                raise OSError(error, "Discord IPC operation failed.")
            return operation, count
        finally:
            operation.cancel()
            with self._lock:
                self._operations.remove(operation)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for operation in self._operations:
                operation.cancel()
            self._api.CloseHandle(self._handle)


@dataclass
class _PendingResponse:
    event: threading.Event
    response: dict | None = None
    error: str = ""


class DiscordIpcPipe:
    def __init__(self, fd: int):
        self._fd = fd
        self._closed = False
        self._close_lock = threading.Lock()

    def read(self, size: int) -> bytes:
        return os.read(self._fd, size)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(self._fd, view[written:])
            if count == 0:
                raise OSError("Discord IPC pipe write returned 0 bytes.")
            written += count

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._fd)


@dataclass(frozen=True)
class DiscordRpcWorkerEvent:
    kind: str
    message: str


def encode_frame(opcode: int, payload: dict) -> bytes:
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", opcode, len(payload_bytes)) + payload_bytes


def decode_frame(header: bytes, payload: bytes) -> tuple[int, dict]:
    if len(header) != 8:
        raise ValueError("Discord IPC frame header must be 8 bytes.")
    opcode, length = struct.unpack("<II", header)
    if len(payload) != length:
        raise ValueError("Discord IPC frame payload length mismatch.")
    return opcode, json.loads(payload.decode("utf-8"))


def build_activity_from_snapshot(snapshot: GsiSnapshot) -> dict | None:
    if snapshot.score_ct is None or snapshot.score_t is None:
        state = "Score unavailable"
    else:
        state = f"Score:{format_score(snapshot.score_ct, snapshot.score_t)}"

    return _build_map_activity(snapshot.map_name, players="", state=state)


def build_activity_from_server_info(info: ServerInfo) -> dict | None:
    players = format_players(info)
    return _build_map_activity(info.map_name, players=players, state="Score unavailable", server_info=info)


def build_activity_from_presence_state(
    snapshot: GsiSnapshot | None = None,
    server_info: ServerInfo | None = None,
    rpc_config: DiscordRpcConfig | None = None,
    join_url: str = "",
) -> dict | None:
    if snapshot is not None and not snapshot.map_name:
        snapshot = None
    if snapshot is not None and server_info is not None and snapshot.map_name != server_info.map_name:
        server_info = None
    if server_info is None:
        join_url = ""
    map_name = ""
    if snapshot is not None and snapshot.map_name:
        map_name = snapshot.map_name
    elif server_info is not None and server_info.map_name:
        map_name = server_info.map_name

    if not map_name:
        return None

    players = ""
    if server_info is not None:
        players = format_players(server_info)
    if snapshot is not None and snapshot.score_ct is not None and snapshot.score_t is not None:
        score = f"Score:{format_score(snapshot.score_ct, snapshot.score_t)}"
    else:
        score = "Score unavailable"

    return _build_map_activity(
        map_name,
        players=players,
        state=score,
        server_info=server_info,
        rpc_config=rpc_config,
        join_url=join_url,
    )


def format_map_line(map_name: str, players: str) -> str:
    return f"{map_name} ({players})" if players else map_name


def format_players(info: ServerInfo) -> str:
    return f"{info.players}/{info.max_players}"


def format_score(score_ct: int, score_t: int) -> str:
    return f"Human:{score_ct} - Zombie:{score_t}"


def format_details(info: ServerInfo | None, players: str) -> str:
    if info is None:
        return ""
    return f"player:{players}" if players else ""


def _build_map_activity(
    map_name: str,
    players: str,
    state: str,
    server_info: ServerInfo | None = None,
    rpc_config: DiscordRpcConfig | None = None,
    join_url: str = "",
) -> dict | None:
    if not map_name:
        return None

    activity = {
        "type": 0,
        "name": _fit_activity_text(format_map_line(map_name, players)),
        "state": _fit_activity_text(state),
    }
    details = format_details(server_info, players)
    if details:
        activity["details"] = _fit_activity_text(details)
    _apply_activity_options(activity, rpc_config=rpc_config, join_url=join_url)
    return activity


def _fit_activity_text(value: str) -> str:
    text = value.strip()
    if len(text) <= DISCORD_ACTIVITY_TEXT_LIMIT:
        return text
    return text[: DISCORD_ACTIVITY_TEXT_LIMIT - 3].rstrip() + "..."


def load_discord_config(path: Path) -> DiscordRpcConfig:
    parser = read_config(path)
    try:
        enabled = parser.getboolean(DISCORD_CONFIG_SECTION, "Enabled", fallback=False)
    except ValueError:
        enabled = False

    return DiscordRpcConfig(
        client_id=parser.get(DISCORD_CONFIG_SECTION, "ClientId", fallback="").strip(),
        enabled=enabled,
        large_image=parser.get(DISCORD_CONFIG_SECTION, "LargeImage", fallback="").strip(),
        small_image=parser.get(DISCORD_CONFIG_SECTION, "SmallImage", fallback="").strip(),
    )


def save_discord_config(path: Path, config: DiscordRpcConfig) -> None:
    parser = read_config(path)
    if not parser.has_section(DISCORD_CONFIG_SECTION):
        parser.add_section(DISCORD_CONFIG_SECTION)

    parser.set(DISCORD_CONFIG_SECTION, "ClientId", config.client_id.strip())
    parser.set(DISCORD_CONFIG_SECTION, "Enabled", "1" if config.enabled else "0")
    parser.set(DISCORD_CONFIG_SECTION, "LargeImage", config.large_image.strip())
    parser.set(DISCORD_CONFIG_SECTION, "SmallImage", config.small_image.strip())

    write_config(path, parser)


class DiscordRpcClient:
    def __init__(
        self,
        client_id: str,
        pid: int | None = None,
        pipe_paths: Iterable[str] | None = None,
        debug_logger: DebugLogger | None = None,
        write_timeout_seconds: float = 2.0,
        response_timeout_seconds: float = 3.0,
    ):
        self.client_id = client_id.strip()
        self.pid = pid if pid is not None else os.getpid()
        self.pipe_paths = list(pipe_paths) if pipe_paths is not None else _default_pipe_paths()
        self._debug_logger = debug_logger
        self._write_timeout_seconds = write_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._pending: dict[str, _PendingResponse] = {}
        self._state_lock = threading.RLock()
        self._file: object | None = None
        self._write_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._ready_error = ""

    @property
    def is_connected(self) -> bool:
        return self._file is not None

    def connect(self) -> None:
        if self.is_connected:
            self._debug("connect skipped; already connected")
            return
        if not self.client_id:
            self._debug("connect failed; empty client id")
            raise DiscordRpcError("Discord Client ID is empty.")

        self._debug(
            "connect start",
            client_id=mask_client_id(self.client_id),
            pid=self.pid,
            pipe_count=len(self.pipe_paths),
        )
        self._ready_event.clear()
        self._ready_error = ""
        last_error: OSError | None = None
        for index, pipe_path in enumerate(self.pipe_paths):
            self._debug("opening pipe", index=index, pipe_path=pipe_path)
            try:
                pipe = _open_ipc_pipe(pipe_path, self._write_timeout_seconds)
            except OSError as exc:
                last_error = exc
                self._debug(
                    "pipe open failed",
                    index=index,
                    pipe_path=pipe_path,
                    error=repr(exc),
                    winerror=getattr(exc, "winerror", None),
                )
                continue

            try:
                frame = encode_frame(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                self._debug("pipe opened", index=index, pipe_path=pipe_path)
                self._debug("handshake write start", bytes=len(frame))
                self._write_bytes_with_timeout(pipe, frame)
                self._debug("handshake write ok")
                self._file = pipe
                self._start_reader_thread()
                return
            except OSError as exc:
                last_error = exc
                self._debug(
                    "handshake failed",
                    index=index,
                    pipe_path=pipe_path,
                    error=repr(exc),
                    winerror=getattr(exc, "winerror", None),
                )
                pipe.close()

        detail = f": {last_error}" if last_error is not None else ""
        self._debug("connect failed; no pipe available", last_error=repr(last_error))
        raise DiscordRpcError(f"Discord IPC pipe was not found{detail}")

    def update_activity(self, activity: dict) -> None:
        if not activity:
            return
        self._send_activity(activity, auto_connect=True)

    def wait_until_ready(self, timeout: float) -> bool:
        self._debug("ready wait start", timeout=timeout, connected=self.is_connected)
        if not self.is_connected:
            self._debug("ready wait skipped; not connected")
            return False

        is_ready = self._ready_event.wait(timeout=timeout)
        self._debug("ready wait done", ready=is_ready, error=self._ready_error)
        if is_ready and self._ready_error:
            raise DiscordRpcError(self._ready_error)
        return is_ready

    def clear_activity(self) -> None:
        if not self.is_connected:
            return
        self._send_activity(None, auto_connect=False)

    def close(self) -> None:
        self._close_file()

    def _send_activity(self, activity: dict | None, auto_connect: bool) -> None:
        self._debug(
            "send activity requested",
            auto_connect=auto_connect,
            connected=self.is_connected,
            activity=_summarize_activity(activity),
        )
        if auto_connect and not self.is_connected:
            self.connect()
        if auto_connect:
            if not self.wait_until_ready(self._response_timeout_seconds):
                self.close()
                raise DiscordRpcError("Discord READY response timed out.")
        if not self.is_connected:
            self._debug("send activity skipped; not connected")
            if auto_connect:
                raise DiscordRpcError("Discord IPC connection closed before sending activity.")
            return

        command = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": self.pid,
                "activity": activity,
            },
            "nonce": str(uuid4()),
        }
        pending = _PendingResponse(threading.Event())
        nonce = command["nonce"]
        with self._state_lock:
            self._pending[nonce] = pending
        try:
            self._write_frame(OP_FRAME, command)
            if not pending.event.wait(self._response_timeout_seconds):
                self._close_file()
                raise DiscordRpcError("Discord SET_ACTIVITY response timed out.")
            if pending.error:
                raise DiscordRpcError(pending.error)
            if pending.response is not None and pending.response.get("evt") == "ERROR":
                raise DiscordRpcRejected(f"Discord rejected activity: {pending.response.get('data')}")
        finally:
            with self._state_lock:
                self._pending.pop(nonce, None)

    def _write_frame(self, opcode: int, payload: dict) -> None:
        frame = encode_frame(opcode, payload)
        command = payload.get("cmd") if isinstance(payload, dict) else None
        with self._write_lock:
            if self._file is None:
                self._debug("write frame failed; not connected", opcode=opcode, command=command)
                raise DiscordRpcError("Discord IPC is not connected.")
            try:
                self._debug("write frame start", opcode=opcode, command=command, bytes=len(frame))
                self._write_bytes_with_timeout(self._file, frame)
                self._debug("write frame ok", opcode=opcode, command=command, bytes=len(frame))
            except TimeoutError as exc:
                self._close_file()
                raise DiscordRpcError(f"Discord IPC write timed out after {self._write_timeout_seconds:.1f}s") from exc
            except OSError as exc:
                self._close_file()
                self._debug(
                    "write frame failed",
                    opcode=opcode,
                    command=command,
                    error=repr(exc),
                    winerror=getattr(exc, "winerror", None),
                )
                raise DiscordRpcError(f"Discord IPC write failed: {exc}") from exc

    def _write_bytes_with_timeout(self, pipe: object, data: bytes) -> None:
        if isinstance(pipe, WindowsDiscordIpcPipe):
            pipe.write(data)
            return
        result: list[Exception | None] = [None]

        def write_pipe() -> None:
            try:
                pipe.write(data)  # type: ignore[attr-defined]
            except Exception as exc:
                result[0] = exc

        writer = threading.Thread(target=write_pipe, name="discord-rpc-writer", daemon=True)
        writer.start()
        writer.join(timeout=self._write_timeout_seconds)
        if writer.is_alive():
            raise TimeoutError("Discord IPC write timed out.")
        if result[0] is not None:
            raise result[0]

    def _start_reader_thread(self) -> None:
        self._reader_thread = threading.Thread(target=self._reader_loop, args=(self._file,), name="discord-rpc-reader", daemon=True)
        self._reader_thread.start()
        self._debug("reader thread started")

    def _reader_loop(self, pipe=None) -> None:
        self._debug("reader loop started")
        pipe = self._file if pipe is None else pipe
        while True:
            if pipe is None or pipe is not self._file:
                self._debug("reader loop stopped; no pipe")
                return
            try:
                self._debug("reader waiting for header")
                header = self._read_exact(pipe, 8)
                if not header:
                    self._debug("reader got empty header; closing")
                    self._close_file(pipe)
                    return
                if len(header) != 8:
                    self._debug("reader got partial header; closing", bytes=len(header))
                    self._close_file(pipe)
                    return
                _, length = struct.unpack("<II", header)
                if length > 1024 * 1024:
                    raise ValueError("Discord IPC frame exceeds the size limit.")
                payload = b""
                if length:
                    payload = self._read_exact(pipe, length)
                    if len(payload) != length:
                        self._debug("reader got partial payload; closing", expected=length, bytes=len(payload))
                        self._close_file(pipe)
                        return
                try:
                    opcode, decoded = decode_frame(header, payload)
                except Exception as exc:
                    self._debug("reader frame decode failed", length=length, error=repr(exc), payload=payload.decode("utf-8", errors="replace"))
                else:
                    if pipe is not self._file:
                        return
                    self._debug("reader frame received", opcode=opcode, length=length, payload=decoded)
                    if opcode == OP_CLOSE:
                        self._close_file(pipe)
                        return
                    if opcode == OP_PING:
                        self._write_frame(OP_PONG, decoded)
                    elif opcode == OP_FRAME:
                        self._handle_reader_frame(decoded)
            except (OSError, ValueError, struct.error, DiscordRpcError, AttributeError):
                self._debug("reader loop failed", error="read/decode exception")
                self._close_file(pipe)
                return

    def _close_file(self, expected_pipe=None) -> None:
        with self._state_lock:
            if expected_pipe is not None and self._file is not expected_pipe:
                return
            pipe = self._file
            self._file = None
            if not self._ready_event.is_set():
                self._ready_error = "Discord IPC closed before READY."
                self._ready_event.set()
            for pending in self._pending.values():
                if not pending.event.is_set():
                    pending.error = "Discord IPC connection closed."
                    pending.event.set()
        if pipe is not None:
            try:
                pipe.close()  # type: ignore[attr-defined]
                self._debug("pipe closed")
            except OSError:
                self._debug("pipe close failed")
                pass

    def _read_exact(self, pipe: object, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = pipe.read(size - len(chunks))  # type: ignore[attr-defined]
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def _debug(self, message: str, **fields) -> None:
        if self._debug_logger is not None:
            self._debug_logger.log("discord.client", message, **fields)

    def _handle_reader_frame(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            raise ValueError("Discord IPC payload must be an object.")
        nonce = payload.get("nonce")
        with self._state_lock:
            pending = self._pending.get(nonce) if isinstance(nonce, str) else None
            if pending is not None and payload.get("cmd") == "SET_ACTIVITY":
                pending.response = payload
                pending.event.set()
                return False
        event_name = payload.get("evt")
        if event_name == "READY":
            self._debug("ready received")
            self._ready_event.set()
            return False
        elif event_name == "ERROR":
            if not self._ready_event.is_set():
                self._ready_error = str(payload.get("data") or payload)
                self._ready_event.set()
            self._debug("error frame received", error=str(payload.get("data")))
        return False


class DiscordRpcWorker:
    def __init__(
        self,
        client_factory: Callable[[str], object] | None = None,
        on_event: Callable[[DiscordRpcWorkerEvent], None] | None = None,
        debug_logger: DebugLogger | None = None,
        ready_timeout_seconds: float = 8.0,
        reconnect_delay_seconds: float = 1.0,
    ):
        self._client_factory = client_factory
        self._on_event = on_event or (lambda event: None)
        self._debug_logger = debug_logger
        self._ready_timeout_seconds = ready_timeout_seconds
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._commands: queue.Queue[tuple[str, dict | None]] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._client = None
        self._client_id = ""
        self._stop_requested = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, client_id: str) -> None:
        if self.is_running:
            self._debug("start skipped; already running")
            return
        self._stop_requested.clear()
        self._debug("start requested", client_id=mask_client_id(client_id))
        self._thread = threading.Thread(target=self._run, args=(client_id,), name="discord-rpc-worker", daemon=True)
        self._thread.start()

    def update_activity(self, activity: dict) -> None:
        self._debug("update requested", activity=_summarize_activity(activity))
        self._put_command("update", activity)

    def clear_and_stop(self) -> None:
        self._debug("clear and stop requested")
        self._stop_requested.set()
        self._put_command("stop", None)

    def clear_activity(self) -> None:
        self._put_command("clear", None)

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _run(self, client_id: str) -> None:
        self._client_id = client_id
        self._debug("worker thread start", client_id=mask_client_id(client_id))
        self._connect_client()

        try:
            while not self._stop_requested.is_set():
                try:
                    command, payload = self._commands.get(timeout=0.2)
                except queue.Empty:
                    continue

                self._debug("command received", command=command, activity=_summarize_activity(payload))
                if command == "stop":
                    return
                if command == "update" and payload is not None:
                    self._update_client(payload)
                elif command == "clear":
                    self._clear_client()
        finally:
            self._clear_client()
            self._close_client()
            self._emit("closed", "Discord RPC stopped.")
            self._debug("worker thread stopped")

    def _update_client(self, activity: dict) -> None:
        if self._client is None:
            self._debug("update needs reconnect; no client")
            if not self._connect_client():
                self._wait_before_reconnect()
                self._requeue_activity(activity)
                return
        try:
            self._debug("client update begin", activity=_summarize_activity(activity))
            self._client.update_activity(activity)
        except DiscordRpcRejected as exc:
            self._emit("rejected", str(exc))
        except Exception as exc:
            self._debug("client update failed", error=repr(exc), activity=_summarize_activity(activity))
            self._emit("warning", f"Discord RPC update failed; reconnecting: {exc}")
            self._close_client()
            if self._wait_before_reconnect():
                return
            self._requeue_activity(activity)
        else:
            self._debug("client update ok", activity=_summarize_activity(activity))
            self._emit("updated", "Discord RPC updated.")

    def _connect_client(self) -> bool:
        try:
            client = self._create_client(self._client_id)
            self._client = client
            self._debug("client connect begin")
            client.connect()
            if hasattr(client, "wait_until_ready"):
                self._debug("client ready wait begin", timeout=self._ready_timeout_seconds)
                if not client.wait_until_ready(self._ready_timeout_seconds):
                    raise DiscordRpcError("Discord RPC READY timeout.")
        except Exception as exc:
            self._debug("client connect failed", error=repr(exc))
            self._close_client()
            self._emit("warning", f"Discord RPC reconnect failed: {exc}")
            return False

        self._debug("client ready")
        self._emit("connected", "Discord RPC connected.")
        return True

    def _wait_before_reconnect(self) -> bool:
        if self._reconnect_delay_seconds <= 0:
            return self._stop_requested.is_set()
        self._debug("reconnect delay", seconds=self._reconnect_delay_seconds)
        return self._stop_requested.wait(self._reconnect_delay_seconds)

    def _requeue_activity(self, activity: dict) -> None:
        if self._stop_requested.is_set():
            return
        self._debug("activity requeued after reconnect failure", activity=_summarize_activity(activity))
        try:
            self._commands.put_nowait(("update", activity))
        except queue.Full:
            pass  # A newer activity (or STOP) always takes precedence.

    def _clear_client(self) -> None:
        if self._client is None:
            self._debug("clear skipped; no client")
            return
        try:
            self._debug("client clear begin")
            self._client.clear_activity()
        except Exception as exc:
            self._debug("client clear failed", error=repr(exc))
            self._close_client()
            self._emit("warning", f"Discord RPC clear failed: {exc}")
        else:
            self._debug("client clear ok")

    def _close_client(self) -> None:
        if self._client is None:
            self._debug("close skipped; no client")
            return
        try:
            self._debug("client close begin")
            self._client.close()
        finally:
            self._client = None
            self._debug("client close done")

    def _put_command(self, command: str, payload: dict | None) -> None:
        if not self.is_running:
            self._debug("command ignored; worker not running", command=command)
            return
        while True:
            try:
                self._commands.put_nowait((command, payload))
                self._debug("command queued", command=command, activity=_summarize_activity(payload))
                return
            except queue.Full:
                try:
                    old_command, _ = self._commands.get_nowait()
                except queue.Empty:
                    continue
                self._debug("dropped queued command", old_command=old_command, new_command=command)
                if old_command == "stop":
                    self._commands.put_nowait((old_command, None))
                    return

    def _emit(self, kind: str, message: str) -> None:
        self._debug("event emitted", kind=kind, event_message=message)
        self._on_event(DiscordRpcWorkerEvent(kind=kind, message=message))

    def _create_client(self, client_id: str):
        if self._client_factory is not None:
            return self._client_factory(client_id)
        return DiscordRpcClient(client_id, debug_logger=self._debug_logger)

    def _debug(self, message: str, **fields) -> None:
        if self._debug_logger is not None:
            self._debug_logger.log("discord.worker", message, **fields)


def _default_pipe_paths() -> list[str]:
    standard_paths = [rf"\\.\pipe\discord-ipc-{index}" for index in range(10)]
    extended_paths = [rf"\\?\pipe\discord-ipc-{index}" for index in range(10)]
    return standard_paths + extended_paths


def _open_ipc_pipe(pipe_path: str, write_timeout_seconds: float = 2.0):
    if os.name == "nt":
        return WindowsDiscordIpcPipe(pipe_path, write_timeout_seconds)
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    return DiscordIpcPipe(os.open(pipe_path, flags))


def _apply_activity_options(
    activity: dict,
    rpc_config: DiscordRpcConfig | None = None,
    join_url: str = "",
) -> None:
    assets = {}
    if rpc_config is not None:
        if rpc_config.large_image.strip():
            assets["large_image"] = rpc_config.large_image.strip()
        if rpc_config.small_image.strip():
            assets["small_image"] = rpc_config.small_image.strip()
    if assets:
        activity["assets"] = assets

    if join_url.strip():
        activity["buttons"] = [{"label": "join server", "url": join_url.strip()}]


def _summarize_activity(activity: dict | None) -> dict | None:
    if activity is None:
        return None
    summary = {
        "name": activity.get("name"),
        "details": activity.get("details"),
        "state": activity.get("state"),
        "type": activity.get("type"),
    }
    if "assets" in activity:
        summary["assets"] = activity.get("assets")
    if "buttons" in activity:
        summary["buttons"] = activity.get("buttons")
    return summary

