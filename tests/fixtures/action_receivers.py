"""Local stand-ins for rule action destinations: an HTTP receiver (webhooks, Slack,
Teams), an MQTT broker and an ONVIF device with relay outputs. Each records what it
received so tests can assert exactly-once delivery."""
from __future__ import annotations

import socket
import socketserver
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HttpReceiver:
    """Answers each POST with the next status in ``statuses`` (the last one repeats)."""

    def __init__(self, statuses: tuple[int, ...] = (200,)) -> None:
        self.requests: list[dict] = []
        self.statuses = list(statuses)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                receiver.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = receiver.statuses.pop(0) if len(receiver.statuses) > 1 else receiver.statuses[0]
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _read_remaining_length(stream) -> int:
    multiplier, value = 1, 0
    while True:
        byte = stream.read(1)
        if not byte:
            raise ConnectionError("closed")
        value += (byte[0] & 0x7F) * multiplier
        if not byte[0] & 0x80:
            return value
        multiplier *= 128


def _string(data: bytes, offset: int) -> tuple[str, int]:
    length = struct.unpack("!H", data[offset:offset + 2])[0]
    return data[offset + 2:offset + 2 + length].decode("utf-8"), offset + 2 + length


class MqttBroker:
    """Enough MQTT 3.1.1 to accept a client: CONNECT with credentials, PUBLISH at QoS 0/1, DISCONNECT."""

    def __init__(self, username: str = "", password: str = "") -> None:
        self.hang_up = False  # Close the connection instead of acknowledging it.
        self.messages: list[dict] = []
        self.connections: list[dict] = []
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    while True:
                        header = self.rfile.read(1)
                        if not header:
                            return
                        kind, flags = header[0] >> 4, header[0] & 0x0F
                        data = self.rfile.read(_read_remaining_length(self.rfile))
                        if kind == 1 and broker.hang_up:
                            return
                        if kind == 1:  # CONNECT
                            _, offset = _string(data, 0)
                            connect_flags = data[offset + 1]
                            offset += 4
                            client_id, offset = _string(data, offset)
                            user = pw = ""
                            if connect_flags & 0x80:
                                user, offset = _string(data, offset)
                            if connect_flags & 0x40:
                                pw, offset = _string(data, offset)
                            broker.connections.append({"client_id": client_id, "username": user, "password": pw})
                            accepted = (not broker.username or (user, pw) == (broker.username, broker.password))
                            self.wfile.write(bytes([0x20, 2, 0, 0 if accepted else 5]))
                            if not accepted:
                                return
                        elif kind == 3:  # PUBLISH
                            qos = (flags >> 1) & 0x03
                            topic, offset = _string(data, 0)
                            packet_id = None
                            if qos:
                                packet_id = struct.unpack("!H", data[offset:offset + 2])[0]
                                offset += 2
                            broker.messages.append({"topic": topic, "payload": data[offset:], "qos": qos, "retain": bool(flags & 0x01)})
                            if qos == 1:
                                self.wfile.write(bytes([0x40, 2]) + struct.pack("!H", packet_id))
                        elif kind == 14:  # DISCONNECT
                            return
                except (ConnectionError, OSError):
                    return

        self.username, self.password = username, password
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RelayDevice:
    """An ONVIF device service with two relay outputs that records each state change and when it happened."""

    def __init__(self) -> None:
        self.changes: list[dict] = []
        self.failing_releases = 0  # Answer this many "inactive" requests with a 500 before obeying.
        device = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                if "GetRelayOutputs" in body:
                    reply = ('<tds:GetRelayOutputsResponse>'
                             '<tds:RelayOutputs token="relay-1"><tt:Properties><tt:Mode>Bistable</tt:Mode></tt:Properties></tds:RelayOutputs>'
                             '<tds:RelayOutputs token="relay-2"><tt:Properties><tt:Mode>Bistable</tt:Mode></tt:Properties></tds:RelayOutputs>'
                             '</tds:GetRelayOutputsResponse>')
                elif "SetRelayOutputState" in body:
                    token = body.split("RelayOutputToken>", 1)[1].split("<", 1)[0]
                    state = body.split("LogicalState>", 1)[1].split("<", 1)[0]
                    if state == "inactive" and device.failing_releases:
                        device.failing_releases -= 1
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    device.changes.append({"token": token, "state": state, "at": time.monotonic()})
                    reply = "<tds:SetRelayOutputStateResponse/>"
                else:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                envelope = ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
                            f'xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>{reply}</s:Body></s:Envelope>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/soap+xml")
                self.send_header("Content-Length", str(len(envelope)))
                self.end_headers()
                self.wfile.write(envelope)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/onvif/device_service"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
