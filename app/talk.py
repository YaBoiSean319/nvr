"""Two-way audio (#147): talking through a camera's speaker over the ONVIF audio backchannel.

The browser sends the microphone as 8 kHz mono 16-bit PCM over a WebSocket (``app/api/talk.py``). This module turns that into G.711 and delivers it to the camera:
an RTSP session opened with the ONVIF backchannel requirement (``Require: www.onvif.org/ver20/backchannel``), in which the camera describes an audio track it
receives (``a=sendonly`` in its SDP), set up over the RTSP connection itself (interleaved RTP) and played; the audio then goes out as RTP packets on that
connection. Only one person talks through a camera at a time (``Talkers``).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import itertools
import json
import re
import secrets
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlsplit

import numpy as np

from app.audit import record
from app.store import connect

BACKCHANNEL_REQUIRE = "www.onvif.org/ver20/backchannel"
SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20 ms
MAX_INPUT_BYTES = 16000  # One message from the browser: a second of audio is far more than it sends
MAX_TALK_SECONDS = 300
IDLE_SECONDS = 10
RECORDING_KEEP_DAYS = 30
ROLES = ("administrator", "operator")
CONNECT_TIMEOUT = 5.0
USER_AGENT = "NVR-talk/1.0"


class BackchannelError(Exception):
    """The camera could not be talked through."""


class BackchannelUnsupported(BackchannelError):
    """The camera has no speaker we can send to (no backchannel, or a codec other than G.711)."""


class TalkBusy(Exception):
    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder


# --- G.711 -------------------------------------------------------------------------------------------------------------------------------

def encode_ulaw(pcm: np.ndarray) -> bytes:
    """G.711 mu-law from 16-bit samples."""
    samples = pcm.astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(samples), 32635) + 0x84
    exponent = np.floor(np.log2(magnitude)).astype(np.int32) - 7
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return bytes((~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8))


def encode_alaw(pcm: np.ndarray) -> bytes:
    """G.711 A-law from 16-bit samples."""
    samples = pcm.astype(np.int32)
    sign = np.where(samples >= 0, 0x80, 0)
    magnitude = np.minimum(np.where(samples < 0, -samples - 1, samples), 32767)
    exponent = np.where(magnitude >= 256, np.floor(np.log2(np.maximum(magnitude, 1))).astype(np.int32) - 7, 0)
    mantissa = np.where(magnitude >= 256, (magnitude >> (np.maximum(exponent, 1) + 3)) & 0x0F, magnitude >> 4)
    return bytes(((sign | (exponent << 4) | mantissa) ^ 0x55).astype(np.uint8))


ENCODERS = {"PCMU": encode_ulaw, "PCMA": encode_alaw}
PAYLOAD_TYPES = {"PCMU": 0, "PCMA": 8}


# --- RTSP backchannel ------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioTrack:
    codec: str
    payload_type: int
    control: str


def parse_sdp(text: str) -> list[AudioTrack]:
    """The audio tracks the camera receives (marked ``a=sendonly`` from its side) that carry G.711, in the order the camera lists them."""
    tracks: list[AudioTrack] = []
    media: dict[str, object] | None = None

    def close() -> None:
        if not media or media["kind"] != "audio" or media["direction"] != "sendonly":
            return
        wanted = {PAYLOAD_TYPES[name]: name for name in PAYLOAD_TYPES}
        names = dict(media["names"])  # payload type -> codec name from a=rtpmap
        for payload in media["formats"]:
            name = names.get(payload) or wanted.get(payload)
            if name in ENCODERS and PAYLOAD_TYPES[name] == payload or (name in ENCODERS and payload >= 96):
                tracks.append(AudioTrack(name, payload, str(media["control"] or "")))
                return

    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if line.startswith("m="):
            close()
            parts = line[2:].split()
            media = {"kind": parts[0] if parts else "", "formats": [int(item) for item in parts[3:] if item.isdigit()], "direction": "", "names": [], "control": ""}
        elif media is not None and line.startswith("a="):
            attribute = line[2:]
            if attribute in {"sendonly", "recvonly", "sendrecv", "inactive"}:
                media["direction"] = attribute
            elif attribute.startswith("rtpmap:"):
                match = re.match(r"rtpmap:(\d+)\s+([A-Za-z0-9._-]+)/(\d+)", attribute)
                if match and int(match.group(3)) == SAMPLE_RATE:
                    media["names"].append((int(match.group(1)), match.group(2).upper()))
            elif attribute.startswith("control:"):
                media["control"] = attribute[len("control:"):].strip()
    close()
    return tracks


def _digest(challenge: str, username: str, password: str, method: str, uri: str) -> str:
    fields = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
    realm, nonce, qop = fields.get("realm", ""), fields.get("nonce", ""), fields.get("qop", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()  # noqa: S324 - RTSP digest is MD5 by definition
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # noqa: S324
    if "auth" in qop.split(","):
        cnonce, count = secrets.token_hex(8), "00000001"
        response = hashlib.md5(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}".encode()).hexdigest()  # noqa: S324
        extra = f', qop=auth, nc={count}, cnonce="{cnonce}"'
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()  # noqa: S324
        extra = ""
    opaque = f', opaque="{fields["opaque"]}"' if "opaque" in fields else ""
    return f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"{extra}{opaque}'


class RtspBackchannel:
    """One RTSP session in which this process sends audio to a camera. Use ``connect`` then ``send`` then ``close``."""

    def __init__(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "rtsp" or not parts.hostname:
            raise BackchannelUnsupported("Talking needs a plain rtsp:// camera address")
        self.host, self.port = parts.hostname, parts.port or 554
        self.username, self.password = unquote(parts.username or ""), unquote(parts.password or "")
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        self.url = f"rtsp://{netloc}:{self.port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
        self.track: AudioTrack | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = itertools.count(1)
        self._session = ""
        self._challenge = ""
        self._channel = 0
        self._sequence = secrets.randbelow(65536)
        self._timestamp = secrets.randbelow(2**32)
        self._ssrc = secrets.randbelow(2**32)
        self._drain: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._responses: dict[int, asyncio.Future] = {}
        self.closed = asyncio.Event()
        self.packets = 0

    # -- the wire
    async def _read_frames(self) -> None:
        """Read everything the camera sends: RTSP responses go to whoever is waiting for them, interleaved media (the camera has none for us) is dropped."""
        reader = self._reader
        assert reader is not None
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    header = await reader.readexactly(3)
                    await reader.readexactly(struct.unpack(">H", header[1:])[0])
                    continue
                head = first + await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
                headers = {key.strip().lower(): value.strip() for key, _, value in (line.partition(":") for line in lines[1:] if ":" in line)}
                body = await reader.readexactly(int(headers.get("content-length", "0") or 0))
                future = self._responses.pop(int(headers.get("cseq", "0") or 0), None)
                if future is not None and not future.done():
                    future.set_result((status, headers, body))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError, ValueError, IndexError):
            pass
        finally:
            self.closed.set()
            for future in self._responses.values():
                if not future.done():
                    future.set_exception(BackchannelError("The camera closed the connection"))

    async def _request(self, method: str, url: str, headers: dict[str, str] | None = None, *, wait: bool = True, authorise: bool = True) -> tuple[int, dict[str, str], bytes]:
        writer = self._writer
        if writer is None or self.closed.is_set():
            raise BackchannelError("The camera closed the connection")
        cseq = next(self._cseq)
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {USER_AGENT}", f"Require: {BACKCHANNEL_REQUIRE}"]
        if self._session:
            lines.append(f"Session: {self._session}")
        if authorise and self._challenge and self.username:
            if self._challenge.lower().startswith("digest"):
                lines.append(f"Authorization: {_digest(self._challenge, self.username, self.password, method, url)}")
            else:
                import base64
                lines.append("Authorization: Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode())
        lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
        future: asyncio.Future | None = None
        if wait:
            future = asyncio.get_running_loop().create_future()
            self._responses[cseq] = future
        async with self._lock:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
        if future is None:
            return 0, {}, b""
        try:
            return await asyncio.wait_for(future, CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            self._responses.pop(cseq, None)
            raise BackchannelError(f"The camera did not answer {method}") from exc

    async def _authorised(self, method: str, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        status, response, body = await self._request(method, url, headers)
        if status == 401 and response.get("www-authenticate") and self.username:
            self._challenge = response["www-authenticate"]
            status, response, body = await self._request(method, url, headers)
        if status == 401:
            raise BackchannelError("The camera refused the sign-in")
        return status, response, body

    # -- the session
    async def connect(self) -> AudioTrack:
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BackchannelError("The camera cannot be reached") from exc
        self._drain = asyncio.create_task(self._read_frames())
        try:
            status, headers, body = await self._authorised("DESCRIBE", self.url, {"Accept": "application/sdp"})
            if status in {551, 405, 501} or (status == 400 and not body):
                raise BackchannelUnsupported("This camera does not offer an audio backchannel")
            if status != 200:
                raise BackchannelError(f"The camera answered DESCRIBE with {status}")
            tracks = parse_sdp(body.decode("utf-8", "replace"))
            if not tracks:
                raise BackchannelUnsupported("This camera's speaker does not take G.711 audio, or it has no backchannel")
            self.track = tracks[0]
            base = headers.get("content-base") or self.url
            control = self.track.control
            target = base if not control or control == "*" else control if "://" in control else urljoin(base if base.endswith("/") else base + "/", control)
            status, headers, _ = await self._authorised("SETUP", target, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
            if status != 200:
                raise BackchannelError(f"The camera would not set up its speaker ({status})")
            self._session = headers.get("session", "").split(";")[0].strip()
            timeout = re.search(r"timeout=(\d+)", headers.get("session", ""))
            match = re.search(r"interleaved=(\d+)", headers.get("transport", ""))
            self._channel = int(match.group(1)) if match else 0
            status, _, _ = await self._authorised("PLAY", self.url, {"Range": "npt=0.000-"})
            if status != 200:
                raise BackchannelError(f"The camera would not start its speaker ({status})")
            self._keepalive = asyncio.create_task(self._keep_alive(int(timeout.group(1)) if timeout else 60))
            return self.track
        except BaseException:
            await self.close()
            raise

    async def _keep_alive(self, timeout: int) -> None:
        with contextlib.suppress(Exception):
            while not self.closed.is_set():
                await asyncio.sleep(max(5, min(timeout // 2, 25)))
                await self._request("OPTIONS", self.url, wait=False)

    async def send(self, payload: bytes) -> None:
        """Send one RTP packet's worth of encoded audio."""
        writer, track = self._writer, self.track
        if writer is None or track is None or self.closed.is_set():
            raise BackchannelError("The camera closed the connection")
        header = struct.pack(">BBHII", 0x80, track.payload_type, self._sequence, self._timestamp, self._ssrc)
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + len(payload)) & 0xFFFFFFFF
        packet = header + payload
        async with self._lock:
            writer.write(b"$" + bytes([self._channel]) + struct.pack(">H", len(packet)) + packet)
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
        self.packets += 1

    async def close(self) -> None:
        for task in (self._keepalive,):
            if task is not None:
                task.cancel()
        if self._writer is not None and not self.closed.is_set() and self._session:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._request("TEARDOWN", self.url), 1.0)
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        if self._drain is not None:
            self._drain.cancel()
        self.closed.set()


