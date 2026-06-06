from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

from .a2s import ServerAddress


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
    parser = _load_config(path)
    return parser.get(TARGET_CONFIG_SECTION, TARGET_ADDRESS_KEY, fallback="").strip()


def save_server_address(path: Path, address: ServerAddress) -> None:
    parser = _load_config(path)
    _ensure_section(parser, TARGET_CONFIG_SECTION)
    parser.set(TARGET_CONFIG_SECTION, TARGET_ADDRESS_KEY, str(address))
    _write_config(path, parser)


def load_request_rate(path: Path, default: int = 1) -> int:
    parser = _load_config(path)
    try:
        value = parser.getint(JOIN_CONFIG_SECTION, REQUEST_RATE_KEY, fallback=default)
    except ValueError:
        return default
    return value if value in VALID_REQUEST_RATES else default


def save_request_rate(path: Path, request_rate: int) -> None:
    if request_rate not in VALID_REQUEST_RATES:
        raise ValueError("Request rate must be 1, 2, or 3.")

    parser = _load_config(path)
    _ensure_section(parser, JOIN_CONFIG_SECTION)
    parser.set(JOIN_CONFIG_SECTION, REQUEST_RATE_KEY, str(request_rate))
    _write_config(path, parser)


def load_sound_settings(path: Path, default_enabled: bool = True) -> SoundSettings:
    parser = _load_config(path)
    try:
        enabled = parser.getboolean(SOUND_CONFIG_SECTION, SOUND_ENABLED_KEY, fallback=default_enabled)
    except ValueError:
        enabled = default_enabled
    selected_file = parser.get(SOUND_CONFIG_SECTION, SOUND_FILE_KEY, fallback="").strip()
    return SoundSettings(enabled=enabled, selected_file=selected_file)


def save_sound_settings(path: Path, settings: SoundSettings) -> None:
    parser = _load_config(path)
    _ensure_section(parser, SOUND_CONFIG_SECTION)
    parser.set(SOUND_CONFIG_SECTION, SOUND_ENABLED_KEY, "1" if settings.enabled else "0")
    parser.set(SOUND_CONFIG_SECTION, SOUND_FILE_KEY, settings.selected_file.strip())
    _write_config(path, parser)


def _load_config(path: Path) -> ConfigParser:
    parser = _new_config_parser()
    if path.exists():
        parser.read(path, encoding="utf-8")
    return parser


def _write_config(path: Path, parser: ConfigParser) -> None:
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _ensure_section(parser: ConfigParser, section: str) -> None:
    if not parser.has_section(section):
        parser.add_section(section)


def _new_config_parser() -> ConfigParser:
    parser = ConfigParser()
    parser.optionxform = str
    return parser
