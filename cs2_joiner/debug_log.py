from __future__ import annotations

from pathlib import Path
from typing import Any


class DebugLogger:
    def __init__(self, path: Path | None = None, enabled: bool = False):
        # Distribution builds keep file logging disabled.
        del enabled
        self.path = path
        self.enabled = False

    def log(self, category: str, message: str, **fields: Any) -> None:
        del category, message, fields
        return


def mask_client_id(client_id: str) -> str:
    text = client_id.strip()
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]
