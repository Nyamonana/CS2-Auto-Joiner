from __future__ import annotations

import os
import webbrowser

from .a2s import ServerAddress


class SteamLauncher:
    def build_connect_uri(self, address: ServerAddress) -> str:
        return f"steam://connect/{address.host}:{address.port}"

    def join_server(self, address: ServerAddress) -> None:
        uri = self.build_connect_uri(address)
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        webbrowser.open(uri)
