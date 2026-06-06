from __future__ import annotations

from dataclasses import dataclass
import socket
import struct
import time


DEFAULT_QUERY_PORT = 27015
A2S_INFO_REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"
SINGLE_PACKET_HEADER = -1
SPLIT_PACKET_HEADER = -2
INFO_RESPONSE_TYPE = ord("I")
CHALLENGE_RESPONSE_TYPE = ord("A")


class A2SProtocolError(RuntimeError):
    """Raised when an A2S response cannot be parsed."""


@dataclass(frozen=True)
class ServerAddress:
    host: str
    port: int = DEFAULT_QUERY_PORT

    def endpoint(self) -> tuple[str, int]:
        return (self.host, self.port)

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ServerInfo:
    name: str
    map_name: str
    folder: str
    game: str
    app_id: int
    players: int
    max_players: int
    bots: int
    server_type: str
    environment: str
    visibility: int
    vac_secured: bool
    version: str
    ping_ms: float = 0.0
    game_port: int | None = None
    steam_id: int | None = None
    spectator_port: int | None = None
    spectator_name: str = ""
    keywords: str = ""
    game_id: int | None = None

    @property
    def visible_players(self) -> int:
        return max(0, self.players - self.bots)

    @property
    def free_slots(self) -> int:
        return max(0, self.max_players - self.players)


class ByteReader:
    def __init__(self, data: bytes, offset: int = 0):
        self._data = data
        self._offset = offset

    @property
    def remaining(self) -> int:
        return len(self._data) - self._offset

    def read(self, length: int) -> bytes:
        if self.remaining < length:
            raise A2SProtocolError("A2S response ended unexpectedly.")
        chunk = self._data[self._offset : self._offset + length]
        self._offset += length
        return chunk

    def read_u8(self) -> int:
        return self.read(1)[0]

    def read_u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def read_u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def read_char(self) -> str:
        return self.read(1).decode("ascii", errors="replace")

    def read_cstring(self) -> str:
        end = self._data.find(b"\x00", self._offset)
        if end == -1:
            raise A2SProtocolError("A2S string is not null terminated.")
        value = self._data[self._offset : end].decode("utf-8", errors="replace")
        self._offset = end + 1
        return value


def parse_server_address(value: str) -> ServerAddress:
    text = value.strip().strip('"').strip("'")
    if not text:
        raise ValueError("Server address is empty.")

    lowered = text.lower()
    if lowered.startswith("steam://connect/"):
        text = text[len("steam://connect/") :].split("/", 1)[0]
    elif lowered.startswith("+connect "):
        text = text.split(None, 1)[1]
    elif lowered.startswith("connect "):
        text = text.split(None, 1)[1]

    text = text.strip().strip('"').strip("'")
    if " " in text:
        text = text.split()[0]

    host = text
    port = DEFAULT_QUERY_PORT
    if text.startswith("[") and "]:" in text:
        host_part, port_part = text.rsplit("]:", 1)
        host = host_part[1:]
        port = _parse_port(port_part)
    elif text.count(":") == 1:
        host_part, port_part = text.rsplit(":", 1)
        host = host_part.strip()
        port = _parse_port(port_part)

    if not host:
        raise ValueError("Server host is empty.")
    return ServerAddress(host=host, port=port)


def parse_info_response(data: bytes, ping_ms: float = 0.0) -> ServerInfo:
    if len(data) < 5:
        raise A2SProtocolError("A2S response is too short.")

    header = struct.unpack("<i", data[:4])[0]
    if header == SPLIT_PACKET_HEADER:
        raise A2SProtocolError("Split A2S packets are not supported for this query.")
    if header != SINGLE_PACKET_HEADER:
        raise A2SProtocolError("A2S response has an invalid header.")

    response_type = data[4]
    if response_type != INFO_RESPONSE_TYPE:
        raise A2SProtocolError(f"Expected A2S_INFO response, got 0x{response_type:02x}.")

    reader = ByteReader(data, offset=5)
    protocol = reader.read_u8()
    del protocol
    info = ServerInfo(
        name=reader.read_cstring(),
        map_name=reader.read_cstring(),
        folder=reader.read_cstring(),
        game=reader.read_cstring(),
        app_id=reader.read_u16(),
        players=reader.read_u8(),
        max_players=reader.read_u8(),
        bots=reader.read_u8(),
        server_type=reader.read_char(),
        environment=reader.read_char(),
        visibility=reader.read_u8(),
        vac_secured=bool(reader.read_u8()),
        version=reader.read_cstring(),
        ping_ms=ping_ms,
    )

    if reader.remaining <= 0:
        return info

    return _parse_extra_data_flags(reader, info)


def parse_challenge_response(data: bytes) -> bytes:
    if len(data) < 9:
        raise A2SProtocolError("A2S challenge response is too short.")
    header = struct.unpack("<i", data[:4])[0]
    if header != SINGLE_PACKET_HEADER or data[4] != CHALLENGE_RESPONSE_TYPE:
        raise A2SProtocolError("A2S response is not a challenge packet.")
    return data[5:9]


class A2SClient:
    def __init__(self, socket_factory=socket.socket):
        self._socket_factory = socket_factory

    def query_info(self, address: ServerAddress, timeout: float = 0.15) -> ServerInfo:
        started_at = time.perf_counter()
        with self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(A2S_INFO_REQUEST, address.endpoint())
            data, _ = sock.recvfrom(4096)

            if _is_challenge(data):
                challenge = parse_challenge_response(data)
                sock.sendto(A2S_INFO_REQUEST + challenge, address.endpoint())
                data, _ = sock.recvfrom(4096)

        ping_ms = (time.perf_counter() - started_at) * 1000.0
        return parse_info_response(data, ping_ms=ping_ms)


def _parse_extra_data_flags(reader: ByteReader, info: ServerInfo) -> ServerInfo:
    edf = reader.read_u8()
    game_port = None
    steam_id = None
    spectator_port = None
    spectator_name = ""
    keywords = ""
    game_id = None

    if edf & 0x80:
        game_port = reader.read_u16()
    if edf & 0x10:
        steam_id = reader.read_u64()
    if edf & 0x40:
        spectator_port = reader.read_u16()
        spectator_name = reader.read_cstring()
    if edf & 0x20:
        keywords = reader.read_cstring()
    if edf & 0x01:
        game_id = reader.read_u64()

    return ServerInfo(
        name=info.name,
        map_name=info.map_name,
        folder=info.folder,
        game=info.game,
        app_id=info.app_id,
        players=info.players,
        max_players=info.max_players,
        bots=info.bots,
        server_type=info.server_type,
        environment=info.environment,
        visibility=info.visibility,
        vac_secured=info.vac_secured,
        version=info.version,
        ping_ms=info.ping_ms,
        game_port=game_port,
        steam_id=steam_id,
        spectator_port=spectator_port,
        spectator_name=spectator_name,
        keywords=keywords,
        game_id=game_id,
    )


def _is_challenge(data: bytes) -> bool:
    return len(data) >= 5 and struct.unpack("<i", data[:4])[0] == SINGLE_PACKET_HEADER and data[4] == CHALLENGE_RESPONSE_TYPE


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("Server port must be a number.") from exc

    if not 1 <= port <= 65535:
        raise ValueError("Server port must be between 1 and 65535.")
    return port
