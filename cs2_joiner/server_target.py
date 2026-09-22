from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

from .a2s import ServerAddress
from .config_io import read_config, write_config


TARGET_CONFIG_SECTION = "Target"
TARGET_ADDRESS_KEY = "ServerAddress"
JOIN_CONFIG_SECTION = "Join"
REQUEST_RATE_KEY = "RequestRate"
VALID_REQUEST_RATES = (1, 2, 3)
SOUND_CONFIG_SECTION = "Sound"
SOUND_ENABLED_KEY = "Enabled"
SOUND_FILE_KEY = "File"


@dataclass(frozen=True)
class SoundSettings:
    enabled: bool
    selected_file: str = ""


def load_saved_server_address(path: Path) -> str:
    parser = read_config(path)
    return parser.get(TARGET_CONFIG_SECTION, TARGET_ADDRESS_KEY, fallback="").strip()


def save_server_address(path: Path, address: ServerAddress) -> None:
    parser = read_config(path)
    _ensure_section(parser, TARGET_CONFIG_SECTION)
    parser.set(TARGET_CONFIG_SECTION, TARGET_ADDRESS_KEY, str(address))
    write_config(path, parser)


def load_request_rate(path: Path, default: int = 1) -> int:
    parser = read_config(path)
    try:
        value = parser.getint(JOIN_CONFIG_SECTION, REQUEST_RATE_KEY, fallback=default)
    except ValueError:
        return default
    return value if value in VALID_REQUEST_RATES else default


def save_request_rate(path: Path, request_rate: int) -> None:
    if request_rate not in VALID_REQUEST_RATES:
        raise ValueError("Request rate must be 1, 2, or 3.")

    parser = read_config(path)
    _ensure_section(parser, JOIN_CONFIG_SECTION)
    parser.set(JOIN_CONFIG_SECTION, REQUEST_RATE_KEY, str(request_rate))
    write_config(path, parser)


def load_console_log_path(path: Path) -> str:
    return read_config(path).get(JOIN_CONFIG_SECTION, "ConsoleLogPath", fallback="").strip()


def load_auto_retry_enabled(path: Path) -> bool:
    try:
        return read_config(path).getboolean(JOIN_CONFIG_SECTION, "AutoRetry", fallback=False)
    except ValueError:
        return False


def save_auto_retry_enabled(path: Path, enabled: bool) -> None:
    parser = read_config(path)
    _ensure_section(parser, JOIN_CONFIG_SECTION)
    parser.set(JOIN_CONFIG_SECTION, "AutoRetry", "1" if enabled else "0")
    write_config(path, parser)


def load_log_cleanup_enabled(path: Path) -> bool:
    try:
        return read_config(path).getboolean("LogCleanup", "Enabled", fallback=False)
    except ValueError:
        return False


def save_log_cleanup_enabled(path: Path, enabled: bool) -> None:
    parser = read_config(path)
    _ensure_section(parser, "LogCleanup")
    parser.set("LogCleanup", "Enabled", "1" if enabled else "0")
    write_config(path, parser)


def save_console_log_path(path: Path, log_path: Path) -> None:
    parser = read_config(path)
    _ensure_section(parser, JOIN_CONFIG_SECTION)
    parser.set(JOIN_CONFIG_SECTION, "ConsoleLogPath", str(log_path))
    write_config(path, parser)


def load_sound_settings(path: Path, default_enabled: bool = True) -> SoundSettings:
    parser = read_config(path)
    try:
        enabled = parser.getboolean(SOUND_CONFIG_SECTION, SOUND_ENABLED_KEY, fallback=default_enabled)
    except ValueError:
        enabled = default_enabled
    selected_file = parser.get(SOUND_CONFIG_SECTION, SOUND_FILE_KEY, fallback="").strip()
    return SoundSettings(enabled=enabled, selected_file=selected_file)


def save_sound_settings(path: Path, settings: SoundSettings) -> None:
    parser = read_config(path)
    _ensure_section(parser, SOUND_CONFIG_SECTION)
    parser.set(SOUND_CONFIG_SECTION, SOUND_ENABLED_KEY, "1" if settings.enabled else "0")
    parser.set(SOUND_CONFIG_SECTION, SOUND_FILE_KEY, settings.selected_file.strip())
    write_config(path, parser)


def _ensure_section(parser: ConfigParser, section: str) -> None:
    if not parser.has_section(section):
        parser.add_section(section)
