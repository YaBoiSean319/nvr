"""Publish to, or listen to, an MQTT 3.1.1 broker: publish one message at QoS 0 or 1, or subscribe and read what arrives.

Just enough of the protocol for rule actions, Home Assistant and events from building systems; the broker URL is
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
MAX_REPLY_BYTES = 256  # CONNACK and PUBACK are 2 bytes; anything large is not a broker we understand.
MAX_MESSAGE_BYTES = 64 * 1024  # The largest message a subscription accepts; a bigger one is skipped.
KEEPALIVE_SECONDS = 60
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


async def _read_packet(reader: asyncio.StreamReader, limit: int = MAX_REPLY_BYTES) -> tuple[int, bytes]:
    header = (await reader.readexactly(1))[0]
    multiplier, length = 1, 0
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    else:
        raise ConnectionError("MQTT reply length is invalid")
    if length > limit:
        raise ConnectionError("MQTT reply is too large")
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
            await asyncio.wait_for(writer.wait_closed(), 2)
        except (ConnectionError, ssl.SSLError, asyncio.TimeoutError):
            pass


def topic_matches(topic_filter: str, topic: str) -> bool:
    """Whether a topic matches an MQTT filter (``+`` is one level, a final ``#`` is the rest)."""
    wanted, actual = topic_filter.split("/"), topic.split("/")
    for index, level in enumerate(wanted):
        if level == "#":
            return index == len(wanted) - 1
        if index >= len(actual) or (level != "+" and level != actual[index]):
            return False
    return len(wanted) == len(actual)


def validate_filter(topic_filter: str) -> str:
    """A subscription filter: wildcards allowed, ``#`` only as the whole last level."""
    topic_filter = topic_filter.strip()
    levels = topic_filter.split("/")
    if not topic_filter or len(topic_filter.encode("utf-8")) > 1024 or "\x00" in topic_filter or any(("#" in level and (level != "#" or index != len(levels) - 1)) or ("+" in level and level != "+")
                                                                                                        for index, level in enumerate(levels)):
        raise ValueError("Enter an MQTT topic filter such as building/doors/# (a # only as the last level, a + only as a whole level)")
    return topic_filter


async def subscribe(broker: Broker, filters: list[str], on_message, *, client_id: str | None = None) -> None:
    """Connect, subscribe and hand each message to ``await on_message(topic, payload)`` until the connection ends (the caller reconnects).

    Messages larger than ``MAX_MESSAGE_BYTES`` are dropped. Raises ``MqttRefused`` when the broker refuses the login, ``ConnectionError`` or ``OSError`` when it goes away."""
    context = ssl.create_default_context() if broker.tls else None
    reader, writer = await asyncio.wait_for(asyncio.open_connection(broker.host, broker.port, ssl=context), TIMEOUT_SECONDS)
    ping: asyncio.Task[None] | None = None
    try:
        flags = 0x02
        credentials = b""
        if broker.username:
            flags |= 0x80
            credentials += _string(broker.username)
            if broker.password:
                flags |= 0x40
                credentials += _string(broker.password)
        writer.write(_packet(0x10, _string("MQTT") + bytes([4, flags]) + struct.pack("!H", KEEPALIVE_SECONDS) + _string(client_id or f"nvr-sub-{secrets.token_hex(6)}") + credentials))
        await writer.drain()
        header, body = await asyncio.wait_for(_read_packet(reader), TIMEOUT_SECONDS)
        if header >> 4 != 2 or len(body) != 2:
            raise ConnectionError("MQTT broker did not acknowledge the connection")
        if body[1]:
            raise MqttRefused(body[1])
        packet_id = secrets.randbelow(65534) + 1
        writer.write(_packet(0x82, struct.pack("!H", packet_id) + b"".join(_string(item) + bytes([1]) for item in filters)))
        await writer.drain()
        header, body = await asyncio.wait_for(_read_packet(reader), TIMEOUT_SECONDS)
        if header >> 4 != 9 or body[:2] != struct.pack("!H", packet_id) or any(code == 0x80 for code in body[2:]):
            raise ConnectionError("MQTT broker did not accept the subscription")

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(KEEPALIVE_SECONDS / 2)
                writer.write(bytes([0xC0, 0]))
                await writer.drain()

        ping = asyncio.create_task(keep_alive())
        while True:
            header, body = await _read_packet(reader, MAX_MESSAGE_BYTES + 2048)
            if header >> 4 != 3:
                continue  # PINGRESP, or anything else a broker may send
            qos = (header >> 1) & 0x03
            retained = bool(header & 1)
            length = struct.unpack("!H", body[:2])[0]
            topic, offset = body[2:2 + length].decode("utf-8", errors="replace"), 2 + length
            if qos:
                writer.write(bytes([0x40, 2]) + body[offset:offset + 2])  # PUBACK
                await writer.drain()
                offset += 2
            payload = body[offset:]
            # A retained message is a stored copy the broker replays on every connect, not something that just happened.
            if len(payload) <= MAX_MESSAGE_BYTES and not retained:
                await on_message(topic, payload)
    finally:
        if ping:
            ping.cancel()
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except (ConnectionError, ssl.SSLError, asyncio.TimeoutError, OSError):
            pass
