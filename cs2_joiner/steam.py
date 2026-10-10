from __future__ import annotations

import os
import re
import webbrowser
from urllib.parse import quote

from .a2s import ServerAddress


class SteamLauncher:
    def build_connect_uri(self, address: ServerAddress) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", address.host) or not 1 <= address.port <= 65535:
            raise ValueError("Invalid server address for CS2 connect.")
        command = quote(f"+connect {address}", safe="+:")
        return f"steam://run/730//{command}/"

    def join_server(self, address: ServerAddress) -> None:
        uri = self.build_connect_uri(address)
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        webbrowser.open(uri)
