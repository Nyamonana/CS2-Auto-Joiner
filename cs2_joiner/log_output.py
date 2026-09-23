"""Game-native log profiles and read-only confirmation of their state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Callable
import uuid

from .config_io import read_config, write_config


SECTION = "ManualLogOutput"
CFG_NAMES = {"paused": "cs2_joiner_log_pause.cfg", "recording": "cs2_joiner_log_resume.cfg"}
KEY_CFG_NAME = "cs2_joiner_log_keys.cfg"
LOG_KEYS = ("F6", "F7", "F8", "F9", "F10", "F11")
OWNER = "// CS2 Joiner manual log output v1\n"
CONSOLE_LINE = re.compile(r"^(?:\d{2}/\d{2} \d{2}:\d{2}:\d{2} )?\[Console\]\s+(.*)$")
CHANNEL_ROW = re.compile(
    r"^\d+\s+(.+?)\s{2,}(off|essential|default|detailed|max)\s+0x[0-9A-Fa-f]{8}\s*(.*)$"
)


@dataclass(frozen=True)
class LogChannel:
    name: str
    severity: str
    console_only: bool


def parse_channel(line: str) -> LogChannel | None:
    console = CONSOLE_LINE.fullmatch(line.strip())
    if console is None:
        return None
    row = CHANNEL_ROW.fullmatch(console[1])
    if row is None:
        return None
    name, severity, remainder = row.groups()
    # Channel names become quoted CFG arguments, not arbitrary console commands.
    if not re.fullmatch(r"[A-Za-z0-9_ .:/()\-]+", name):
        raise ValueError(f"Unsupported logging channel name: {name!r}")
    flags = re.split(r"\s{2,}", remainder, maxsplit=1)[0]
    return LogChannel(name, severity, "[ConsoleOnly]" in flags)


def parse_channel_dump(text: str) -> tuple[LogChannel, ...]:
    channels = {}
    for line in text.splitlines():
        channel = parse_channel(line)
        if channel is not None:
            key = channel.name.casefold()
            if key in channels:
                raise ValueError("Use one complete log_dumpchannels output, not multiple snapshots.")
            channels[key] = channel
    for name in ("console", "client", "hoststatemanager"):
        channel = channels.get(name)
        if channel is None:
            raise ValueError(f"The channel dump is missing {name}.")
        if channel.severity not in ("default", "detailed", "max"):
            raise ValueError(f"{channel.name} must allow default messages for confirmation.")
        if name != "console" and channel.console_only:
            raise ValueError(f"{channel.name} is already ConsoleOnly. Restore logging before taking the snapshot.")
    return tuple(channels.values())


@dataclass(frozen=True)
class LogOutputSettings:
    cfg_dir: Path
    snapshot: str
    token: str
    pause_key: str = ""
    resume_key: str = ""

    @property
    def channels(self) -> tuple[LogChannel, ...]:
        return parse_channel_dump(self.snapshot)

    def validate(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", self.token):
            raise ValueError("Invalid manual log output token.")
        if any(key and key not in LOG_KEYS for key in (self.pause_key, self.resume_key)):
            raise ValueError("Choose a function key from F6 to F11.")
        if self.pause_key and self.pause_key == self.resume_key:
            raise ValueError("Pause and resume need different keys.")
        self.channels


def load_log_output_settings(path: Path) -> LogOutputSettings | None:
    parser = read_config(path)
    if not parser.has_section(SECTION):
        return None
    try:
        settings = LogOutputSettings(
            Path(parser.get(SECTION, "CfgDirectory")),
            json.loads(parser.get(SECTION, "ChannelSnapshot")),
            parser.get(SECTION, "Token"),
            parser.get(SECTION, "PauseKey", fallback=""),
            parser.get(SECTION, "ResumeKey", fallback=""),
        )
        if not isinstance(settings.snapshot, str):
            raise ValueError("Invalid channel snapshot.")
        settings.validate()
    except Exception as exc:
        raise ValueError(f"Manual log output settings are invalid: {exc}") from exc
    return settings


def save_log_output_settings(path: Path, settings: LogOutputSettings) -> None:
    settings.validate()
    parser = read_config(path)
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    parser.set(SECTION, "CfgDirectory", str(settings.cfg_dir))
    parser.set(SECTION, "ChannelSnapshot", json.dumps(settings.snapshot, ensure_ascii=True))
    parser.set(SECTION, "Token", settings.token)
    parser.set(SECTION, "PauseKey", settings.pause_key)
    parser.set(SECTION, "ResumeKey", settings.resume_key)
    write_config(path, parser)


def install_key_bindings(settings: LogOutputSettings) -> None:
    settings.validate()
    path = settings.cfg_dir / KEY_CFG_NAME
    if path.is_symlink() or (path.exists() and not path.read_text(encoding="utf-8").startswith(OWNER)):
        raise ValueError(f"Refusing to overwrite a file not created by this feature: {path}")
    lines = [OWNER.rstrip()]
    for key, state in ((settings.pause_key, "paused"), (settings.resume_key, "recording")):
        if key:
            lines.append(f'bind "{key}" "exec {CFG_NAMES[state][:-4]}"')
    if settings.resume_key:
        lines.extend(('log_flags "Console" -ConsoleOnly',
                      f"echo CS2J_KEYS_{settings.token}_{settings.resume_key}"))
    lines.append("exec cs2_joiner_log_resume")
    _write_cfg(path, "\n".join(lines) + "\n")


def _write_cfg(path: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_log_profile(settings: LogOutputSettings, state: str) -> str:
    if state not in CFG_NAMES:
        raise ValueError("Unknown log output state.")
    settings.validate()
    channels = settings.channels
    marker = f"CS2J_LOG_{settings.token}_{state}"
    lines = [OWNER.rstrip(), 'log_flags "Console" -ConsoleOnly', f"echo {marker}_BEGIN"]
    for channel in channels:
        if channel.name.casefold() != "console":
            flag = "+" if state == "paused" or channel.console_only else "-"
            lines.append(f'log_flags "{channel.name}" {flag}ConsoleOnly')
    lines.extend(("log_dumpchannels", f"echo {marker}_END"))
    console = next(channel for channel in channels if channel.name.casefold() == "console")
    flag = "+" if state == "paused" or console.console_only else "-"
    lines.append(f'log_flags "Console" {flag}ConsoleOnly')
    return "\n".join(lines) + "\n"


def install_log_profiles(cfg_dir: Path, snapshot: str) -> LogOutputSettings:
    channels = parse_channel_dump(snapshot)
    # Persist channel metadata only, never incidental chat copied with the dump.
    snapshot = "\n".join(
        f"[Console] {index}  {channel.name}  {channel.severity}  0x00000000    "
        + ("[ConsoleOnly]" if channel.console_only else "")
        for index, channel in enumerate(channels)
    )
    settings = LogOutputSettings(cfg_dir.resolve(), snapshot, uuid.uuid4().hex)
    settings.validate()
    if not cfg_dir.is_dir():
        raise ValueError("Select the existing CS2 game/csgo/cfg directory.")
    paths = {state: cfg_dir / filename for state, filename in CFG_NAMES.items()}
    for path in paths.values():
        if path.is_symlink() or (path.exists() and not path.read_text(encoding="utf-8").startswith(OWNER)):
            raise ValueError(f"Refusing to overwrite a file not created by this feature: {path}")
    # Install the recovery command first. No autoexec, bind or existing CFG is edited.
    for state in ("recording", "paused"):
        _write_cfg(paths[state], build_log_profile(settings, state))
    return settings


class LogOutputMonitor:
    """Observe fresh acknowledgements, retaining no chat or other game messages."""

    def __init__(self, settings: LogOutputSettings, log_path: Path,
                 on_status: Callable[[str, str], None]):
        self.settings = settings
        self.log_path = log_path
        self._on_status = on_status
        self._channels = {channel.name.casefold(): channel for channel in settings.channels}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._identity = None
        self._offset = 0
        self._buffer = b""
        self._skip_partial = False
        self._capture = ""
        self._seen: dict[str, LogChannel] = {}
        self._state = "unknown"
        self._last_message = ""
        self._verified_resume_key = ""

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def start(self) -> None:
        # Bookmarks are established synchronously, before the command can be copied.
        self.poll()
        self._thread = threading.Thread(target=self._run, name="cs2-log-output", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def recording_ready(self) -> bool:
        with self._lock:
            caught_up = self._poll_locked()
            return caught_up and self._state == "recording"

    def resume_key_verified(self, key: str) -> bool:
        with self._lock:
            return bool(key) and self._verified_resume_key == key

    def expect_command(self, message: str = "Waiting for CS2 to confirm the log output command.",
                       *, reset_keys: bool = False) -> None:
        with self._lock:
            previous_identity, verified_key = self._identity, self._verified_resume_key
            previous_offset = self._offset
            self._identity = None
            self._poll_locked()
            if (not reset_keys and self._identity is not None and self._identity == previous_identity
                    and self._offset >= previous_offset):
                self._verified_resume_key = verified_key
            self._capture = ""
            self._seen.clear()
            self._set_state("unknown", message)

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            self.poll()

    def poll(self) -> None:
        with self._lock:
            self._poll_locked()

    def _poll_locked(self) -> bool:
        try:
            with self.log_path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self._identity or stat.st_size < self._offset:
                    self._verified_resume_key = ""
                    self._identity = identity
                    self._offset = stat.st_size
                    self._buffer = b""
                    self._capture = ""
                    self._seen.clear()
                    self._skip_partial = False
                    if self._offset:
                        handle.seek(-1, 2)
                        self._skip_partial = handle.read(1) != b"\n"
                    self._set_state("unknown", "Run the recording command in CS2 before JOIN (-condebug required).")
                    return False
                handle.seek(self._offset)
                # Bounded batches keep both memory and the connection guard responsive.
                data = handle.read(512 * 1024)
                self._offset = handle.tell()
                caught_up = self._offset >= os.fstat(handle.fileno()).st_size
        except OSError as exc:
            self._identity = None
            self._verified_resume_key = ""
            self._capture = ""
            self._seen.clear()
            self._set_state("unknown", f"Manual log output cannot be confirmed: {exc}")
            return False
        lines = (self._buffer + data).split(b"\n")
        self._buffer = lines.pop()
        for line in lines:
            if self._skip_partial:
                self._skip_partial = False
                continue
            self._feed(line.decode("utf-8", errors="replace").strip())
        if len(self._buffer) > 65536:
            self._buffer = b""
            self._skip_partial = True
            self._capture = ""
            self._set_state("unknown", "Oversized log line; run the recording command again.")
        return caught_up

    def _feed(self, line: str) -> None:
        console = CONSOLE_LINE.fullmatch(line)
        if console is None:
            return
        message = console[1]
        prefix = f"CS2J_KEYS_{self.settings.token}_"
        if message.startswith(prefix) and message[len(prefix):] in LOG_KEYS:
            self._verified_resume_key = message[len(prefix):]
            self._on_status(self._state, "CS2 applied the log key bindings.")
            return
        for state in CFG_NAMES:
            marker = f"CS2J_LOG_{self.settings.token}_{state}"
            if message == marker + "_BEGIN":
                self._capture = state
                self._seen.clear()
                self._set_state("unknown", "CS2 is applying log output flags.")
                return
            if self._capture == state and message == marker + "_END":
                valid = all(
                    name in self._seen
                    and self._seen[name].console_only == (state == "paused" or channel.console_only)
                    and self._seen[name].severity == channel.severity
                    for name, channel in self._channels.items() if name != "console"
                )
                extra = len(self._seen.keys() - self._channels.keys())
                self._capture = ""
                if valid:
                    detail = "CS2 confirmed ConsoleOnly flags for the saved channels." if state == "paused" else "CS2 confirmed the saved logging flags; JOIN is available."
                    if extra:
                        detail += f" {extra} new channel(s) are not managed; refresh the snapshot after restoring."
                    self._set_state(state, detail)
                else:
                    self._set_state("unknown", "CS2 log flags did not match the profile. JOIN remains blocked.")
                return
        if self._capture:
            try:
                channel = parse_channel(line)
            except ValueError:
                self._capture = ""
                self._set_state("unknown", "Unsupported channel in CS2 confirmation; refresh the snapshot.")
                return
            if channel is not None:
                self._seen[channel.name.casefold()] = channel

    def _set_state(self, state: str, message: str) -> None:
        self._state = state
        if message != self._last_message:
            self._last_message = message
            self._on_status(state, message)
