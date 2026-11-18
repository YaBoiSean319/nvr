"""One oversized MQTT message does not end the events subscription (#382)."""
from __future__ import annotations

import asyncio
import struct
import unittest
from unittest.mock import patch

from app import mqtt


def publish(topic: str, payload: bytes, *, retained: bool = False, qos: int = 0, packet_id: int = 7) -> bytes:
    body = mqtt._string(topic) + (struct.pack("!H", packet_id) if qos else b"") + payload
    return mqtt._packet(0x30 | (qos << 1) | int(retained), body)


class Broker:
    """Enough of a broker: acknowledges the connection and the subscription, sends what it was given, then waits."""

    def __init__(self, messages: list[bytes], answer_pings: bool = True) -> None:
        self.messages, self.answer_pings, self.received = messages, answer_pings, bytearray()

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await mqtt._read_packet(reader, 4096)
            writer.write(bytes([0x20, 2, 0, 0]))
            _, body = await mqtt._read_packet(reader, 4096)
            writer.write(bytes([0x90, 3]) + body[:2] + bytes([1]))
            for message in self.messages:
                writer.write(message)
            await writer.drain()
            while data := await reader.read(64):
                self.received.extend(data)
                if self.answer_pings and b"\xc0\x00" in data:
                    writer.write(bytes([0xD0, 0]))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


class OversizedTests(unittest.IsolatedAsyncioTestCase):
    async def listen(self, broker: Broker, seconds: float = 1.0, **options):
        server = await asyncio.start_server(broker, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        got: list[tuple[str, bytes]] = []
        skipped: list[str] = []

        async def on_message(topic: str, payload: bytes) -> None:
            got.append((topic, payload))

        task = asyncio.create_task(mqtt.subscribe(mqtt.Broker(False, "127.0.0.1", port, "", ""), ["site/#"], on_message, on_skipped=skipped.append, **options))
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            error = task.exception() if task in done else None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()
        return got, skipped, error

    async def test_a_message_far_over_the_limit_is_skipped_and_the_event_behind_it_arrives(self):
        large = publish("site/camera/snapshot", b"x" * (mqtt.MAX_MESSAGE_BYTES + 6000), retained=True)
        huge = publish("site/camera/video", b"x" * (5 * 1024 * 1024))
        got, skipped, error = await self.listen(Broker([large, huge, publish("site/door/1", b'{"type":"opened"}')]))
        self.assertIsNone(error, f"the subscription ended: {error!r}")
        self.assertEqual(got, [("site/door/1", b'{"type":"opened"}')])
        self.assertEqual(skipped, ["site/camera/snapshot", "site/camera/video"])

    async def test_an_oversized_message_that_needs_acknowledging_is_acknowledged(self):
        broker = Broker([publish("site/camera/snapshot", b"x" * (mqtt.MAX_MESSAGE_BYTES + 6000), qos=1, packet_id=513)])
        _, skipped, error = await self.listen(broker, seconds=0.5)
        self.assertIsNone(error)
        self.assertEqual(skipped, ["site/camera/snapshot"])
        self.assertIn(bytes([0x40, 2]) + struct.pack("!H", 513), bytes(broker.received))

    async def test_the_caller_is_told_once_the_subscription_is_working(self):
        working: list[bool] = []
        await self.listen(Broker([]), seconds=0.3, on_subscribed=lambda: working.append(True))
        self.assertEqual(working, [True])

    async def test_a_broker_that_stops_answering_is_noticed(self):
        with patch.object(mqtt, "SILENCE_SECONDS", 0.3):
            _, _, error = await self.listen(Broker([], answer_pings=False), seconds=3)
        self.assertIsInstance(error, ConnectionError)


if __name__ == "__main__":
    unittest.main()
