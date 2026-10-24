"""Publish one message to an MQTT 3.1.1 broker: connect, publish at QoS 0 or 1, disconnect.

Just enough of the protocol for rule actions; the broker URL is
``mqtt://user:password@host:1883`` or ``mqtts://…:8883`` (TLS, verified).
"""
from __future__ import annotations

import asyncio
import secrets
import ssl
import struct
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

TIMEOUT_SECONDS = 10
DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883}


class MqttRefused(Exception):
    """The broker refused the connection (bad credentials, not authorised…): retrying will not help."""

    def __init__(self, code: int) -> None:
        super().__init__(f"MQTT broker refused the connection ({code})")
        self.code = code


@dataclass(frozen=True)
class Broker:
    tls: bool
    host: str
    port: int
    username: str
    password: str


def parse_broker(url: str) -> Broker:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter the broker as mqtt://host:1883 or mqtts://host:8883") from exc
    if parsed.scheme not in DEFAULT_PORTS or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Enter the broker as mqtt://host:1883 or mqtts://host:8883")
    return Broker(parsed.scheme == "mqtts", parsed.hostname, port or DEFAULT_PORTS[parsed.scheme],
                  unquote(parsed.username or ""), unquote(parsed.password or ""))


def validate_topic(topic: str) -> str:
    topic = topic.strip()
    if not topic or len(topic.encode("utf-8")) > 1024 or "#" in topic or "+" in topic or "\x00" in topic:
        raise ValueError("Enter an MQTT topic without wildcards")
    return topic


def _string(value: str) -> bytes:
    data = value.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def _packet(kind: int, body: bytes) -> bytes:
    length, encoded = len(body), bytearray()
    while True:
        byte, length = length % 128, length // 128
        encoded.append(byte | (0x80 if length else 0))
        if not length:
            return bytes([kind]) + bytes(encoded) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = (await reader.readexactly(1))[0]
    multiplier, length = 1, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    return header, await reader.readexactly(length)


async def publish(broker: Broker, topic: str, payload: bytes, *, qos: int = 1, retain: bool = False) -> None:
    context = ssl.create_default_context() if broker.tls else None
    reader, writer = await asyncio.wait_for(asyncio.open_connection(broker.host, broker.port, ssl=context), TIMEOUT_SECONDS)
    try:
        flags = 0x02  # Clean session.
        credentials = b""
        if broker.username:
            flags |= 0x80
            credentials += _string(broker.username)
            if broker.password:
                flags |= 0x40
                credentials += _string(broker.password)
        client_id = f"nvr-{secrets.token_hex(6)}"
        writer.write(_packet(0x10, _string("MQTT") + bytes([4, flags]) + struct.pack("!H", 30) + _string(client_id) + credentials))
        await writer.drain()
        header, body = await asyncio.wait_for(_read_packet(reader), TIMEOUT_SECONDS)
        if header >> 4 != 2 or len(body) != 2:
            raise ConnectionError("MQTT broker did not acknowledge the connection")
        if body[1]:
            raise MqttRefused(body[1])
        packet_id = secrets.randbelow(65535) + 1
        variable = _string(topic) + (struct.pack("!H", packet_id) if qos else b"")
        writer.write(_packet(0x30 | (qos << 1) | int(retain), variable + payload))
        await writer.drain()
        if qos:
            header, body = await asyncio.wait_for(_read_packet(reader), TIMEOUT_SECONDS)
            if header >> 4 != 4 or body != struct.pack("!H", packet_id):
                raise ConnectionError("MQTT broker did not acknowledge the message")
        writer.write(bytes([0xE0, 0]))
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, ssl.SSLError):
            pass
