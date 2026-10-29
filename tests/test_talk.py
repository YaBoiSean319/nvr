"""Two-way audio (#147): the G.711 encoders, the RTSP backchannel client against a camera stand-in, and the talk API."""
from __future__ import annotations

import asyncio
import io
import json
import time
import unittest
import wave

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import talk
from app.credentials import encrypt
from app.security import hash_password
from app.store import connect
from tests.fixtures.rtsp_backchannel import BackchannelCamera, decode_alaw, decode_ulaw
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD


def tone(samples: int = 160, frequency: float = 440.0, amplitude: float = 12000.0, start: int = 0) -> bytes:
    t = (np.arange(samples) + start) / talk.SAMPLE_RATE
    return (np.sin(2 * np.pi * frequency * t) * amplitude).astype("<i2").tobytes()


class EncoderTests(unittest.TestCase):
    def test_g711_round_trips_within_its_quantisation(self):
        samples = np.concatenate([np.arange(-32768, 32768, 7), np.array([-32768, -1, 0, 1, 32767])]).astype(np.int16)
        for encode, decode in ((talk.encode_ulaw, decode_ulaw), (talk.encode_alaw, decode_alaw)):
            decoded = decode(encode(samples)).astype(np.int32)
            error = np.abs(decoded - samples.astype(np.int32))
            # G.711 keeps about 13 bits: the step is at most 1/16 of the level above the quietest segments.
            allowed = np.maximum(16, np.abs(samples.astype(np.int32)) // 16 + 16)
            self.assertTrue(np.all(error <= allowed), (encode.__name__, int(error.max())))

    def test_silence_and_known_codes(self):
        self.assertEqual(talk.encode_ulaw(np.zeros(2, dtype=np.int16)), b"\xff\xff")
        self.assertEqual(talk.encode_alaw(np.zeros(2, dtype=np.int16)), b"\xd5\xd5")
        self.assertEqual(talk.encode_ulaw(np.array([32767], dtype=np.int16)), b"\x80")
        self.assertEqual(talk.encode_ulaw(np.array([-32768], dtype=np.int16)), b"\x00")

    def test_sdp_picks_the_speaker_track_the_camera_receives(self):
        text = ("v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=recvonly\r\na=control:track1\r\n"
                "m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=recvonly\r\na=control:track2\r\n"
                "m=audio 0 RTP/AVP 8 97\r\na=rtpmap:8 PCMA/8000\r\na=sendonly\r\na=control:track3\r\n")
        tracks = talk.parse_sdp(text)
        self.assertEqual([(track.codec, track.payload_type, track.control) for track in tracks], [("PCMA", 8, "track3")])
        self.assertEqual(talk.parse_sdp("v=0\r\nm=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/16000\r\na=sendonly\r\n"), [])


class BackchannelTests(unittest.TestCase):
    def run_session(self, camera: BackchannelCamera, frames: int = 5) -> None:
        async def go():
            channel = talk.RtspBackchannel(camera.url)
            track = await channel.connect()
            self.assertEqual(track.codec, camera.codec)
            talking = talk.Talking(channel, False)
            for index in range(frames):
                await talking.feed(tone(160, start=index * 160))
            await channel.close()
        asyncio.run(go())

    def test_signs_in_with_digest_and_delivers_packets_in_order(self):
        camera = BackchannelCamera(username="operator", password="S3cret!pass")
        self.addCleanup(camera.close)
        self.run_session(camera, 6)
        self.assertTrue(camera.wait_for(6))
        sequence = camera.sequence()
        for (first_seq, first_ts), (second_seq, second_ts) in zip(sequence, sequence[1:]):
            self.assertEqual(second_seq, (first_seq + 1) & 0xFFFF)
            self.assertEqual(second_ts, (first_ts + 160) & 0xFFFFFFFF)
        methods = [method for method, _ in camera.requests]
        self.assertEqual([m for m in methods if m != "OPTIONS"][:2], ["DESCRIBE", "DESCRIBE"])  # The first is challenged
        self.assertIn("TEARDOWN", methods)
        self.assertTrue(all(headers.get("require") == talk.BACKCHANNEL_REQUIRE for method, headers in camera.requests if method in {"DESCRIBE", "SETUP", "PLAY"}))
        sent = np.frombuffer(b"".join(tone(160, start=index * 160) for index in range(6)), dtype="<i2").astype(np.int32)
        heard = camera.samples().astype(np.int32)
        self.assertEqual(len(heard), len(sent))
        self.assertLess(float(np.abs(heard - sent).max()), 1000)

    def test_a_camera_that_takes_alaw_is_sent_alaw(self):
        camera = BackchannelCamera(codec="PCMA")
        self.addCleanup(camera.close)
        self.run_session(camera, 3)
        self.assertTrue(camera.wait_for(3))
        self.assertEqual({packet[0] for packet in camera.packets}, {8})

    def test_a_camera_without_a_backchannel_is_unsupported(self):
        camera = BackchannelCamera(backchannel=False)
        self.addCleanup(camera.close)
        with self.assertRaises(talk.BackchannelUnsupported):
            asyncio.run(talk.RtspBackchannel(camera.url).connect())

    def test_a_wrong_password_is_refused(self):
        camera = BackchannelCamera(username="operator", password="right")
        self.addCleanup(camera.close)
        with self.assertRaises(talk.BackchannelError):
            asyncio.run(talk.RtspBackchannel(f"rtsp://operator:wrong@127.0.0.1:{camera.port}/stream").connect())


class TalkApiTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.camera = BackchannelCamera(username="operator", password="S3cret!pass")
        self.addCleanup(self.camera.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(self.camera.url), self.camera_id))
        self.path = f"/api/cameras/{self.camera_id}/talk"
        self.stream = f"{self.path}/stream"

    def enable(self, **changes) -> dict:
        body = {"enabled": True, "roles": ["administrator"], "record_audio": False} | changes
        response = self.client.put(self.path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def operator(self, name: str = "watcher") -> TestClient:
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (name, hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD}).status_code, 200)
        return client

    def refused(self, client: TestClient) -> tuple[dict, int]:
        with client.websocket_connect(self.stream) as websocket:
            message = websocket.receive_json()
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        return message, closed.exception.code

    def audit(self, action: str) -> list[dict]:
        with connect() as db:
            rows = db.execute("SELECT actor_id,details_json FROM audit_log WHERE action=? AND resource_id=? ORDER BY id", (action, self.camera_id)).fetchall()
        return [{"actor": row["actor_id"], **json.loads(row["details_json"])} for row in rows]

    def test_holding_talk_delivers_audio_and_releasing_stops_it_at_once(self):
        self.enable()
        with self.client.websocket_connect(self.stream) as websocket:
            ready = websocket.receive_json()
            self.assertEqual((ready["type"], ready["codec"]), ("ready", "PCMU"))
            self.assertEqual(self.client.get("/api/talk").json()["cameras"][self.camera_id]["talking"]["username"], "keeper")
            for index in range(10):
                websocket.send_bytes(tone(160, start=index * 160))
            self.assertTrue(self.camera.wait_for(10))
            websocket.send_text(json.dumps({"type": "stop"}))
            ended = websocket.receive_json()
            self.assertEqual((ended["type"], ended["reason"]), ("ended", "released"))
        delivered = len(self.camera.packets)
        self.assertEqual(delivered, 10)
        time.sleep(0.3)
        self.assertEqual(len(self.camera.packets), delivered)  # Nothing more after release
        self.assertEqual(self.camera.torn_down, 1)
        self.assertIsNone(self.client.get("/api/talk").json()["cameras"][self.camera_id]["talking"])
        self.assertEqual(self.client.get(self.path).json()["talking"], None)

    def test_a_second_talker_is_refused_while_one_is_active_and_may_talk_after(self):
        self.enable(roles=["administrator", "operator"])
        other = self.operator()
        with self.client.websocket_connect(self.stream) as first:
            self.assertEqual(first.receive_json()["type"], "ready")
            message, code = self.refused(other)
            self.assertEqual((message["type"], code, message["talker"]), ("busy", 4429, "keeper"))
            self.assertEqual(other.get(self.path).json()["talking"]["username"], "keeper")
            first.send_text(json.dumps({"type": "stop"}))
            first.receive_json()
        with other.websocket_connect(self.stream) as second:
            self.assertEqual(second.receive_json()["type"], "ready")
            second.send_bytes(tone())
            second.send_text(json.dumps({"type": "stop"}))
            self.assertEqual(second.receive_json()["reason"], "released")

    def test_closing_the_socket_releases_the_camera_too(self):
        self.enable()
        with self.client.websocket_connect(self.stream) as websocket:
            websocket.receive_json()
            websocket.send_bytes(tone())
        for _ in range(50):
            if talk.talkers.holder(self.camera_id) is None:
                break
            time.sleep(0.05)
        self.assertIsNone(talk.talkers.holder(self.camera_id))

    def test_talking_is_refused_unless_enabled_for_the_role(self):
        message, code = self.refused(self.client)  # Off by default
        self.assertEqual((message["type"], code), ("forbidden", 4403))
        self.enable()
        other = self.operator()
        message, code = self.refused(other)  # On, but for administrators only
        self.assertEqual((message["type"], code), ("forbidden", 4403))
        self.assertFalse(other.get("/api/talk").json()["cameras"])
        self.enable(roles=["administrator", "operator"])
        self.assertIn(self.camera_id, other.get("/api/talk").json()["cameras"])
        self.assertEqual(other.put(self.path, json={"enabled": False, "roles": [], "record_audio": False}).status_code, 403)
        self.assertNotIn("roles", other.get(self.path).json())  # The setting is the administrator's

    def test_settings_are_validated_and_audited(self):
        self.assertEqual(self.client.put(self.path, json={"enabled": True, "roles": []}).status_code, 422)
        self.assertEqual(self.client.put(self.path, json={"enabled": True, "roles": ["root"]}).status_code, 422)
        self.assertEqual(self.client.put(f"/api/cameras/{self.camera_id[:-1]}x/talk", json={"enabled": False, "roles": []}).status_code, 404)
        self.enable(record_audio=True)
        self.assertEqual(self.audit("camera_talk_changed")[-1]["record_audio"], True)

    def test_sessions_are_audited_and_the_audio_is_kept_only_when_asked(self):
        self.enable()
        with self.client.websocket_connect(self.stream) as websocket:
            websocket.receive_json()
            websocket.send_bytes(tone())
            websocket.send_text(json.dumps({"type": "stop"}))
            self.assertFalse(websocket.receive_json()["recorded"])
        self.assertEqual([entry["actor"] for entry in self.audit("talk_started")], [1])
        self.assertEqual(self.audit("talk_ended")[0]["recorded"], False)
        sessions = self.client.get(f"{self.path}/sessions").json()["sessions"]
        self.assertEqual((len(sessions), sessions[0]["has_audio"], sessions[0]["ended_reason"]), (1, False, "released"))
        self.assertEqual(self.client.get(f"{self.path}/sessions/{sessions[0]['id']}/audio").status_code, 404)

        self.enable(record_audio=True)
        with self.client.websocket_connect(self.stream) as websocket:
            websocket.receive_json()
            for index in range(5):
                websocket.send_bytes(tone(160, start=index * 160))
            websocket.send_text(json.dumps({"type": "stop"}))
            self.assertTrue(websocket.receive_json()["recorded"])
        kept = [session for session in self.client.get(f"{self.path}/sessions").json()["sessions"] if session["has_audio"]]
        self.assertEqual(len(kept), 1)
        audio = self.client.get(f"{self.path}/sessions/{kept[0]['id']}/audio")
        self.assertEqual((audio.status_code, audio.headers["content-type"]), (200, "audio/wav"))
        with wave.open(io.BytesIO(audio.content)) as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()), (1, 2, 8000, 800))
        self.assertEqual(len(self.audit("talk_audio_downloaded")), 1)

    def test_a_camera_without_a_speaker_says_so(self):
        self.enable()
        silent = BackchannelCamera(backchannel=False)
        self.addCleanup(silent.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(silent.url), self.camera_id))
        message, code = self.refused(self.client)
        self.assertEqual((message["type"], code), ("unsupported", 4415))
        self.assertIsNone(talk.talkers.holder(self.camera_id))  # The slot is not left held
        self.assertEqual(self.client.get(f"{self.path}/sessions").json()["sessions"], [])

    def test_bad_audio_ends_the_talk(self):
        self.enable()
        with self.client.websocket_connect(self.stream) as websocket:
            websocket.receive_json()
            websocket.send_bytes(b"\x01\x02\x03")  # Not whole 16-bit samples
            self.assertEqual(websocket.receive_json()["reason"], "bad_audio")

    def test_a_signed_in_person_who_cannot_see_the_camera_gets_not_found(self):
        self.enable(roles=["administrator", "operator"])
        other = self.operator()
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at='2026-01-01T00:00:00Z' WHERE id=?", (self.camera_id,))
        message, code = self.refused(other)
        self.assertEqual((message["type"], code), ("not_found", 4404))
        self.assertEqual(other.get(self.path).status_code, 404)


if __name__ == "__main__":
    unittest.main()