# --- one talker at a time -----------------------------------------------------------------------------------------------------------

@dataclass
class Talk:
    id: str
    camera_id: str
    user_id: int
    username: str
    started_at: str
    started: float = field(default_factory=time.monotonic)


class Talkers:
    """Who is talking through which camera, in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Talk] = {}

    def begin(self, camera_id: str, user_id: int, username: str) -> Talk:
        with self._lock:
            held = self._active.get(camera_id)
            if held is not None:
                raise TalkBusy(held.username)
            talk = Talk(secrets.token_hex(16), camera_id, user_id, username, datetime.now(timezone.utc).isoformat())
            self._active[camera_id] = talk
            return talk

    def end(self, talk: Talk) -> None:
        with self._lock:
            if self._active.get(talk.camera_id) is talk:
                del self._active[talk.camera_id]

    def holder(self, camera_id: str) -> dict[str, str] | None:
        with self._lock:
            held = self._active.get(camera_id)
        return {"username": held.username, "since": held.started_at} if held else None

    def holders(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {camera_id: {"username": held.username, "since": held.started_at} for camera_id, held in self._active.items()}


talkers = Talkers()


# --- settings and the record --------------------------------------------------------------------------------------------------------

def settings(db, camera_id: str) -> dict[str, object]:
    row = db.execute("SELECT enabled,roles_json,record_audio FROM camera_talk WHERE camera_id=?", (camera_id,)).fetchone()
    if row is None:
        return {"enabled": False, "roles": ["administrator"], "record_audio": False}
    try:
        roles = [role for role in json.loads(row["roles_json"]) if role in ROLES]
    except (TypeError, ValueError):
        roles = []
    return {"enabled": bool(row["enabled"]), "roles": roles, "record_audio": bool(row["record_audio"])}


def allowed(config: dict[str, object], role: str) -> bool:
    return bool(config["enabled"]) and role in config["roles"]  # type: ignore[operator]


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return out.getvalue()


def begin_record(talk: Talk, codec: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO talk_sessions(id,camera_id,user_id,username,started_at,codec) VALUES(?,?,?,?,?,?)",
                   (talk.id, talk.camera_id, talk.user_id, talk.username, talk.started_at, codec))
        record(db, talk.user_id, "talk_started", "camera", talk.camera_id, session=talk.id, codec=codec)


def end_record(talk: Talk, reason: str, pcm: bytes | None) -> None:
    seconds = round(time.monotonic() - talk.started, 2)
    with connect() as db:
        db.execute("UPDATE talk_sessions SET ended_at=?,seconds=?,ended_reason=?,audio_wav=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(), seconds, reason, wav_bytes(pcm) if pcm else None, talk.id))
        record(db, talk.user_id, "talk_ended", "camera", talk.camera_id, session=talk.id, seconds=seconds, reason=reason, recorded=bool(pcm))
        db.execute("UPDATE talk_sessions SET audio_wav=NULL WHERE audio_wav IS NOT NULL AND started_at < ?",
                   (datetime.fromtimestamp(time.time() - RECORDING_KEEP_DAYS * 86400, timezone.utc).isoformat(),))


class Talking:
    """One talk in progress: turns what the browser sends into G.711 packets for the camera, and keeps the audio when asked to."""

    def __init__(self, backchannel: RtspBackchannel, keep_audio: bool) -> None:
        assert backchannel.track is not None
        self.backchannel = backchannel
        self.encode = ENCODERS[backchannel.track.codec]
        self.keep = bytearray() if keep_audio else None
        self._pending = np.zeros(0, dtype=np.int16)

    async def feed(self, data: bytes) -> None:
        if not data or len(data) > MAX_INPUT_BYTES or len(data) % 2:
            raise ValueError("Audio must be 16-bit samples, in pieces of up to a second")
        if self.keep is not None:
            self.keep += data
        self._pending = np.concatenate([self._pending, np.frombuffer(data, dtype="<i2")])
        while len(self._pending) >= FRAME_SAMPLES:
            frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
            await self.backchannel.send(self.encode(frame))
