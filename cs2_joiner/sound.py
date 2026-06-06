from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import threading
import uuid
import webbrowser
from typing import Callable


SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".wav",
    ".wma",
}


@dataclass(frozen=True)
class SoundFile:
    name: str
    path: Path


def discover_sound_files(sound_dir: str | Path) -> list[SoundFile]:
    root = Path(sound_dir)
    if not root.exists() or not root.is_dir():
        return []

    sounds = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
            try:
                name = path.relative_to(root).as_posix()
            except ValueError:
                name = path.name
            sounds.append(SoundFile(name=name, path=path))

    return sorted(sounds, key=lambda sound: sound.name.casefold())


def play_notification_sound(
    enabled: bool,
    selected_path: Path | None,
    player: Callable[[Path], None],
) -> bool:
    if not enabled or selected_path is None:
        return False
    player(selected_path)
    return True


class SoundPlayer:
    def play(self, path: str | Path) -> None:
        sound_path = Path(path)
        if not sound_path.exists():
            raise FileNotFoundError(f"Sound file not found: {sound_path}")

        if sys.platform.startswith("win"):
            self._play_windows_async(sound_path)
            return

        webbrowser.open(sound_path.resolve().as_uri())

    def _play_windows_async(self, path: Path) -> None:
        thread = threading.Thread(target=self._play_windows_blocking, args=(path,), daemon=True)
        thread.start()

    def _play_windows_blocking(self, path: Path) -> None:
        alias = f"zejoiner_{uuid.uuid4().hex}"
        opened = False
        try:
            self._mci_send(f'open "{path}" alias {alias}')
            opened = True
            self._mci_send(f"play {alias} wait")
        finally:
            if opened:
                try:
                    self._mci_send(f"close {alias}")
                except RuntimeError:
                    return

    def _mci_send(self, command: str) -> str:
        import ctypes

        buffer = ctypes.create_unicode_buffer(255)
        result = ctypes.windll.winmm.mciSendStringW(command, buffer, len(buffer), None)
        if result == 0:
            return buffer.value

        error = ctypes.create_unicode_buffer(255)
        ctypes.windll.winmm.mciGetErrorStringW(result, error, len(error))
        raise RuntimeError(error.value or f"MCI error {result}")
