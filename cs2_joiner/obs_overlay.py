"""Local OBS presentation, settings, and a thread-safe browser snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING

from .a2s import ServerAddress, ServerInfo
from .config_io import read_config, write_config

if TYPE_CHECKING:
    from .gsi import GsiSnapshot


SECTION = "ObsOverlay"
MISSING = "\u2014"
NUMERIC_LIMITS = {"opacity": (0, 100), "font_size": (12, 72), "width": (240, 1600), "padding": (0, 64)}


@dataclass(frozen=True)
class ObsOverlaySettings:
    enabled: bool = False
    opacity: int = 50
    font_size: int = 24
    width: int = 640
    padding: int = 12
    text_color: str = "#e8ece9"
    background_color: str = "#101513"
    alignment: str = "left"

    def __post_init__(self) -> None:
        for name, (low, high) in NUMERIC_LIMITS.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name.replace('_', ' ').title()} must be between {low} and {high}.")
        for color in (self.text_color, self.background_color):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("Colors must use #RRGGBB format.")
        if self.alignment not in ("left", "center", "right"):
            raise ValueError("Invalid text alignment.")


def load_obs_settings(path: Path) -> ObsOverlaySettings:
    parser = read_config(path)
    defaults = ObsOverlaySettings()
    values = {}
    for field in fields(defaults):
        default = getattr(defaults, field.name)
        try:
            if field.name == "enabled":
                value = parser.getboolean(SECTION, field.name, fallback=default)
            elif field.name in NUMERIC_LIMITS:
                value = parser.getint(SECTION, field.name, fallback=default)
            else:
                value = parser.get(SECTION, field.name, fallback=default)
            ObsOverlaySettings(**{field.name: value})
        except ValueError:
            value = default
        values[field.name] = value
    return ObsOverlaySettings(**values)


def save_obs_settings(path: Path, settings: ObsOverlaySettings) -> None:
    parser = read_config(path)
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    for name, value in asdict(settings).items():
        parser.set(SECTION, name, str(int(value)) if isinstance(value, bool) else str(value))
    write_config(path, parser)


def build_obs_lines(address: ServerAddress | None, info: ServerInfo | None,
                    snapshot: GsiSnapshot | None) -> tuple[str, str, str]:
    name = (info.name or MISSING) if info is not None else MISSING
    players = f"{info.players}/{info.max_players}" if info is not None else f"{MISSING}/{MISSING}"
    map_name = snapshot.map_name if snapshot is not None and snapshot.activity.casefold() != "menu" else ""
    return name, str(address) if address is not None else MISSING, f"{map_name or MISSING} ({players})"


class ObsOverlayState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.publish(ObsOverlaySettings(), build_obs_lines(None, None, None))

    def publish(self, settings: ObsOverlaySettings, lines: tuple[str, str, str]) -> None:
        # A missed GUI heartbeat must not leave stale information on a live stream.
        document = {"enabled": settings.enabled, "lines": lines if settings.enabled else (),
                    "style": asdict(settings)}
        encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        with self._lock:
            self._payload = encoded
            self._published_at = time.monotonic()

    def read(self) -> bytes:
        with self._lock:
            if time.monotonic() - self._published_at > 5:
                return b'{"enabled":false}'
            return self._payload


OBS_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CS2 Joiner OBS overlay</title>
<style>
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: transparent; }
body { font-family: "Segoe UI", "Yu Gothic UI", "Microsoft YaHei UI", sans-serif; }
#overlay { max-width: 100%; height: auto; line-height: 1.35; border-radius: 4px;
  font-weight: 500; letter-spacing: 0; overflow-wrap: anywhere; }
#overlay[hidden] { display: none; }
.line { white-space: pre-wrap; }
</style>
</head>
<body>
<div id="overlay" hidden><div class="line"></div><div class="line"></div><div class="line"></div></div>
<script>
"use strict";
const overlay = document.getElementById("overlay");
const lines = Array.from(overlay.children);
function render(state) {
  if (!state.enabled) { overlay.hidden = true; return; }
  const s = state.style;
  const hex = s.background_color;
  const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
  overlay.style.backgroundColor = `rgba(${rgb.join(",")},${s.opacity / 100})`;
  overlay.style.color = s.text_color;
  overlay.style.fontSize = `${s.font_size}px`;
  overlay.style.width = `${s.width}px`;
  overlay.style.padding = `${s.padding}px`;
  overlay.style.textAlign = s.alignment;
  lines.forEach((line, index) => { line.textContent = state.lines[index] || ""; });
  overlay.hidden = false;
}
async function refresh() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2000);
  try {
    const response = await fetch("/obs/state", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error("Overlay unavailable");
    render(await response.json());
  } catch (_) { overlay.hidden = true; }
  finally { clearTimeout(timeout); setTimeout(refresh, 1000); }
}
refresh();
</script>
</body>
</html>
'''
