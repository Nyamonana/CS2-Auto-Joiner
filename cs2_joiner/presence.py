from __future__ import annotations

from dataclasses import dataclass

from .a2s import ServerInfo
from .discord_rpc import format_players, format_score
from .gsi import GsiSnapshot


@dataclass(frozen=True)
class PresencePreview:
    details: str
    state: str


def build_presence_preview(snapshot: GsiSnapshot) -> PresencePreview:
    map_name = snapshot.map_name or "Unknown map"
    if snapshot.score_ct is None or snapshot.score_t is None:
        score = "Score unavailable"
    else:
        score = format_score(snapshot.score_ct, snapshot.score_t)

    return PresencePreview(details=f"Playing {map_name}", state=score)


def build_server_presence_preview(info: ServerInfo) -> PresencePreview:
    map_name = info.map_name or "Unknown map"
    return PresencePreview(details=f"Playing {map_name}", state=f"Players {format_players(info)}")
