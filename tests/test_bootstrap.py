import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NVR_DB_PATH"] = str(Path(self.tmp.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.tmp.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.tmp.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        os.environ["NVR_SEGMENT_SECONDS"] = "10"
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)
        from app.main import app
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self):
        self.client_ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("NVR_DB_PATH", None)
        os.environ.pop("NVR_RECORDINGS_PATH", None)
        os.environ.pop("NVR_CREDENTIAL_KEY_PATH", None)
        os.environ.pop("NVR_COOKIE_SECURE", None)
        os.environ.pop("NVR_SEGMENT_SECONDS", None)
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)

    def test_bootstrap_closes_setup_and_authenticates_admin(self):
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": True})
        response = self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "administrator")
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": False})
        self.assertEqual(self.client.get("/api/system/status").status_code, 200)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"second-admin", "password":"AnotherStrongPass2026"}).status_code, 409)

    def test_api_denies_status_and_invalid_credentials(self):
        self.assertEqual(self.client.get("/api/system/status").status_code, 401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client.post("/api/auth/logout")
        bad = self.client.post("/api/auth/login", json={"username":"local-admin", "password":"wrong"})
        self.assertEqual(bad.status_code, 401)

    def test_restart_preserves_account_and_health(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client_ctx.__exit__(None, None, None)
        from app.main import app
        with TestClient(app) as restarted:
            self.assertEqual(restarted.get("/healthz").status_code, 200)
            self.assertEqual(restarted.get("/api/setup/status").json(), {"setup_required": False})
            self.assertEqual(restarted.get("/api/system/status").status_code, 401)
            self.assertEqual(restarted.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_password_policy_and_username_validation(self):
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"a", "password":"short"}).status_code, 422)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"longpasswordonly"}).status_code, 422)

    def test_operator_can_view_but_cannot_change_camera_configuration(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("operator1", hash_password("OperatorPassword2026"), "operator"))
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"operator1", "password":"OperatorPassword2026"}).status_code, 200)
        self.assertEqual(self.client.get("/api/cameras").status_code, 200)
        self.assertEqual(self.client.get("/api/system/status").status_code, 403)
        self.assertEqual(self.client.post("/api/cameras/test", json={"stream_url":"rtsp://camera/stream"}).status_code, 403)
        self.assertEqual(self.client.post("/api/cameras", json={"name":"Unauthorized", "stream_url":"rtsp://camera/stream"}).status_code, 403)
        self.assertEqual(self.client.patch("/api/cameras/missing", json={"recording_mode":"disabled"}).status_code, 403)

    def test_administrator_can_terminate_another_session(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("operator1", hash_password("OperatorPassword2026"), "operator"))
        operator = TestClient(self.client_ctx.app)
        with operator:
            operator.post("/api/auth/login", json={"username":"operator1", "password":"OperatorPassword2026"})
            operator_me = operator.get("/api/auth/me")
            self.assertEqual(operator_me.status_code, 200)
            sessions = self.client.get("/api/admin/sessions").json()["sessions"]
            target = next(s for s in sessions if s["username"] == "operator1")
            self.assertEqual(self.client.delete(f"/api/admin/sessions/{target['session_id']}").status_code, 204)
            self.assertEqual(operator.get("/api/auth/me").status_code, 401)

    def test_existing_database_migrates_roles_and_preserves_live_sessions(self):
        legacy_path=Path(self.tmp.name)/"legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
              CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL COLLATE NOCASE UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('administrator')),created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z');
              CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z');
              CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
              INSERT INTO users(username,password_hash,role) VALUES('legacy-admin','unused','administrator');
              INSERT INTO sessions(token_hash,user_id,expires_at) VALUES('legacy-token',1,'2999-01-01T00:00:00Z');
            """)
        old_path=os.environ["NVR_DB_PATH"]
        os.environ["NVR_DB_PATH"]=str(legacy_path)
        from app.store import initialize
        initialize()
        with sqlite3.connect(legacy_path) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('new-operator','unused','operator')")
            migrated=db.execute("SELECT session_id,token_hash,user_id FROM sessions").fetchone()
        self.assertTrue(migrated[0])
        self.assertEqual(migrated[1:], ("legacy-token",1))
        os.environ["NVR_DB_PATH"]=old_path

    def test_oidc_maps_roles_denies_unmapped_users_and_keeps_local_login_available(self):
        os.environ.update({
            "NVR_PUBLIC_URL":"https://nvr.example.test",
            "NVR_OIDC_TENANT_ID":"11111111-1111-4111-8111-111111111111",
            "NVR_OIDC_CLIENT_ID":"client-id",
            "NVR_OIDC_CLIENT_SECRET":"client-secret",
            "NVR_OIDC_SESSION_SECRET":"test-session-signing-secret-long-enough",
            "NVR_OIDC_OPERATOR_ROLES":"NVR-Operator",
        })
        from unittest.mock import AsyncMock, patch
        from app.main import _mapped_role
        self.assertIsNone(_mapped_role({"roles":["Unmapped"]}))
        self.assertEqual(_mapped_role({"roles":["NVR-Operator"]}), "operator")
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        tenant_id="11111111-1111-4111-8111-111111111111"
        claims={"sub":"object-123", "tid":tenant_id, "iss":f"https://login.microsoftonline.com/{tenant_id}/v2.0", "preferred_username":"operator@example.test", "roles":["NVR-Operator"]}
        fake=type("FakeOIDC", (), {"authorize_access_token":AsyncMock(return_value={"userinfo":claims})})()
        with patch("app.main._oidc_client", return_value=fake):
            callback=self.client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False)
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(self.client.get("https://testserver.local/api/auth/me").json()["role"], "operator")
        self.client.post("/api/auth/logout")
        claims["roles"]=["Unmapped"]
        with patch("app.main._oidc_client", return_value=fake):
            denied=self.client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_authlib_authorization_request_uses_state_nonce_and_pkce_s256(self):
        import asyncio
        from urllib.parse import parse_qs, urlsplit
        from authlib.integrations.starlette_client import OAuth
        oauth=OAuth()
        oauth.register("test", client_id="id", client_secret="secret", authorize_url="https://idp.test/authorize", access_token_url="https://idp.test/token", client_kwargs={"scope":"openid profile email", "code_challenge_method":"S256", "trust_env":False})
        data=asyncio.run(oauth.create_client("test").create_authorization_url("https://nvr.test/callback"))
        query=parse_qs(urlsplit(data["url"]).query)
        self.assertTrue(query.get("state"))
        self.assertTrue(query.get("nonce"))
        self.assertTrue(query.get("code_challenge"))
        self.assertEqual(query.get("code_challenge_method"), ["S256"])

    def test_oidc_provider_outage_does_not_disable_local_administrator(self):
        os.environ.update({
            "NVR_PUBLIC_URL":"https://nvr.example.test",
            "NVR_OIDC_TENANT_ID":"11111111-1111-4111-8111-111111111111",
            "NVR_OIDC_CLIENT_ID":"client-id",
            "NVR_OIDC_CLIENT_SECRET":"client-secret",
            "NVR_OIDC_SESSION_SECRET":"test-session-signing-secret-long-enough",
        })
        from unittest.mock import AsyncMock, patch
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        fake=type("FakeOIDC", (), {"authorize_redirect":AsyncMock(side_effect=OSError("provider offline"))})()
        with patch("app.main._oidc_client", return_value=fake):
            self.assertEqual(self.client.get("/api/auth/oidc/login", follow_redirects=False).status_code, 503)
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_camera_credentials_are_encrypted_and_not_returned(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        stream_url = "rtsp://viewer:CameraSecret2026@127.0.0.1:554/stream?token=NeverReturnThis"
        probe = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=probe)):
            added = self.client.post("/api/cameras", json={"name":"Test Camera", "stream_url":stream_url})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.json()["recording_mode"], "motion")
        self.assertEqual(added.json()["recording_status"], "unsupported")
        response_text = added.text + self.client.get("/api/cameras").text
        self.assertNotIn("CameraSecret2026", response_text)
        self.assertNotIn("NeverReturnThis", response_text)
        self.assertIn("rtsp://127.0.0.1:554", response_text)
        self.assertNotIn("/stream", response_text)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            ciphertext = db.execute("SELECT stream_url_ciphertext FROM cameras").fetchone()[0]
        self.assertNotIn(b"CameraSecret2026", ciphertext)
        self.assertEqual(Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "FFprobe is required for the stream test")
    def test_real_probe_failure_is_reported_without_echoing_credentials(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.post("/api/cameras/test", json={
            "stream_url":"rtsp://probe-user:ProbeSecret2026@127.0.0.1:1/unavailable"
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reachable"])
        self.assertNotIn("ProbeSecret2026", response.text)
        self.assertEqual(response.json()["reason"], "Camera stream could not be opened")

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for reconnect testing")
    def test_continuous_recorder_reconnects_after_stream_failure(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            created = self.client.post("/api/cameras", json={
                "name":"Unavailable test feed",
                "stream_url":"rtsp://feed-user:FeedSecret2026@127.0.0.1:1/stream",
                "recording_mode":"continuous",
            })
        self.assertEqual(created.status_code, 201)
        camera_id = created.json()["id"]
        deadline = time.monotonic() + 8
        current = created.json()
        while time.monotonic() < deadline:
            current = self.client.get("/api/cameras").json()["cameras"][0]
            if current["recording_status"] == "reconnecting":
                break
            time.sleep(0.2)
        self.assertEqual(current["recording_status"], "reconnecting")
        self.assertNotIn("FeedSecret2026", self.client.get("/api/cameras").text)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(os.environ.get("NVR_RTSP_TEST_URL"), "A simulated RTSP publisher is required")
    def test_rtsp_stream_is_probed_recorded_indexed_and_playable(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        ffmpeg_log = Path(self.tmp.name) / "ffmpeg-stderr.log"
        stderr_log = ffmpeg_log.open("wb")
        self.addCleanup(stderr_log.close)
        stderr_patch = patch("app.recording.asyncio.subprocess.DEVNULL", stderr_log)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)
        created = self.client.post("/api/cameras", json={
            "name":"Simulated RTSP camera",
            "stream_url":os.environ["NVR_RTSP_TEST_URL"],
            "recording_mode":"continuous",
        })
        self.assertEqual(created.status_code, 201, created.text)
        camera_id = created.json()["id"]
        self.assertTrue(created.json()["video"]["codec"])
        deadline = time.monotonic() + 35
        segments = []
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/cameras/{camera_id}/segments")
            self.assertEqual(response.status_code, 200)
            segments = response.json()["segments"]
            if segments:
                break
            time.sleep(1)
        log_text = ffmpeg_log.read_text(errors="replace")
        self.assertTrue(segments, f"FFmpeg did not index a completed segment from the RTSP stream. FFmpeg stderr:\n{log_text}")
        playback = self.client.get(segments[0]["media_url"])
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.headers["content-type"], "video/mp4")
        self.assertIn("inline", playback.headers["content-disposition"])
        self.assertGreater(len(playback.content), 128)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for the media response test")
    def test_authenticated_browser_media_route_serves_indexed_video(self):
        media_root = Path(os.environ["NVR_RECORDINGS_PATH"])
        camera_id = str(uuid.uuid4())
        directory = media_root / camera_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        index_path = directory / "segments.csv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=duration=2:size=64x64:rate=10", "-an", "-c:v", "libx264", "-f", "segment",
            "-segment_time", "60", "-reset_timestamps", "1", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+faststart", "-segment_list", str(index_path),
            "-segment_list_type", "csv", "-strftime", "1", str(directory / "segment-%Y%m%dT%H%M%SZ.mp4"),
        ], check=True, timeout=20)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.credentials import encrypt
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute(
                "INSERT INTO cameras(id, name, stream_url_ciphertext, recording_mode) VALUES (?, ?, ?, 'disabled')",
                (camera_id, "Playback camera", encrypt("rtsp://viewer:secret@camera/stream")),
            )
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("viewer1", hash_password("ViewerPassword2026"), "operator"))
        from app.recording import RecordingManager
        self.assertEqual(RecordingManager()._index_csv(camera_id, index_path, directory), 1)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            segment_id = db.execute("SELECT id FROM recording_segments").fetchone()[0]
            relative_path = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment_id,)).fetchone()[0]
        media = media_root / relative_path
        anonymous = self.client.post("/api/auth/logout")
        self.assertEqual(anonymous.status_code, 204)
        denied = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(denied.status_code, 401)
        self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertGreater(len(response.content), 100)
        partial = self.client.get(
            f"/api/cameras/{camera_id}/segments/{segment_id}/media", headers={"Range":"bytes=0-99"}
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(len(partial.content), 100)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username":"viewer1", "password":"ViewerPassword2026"})
        self.assertEqual(self.client.get(f"/api/cameras/{camera_id}/segments").status_code, 200)
        self.assertEqual(self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media").status_code, 200)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertGreater(float(probe.stdout.strip()), 0)


if __name__ == "__main__":
    unittest.main()
