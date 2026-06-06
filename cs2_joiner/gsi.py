from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
from typing import Callable

from .debug_log import DebugLogger


CS2_APP_ID = "730"
CS2_INSTALL_DIR_NAME = "Counter-Strike Global Offensive"
GSI_CONFIG_FILENAME = "gamestate_integration_ze_joiner.cfg"


@dataclass(frozen=True)
class GsiSnapshot:
    player_name: str = ""
    activity: str = ""
    map_name: str = ""
    mode: str = ""
    phase: str = ""
    score_ct: int | None = None
    score_t: int | None = None


@dataclass(frozen=True)
class GsiConfigInstallResult:
    installed_paths: tuple[Path, ...]
    errors: tuple[str, ...] = ()


def extract_gsi_snapshot(payload: dict) -> GsiSnapshot:
    player = _get_dict(payload, "player")
    game_map = _get_dict(payload, "map")
    team_ct = _get_dict(game_map, "team_ct")
    team_t = _get_dict(game_map, "team_t")

    return GsiSnapshot(
        player_name=str(player.get("name", "")),
        activity=str(player.get("activity", "")),
        map_name=str(game_map.get("name", "")),
        mode=str(game_map.get("mode", "")),
        phase=str(game_map.get("phase", "")),
        score_ct=_optional_int(team_ct.get("score")),
        score_t=_optional_int(team_t.get("score")),
    )


def build_gsi_config(uri: str = "http://127.0.0.1:3000", token: str = "ze_joiner") -> str:
    return f'''"Ze Joiner"
{{
  "uri" "{uri}"
  "timeout" "5.0"
  "buffer" "0.1"
  "throttle" "0.5"
  "heartbeat" "30.0"
  "auth"
  {{
    "token" "{token}"
  }}
  "data"
  {{
    "provider" "1"
    "map" "1"
    "round" "1"
    "player_id" "1"
    "player_state" "1"
    "player_match_stats" "1"
  }}
}}
'''


def install_gsi_config(
    uri: str = "http://127.0.0.1:3000",
    token: str = "ze_joiner",
    steam_path: Path | None = None,
) -> GsiConfigInstallResult:
    content = build_gsi_config(uri=uri, token=token)
    installed_paths: list[Path] = []
    errors: list[str] = []

    for cfg_dir in find_cs2_cfg_dirs(steam_path=steam_path):
        path = cfg_dir / GSI_CONFIG_FILENAME
        try:
            path.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
        else:
            installed_paths.append(path)

    return GsiConfigInstallResult(installed_paths=tuple(installed_paths), errors=tuple(errors))


def find_cs2_cfg_dirs(steam_path: Path | None = None) -> list[Path]:
    steam_root = steam_path if steam_path is not None else _find_steam_path_from_registry()
    if steam_root is None:
        return []

    library_roots = _find_steam_library_roots(steam_root)
    if steam_root not in library_roots:
        library_roots.insert(0, steam_root)

    cfg_dirs = []
    seen = set()
    for library_root in library_roots:
        cfg_dir = (
            library_root
            / "steamapps"
            / "common"
            / CS2_INSTALL_DIR_NAME
            / "game"
            / "csgo"
            / "cfg"
        )
        normalized = str(cfg_dir).casefold()
        if normalized in seen or not cfg_dir.is_dir():
            continue
        seen.add(normalized)
        cfg_dirs.append(cfg_dir)
    return cfg_dirs


class GsiServer:
    def __init__(
        self,
        on_snapshot: Callable[[GsiSnapshot], None],
        host: str = "127.0.0.1",
        port: int = 3000,
        debug_logger: DebugLogger | None = None,
    ):
        self._on_snapshot = on_snapshot
        self._host = host
        self._port = port
        self._debug_logger = debug_logger
        self._server: _SnapshotHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def uri(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        if self.is_running:
            return
        if self._debug_logger is not None:
            self._debug_logger.log("gsi", "server start requested", host=self._host, port=self._port)
        server = _SnapshotHTTPServer(
            (self._host, self._port),
            _GsiRequestHandler,
            self._on_snapshot,
            self._debug_logger,
        )
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="cs2-gsi", daemon=True)
        self._thread.start()
        if self._debug_logger is not None:
            self._debug_logger.log("gsi", "server thread started", uri=self.uri)

    def stop(self) -> None:
        if self._server is None:
            return
        if self._debug_logger is not None:
            self._debug_logger.log("gsi", "server stop requested", uri=self.uri)
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        if self._debug_logger is not None:
            self._debug_logger.log("gsi", "server stopped")


class _SnapshotHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, request_handler_class, on_snapshot, debug_logger):
        super().__init__(server_address, request_handler_class)
        self.on_snapshot = on_snapshot
        self.debug_logger = debug_logger


class _GsiRequestHandler(BaseHTTPRequestHandler):
    server: _SnapshotHTTPServer

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            self._debug(
                "post received",
                path=self.path,
                length=length,
                content_type=self.headers.get("Content-Type", ""),
                user_agent=self.headers.get("User-Agent", ""),
            )
            raw_payload = self.rfile.read(length)
            raw_text = raw_payload.decode("utf-8")
            self._debug("raw payload", payload=raw_text)
            payload = json.loads(raw_text)
            snapshot = extract_gsi_snapshot(payload)
            game_map = _get_dict(payload, "map")
            team_ct = _get_dict(game_map, "team_ct")
            team_t = _get_dict(game_map, "team_t")
            self._debug(
                "parsed snapshot",
                player=snapshot.player_name,
                activity=snapshot.activity,
                map=snapshot.map_name,
                mode=snapshot.mode,
                phase=snapshot.phase,
                score_ct=snapshot.score_ct,
                score_t=snapshot.score_t,
                map_keys=sorted(game_map.keys()),
                team_ct_keys=sorted(team_ct.keys()),
                team_t_keys=sorted(team_t.keys()),
            )
            self.server.on_snapshot(snapshot)
        except Exception as exc:
            self._debug("post failed", error=repr(exc))
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args) -> None:
        return

    def _debug(self, message: str, **fields) -> None:
        logger = self.server.debug_logger
        if logger is not None:
            logger.log("gsi", message, **fields)


def _get_dict(source: dict, key: str) -> dict:
    value = source.get(key, {})
    return value if isinstance(value, dict) else {}


def _optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_steam_path_from_registry() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
    except OSError:
        return None
    path = Path(str(value).replace("/", "\\"))
    return path if path.is_dir() else None


def _find_steam_library_roots(steam_root: Path) -> list[Path]:
    roots = [steam_root]
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    if not library_file.is_file():
        return roots

    try:
        text = library_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return roots

    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
        roots.append(Path(match.group(1).replace("\\\\", "\\")))
    for match in re.finditer(r'"\d+"\s+"([^"]+)"', text):
        roots.append(Path(match.group(1).replace("\\\\", "\\")))

    unique_roots = []
    seen = set()
    for root in roots:
        normalized = str(root).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_roots.append(root)
    return unique_roots
