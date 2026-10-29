"""A camera's RTSP server with an ONVIF audio backchannel, for the two-way audio tests (#147).

It answers DESCRIBE with an SDP that has the video track the camera sends and, when the request carries the backchannel requirement, a second audio track the
camera receives (``a=sendonly``), then SETUP and PLAY, and collects the RTP packets that arrive on the interleaved channel. Digest sign-in is optional.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import socketserver
import struct
import threading
import time

import numpy as np

REQUIRE = "www.onvif.org/ver20/backchannel"


def decode_ulaw(data: bytes) -> np.ndarray:
    values = (~np.frombuffer(data, dtype=np.uint8)).astype(np.int32) & 0xFF
    sign, exponent, mantissa = values & 0x80, (values >> 4) & 0x07, values & 0x0F
    magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -magnitude, magnitude).astype(np.int16)


def decode_alaw(data: bytes) -> np.ndarray:
    values = np.frombuffer(data, dtype=np.uint8).astype(np.int32) ^ 0x55
    sign, exponent, mantissa = values & 0x80, (values >> 4) & 0x07, values & 0x0F
    magnitude = np.where(exponent == 0, (mantissa << 4) + 8, ((mantissa << 4) + 0x108) << np.maximum(exponent - 1, 0))
    return np.where(sign != 0, magnitude, -magnitude).astype(np.int16)


class BackchannelCamera:
    def __init__(self, *, codec: str = "PCMU", backchannel: bool = True, username: str = "", password: str = "") -> None:
        self.codec, self.backchannel = codec, backchannel
        self.username, self.password = username, password
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.packets: list[tuple[int, int, int, bytes, float]] = []  # payload type, sequence, timestamp, payload, arrival time
        self.sessions = 0
        self.torn_down = 0
        self.lock = threading.Lock()
        self.nonce = secrets.token_hex(8)
        camera = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                camera.serve(self)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"rtsp://{username}:{password}@127.0.0.1:{self.port}/stream" if username else f"rtsp://127.0.0.1:{self.port}/stream"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- what arrived
    def received(self) -> list[bytes]:
        with self.lock:
            return [packet[3] for packet in self.packets]

    def sequence(self) -> list[tuple[int, int]]:
        with self.lock:
            return [(packet[1], packet[2]) for packet in self.packets]

    def samples(self) -> np.ndarray:
        decode = decode_ulaw if self.codec == "PCMU" else decode_alaw
        data = b"".join(self.received())
        return decode(data) if data else np.zeros(0, dtype=np.int16)

    def wait_for(self, count: int, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if len(self.packets) >= count:
                    return True
            time.sleep(0.02)
        return False

    # -- the protocol
    def authorised(self, method: str, headers: dict[str, str]) -> bool:
        if not self.username:
            return True
        header = headers.get("authorization", "")
        if not header.lower().startswith("digest "):
            return False
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        if fields.get("nonce") != self.nonce or fields.get("username") != self.username:
            return False
        ha1 = hashlib.md5(f"{self.username}:nvr-camera:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{fields.get('uri', '')}".encode()).hexdigest()
        if fields.get("qop") == "auth":
            expected = hashlib.md5(f"{ha1}:{self.nonce}:{fields['nc']}:{fields['cnonce']}:auth:{ha2}".encode()).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{self.nonce}:{ha2}".encode()).hexdigest()
        return fields.get("response") == expected

    def sdp(self) -> str:
        payload = 0 if self.codec == "PCMU" else 8
        lines = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=Camera", "t=0 0",
                 "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/90000", "a=recvonly", "a=control:track1"]
        if self.backchannel:
            lines += [f"m=audio 0 RTP/AVP {payload}", f"a=rtpmap:{payload} {self.codec}/8000", "a=sendonly", "a=control:track2"]
        return "\r\n".join(lines) + "\r\n"

    def serve(self, handler: socketserver.StreamRequestHandler) -> None:
        reader, writer = handler.rfile, handler.wfile

        def respond(cseq: str, status: str, headers: dict[str, str] | None = None, body: str = "") -> None:
            lines = [f"RTSP/1.0 {status}", f"CSeq: {cseq}"] + [f"{key}: {value}" for key, value in (headers or {}).items()]
            if body:
                lines += ["Content-Type: application/sdp", f"Content-Length: {len(body.encode())}"]
            writer.write(("\r\n".join(lines) + "\r\n\r\n" + body).encode())
            writer.flush()

        playing = False
        try:
            while True:
                first = reader.read(1)
                if not first:
                    return
                if first == b"$":
                    channel_and_length = reader.read(3)
                    channel, length = channel_and_length[0], struct.unpack(">H", channel_and_length[1:])[0]
                    data = reader.read(length)
                    if playing and channel == 0 and len(data) >= 12:
                        _, payload_type, sequence, timestamp, _ = struct.unpack(">BBHII", data[:12])
                        with self.lock:
                            self.packets.append((payload_type & 0x7F, sequence, timestamp, data[12:], time.monotonic()))
                    continue
                head = first + reader.readuntil(b"\r\n\r\n") if hasattr(reader, "readuntil") else first + self._until_blank(reader)
                lines = head.decode("latin-1").split("\r\n")
                method = lines[0].split()[0]
                headers = {key.strip().lower(): value.strip() for key, _, value in (line.partition(":") for line in lines[1:] if ":" in line)}
                cseq = headers.get("cseq", "0")
                with self.lock:
                    self.requests.append((method, headers))
                if not self.authorised(method, headers):
                    respond(cseq, "401 Unauthorized", {"WWW-Authenticate": f'Digest realm="nvr-camera", nonce="{self.nonce}", qop="auth"'})
                    continue
                if method == "OPTIONS":
                    respond(cseq, "200 OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})
                elif method == "DESCRIBE":
                    if REQUIRE in headers.get("require", "") and not self.backchannel:
                        respond(cseq, "551 Option not supported", {"Unsupported": REQUIRE})
                    else:
                        respond(cseq, "200 OK", {"Content-Base": f"rtsp://127.0.0.1:{self.port}/stream/"}, self.sdp())
                elif method == "SETUP":
                    with self.lock:
                        self.sessions += 1
                    respond(cseq, "200 OK", {"Session": "8F3A1C;timeout=60", "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
                elif method == "PLAY":
                    playing = True
                    respond(cseq, "200 OK", {"Session": "8F3A1C"})
                elif method == "TEARDOWN":
                    with self.lock:
                        self.torn_down += 1
                    respond(cseq, "200 OK")
                    return
                else:
                    respond(cseq, "405 Method Not Allowed")
        except (ConnectionError, OSError, ValueError, IndexError):
            return

    @staticmethod
    def _until_blank(reader) -> bytes:
        data = b""
        while not data.endswith(b"\r\n\r\n"):
            chunk = reader.read(1)
            if not chunk:
                break
            data += chunk
        return data
