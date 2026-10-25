"""Privacy masks (#115), checked on decoded frames."""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import live_hub, privacy
from app.credentials import encrypt
from app.exports import ExportManager, create_export_record
from app.security import hash_password
from app.store import connect
from tests.fixtures.mask_device import MaskDevice
from tests.scene_case import SceneTestCase
from tests.test_support import TEST_PASSWORD

WIDTH, HEIGHT = 320, 240
# The top-left quarter of the picture is private.
MASK = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5}
INSIDE = (WIDTH // 4, HEIGHT // 4)
OUTSIDE = (WIDTH * 3 // 4, HEIGHT * 3 // 4)


def white_clip(path: Path, seconds: float = 2) -> None:
    subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=white:s={WIDTH}x{HEIGHT}:r=10",
                    "-t", str(seconds), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", "-y", str(path)], check=True, timeout=30)


def brightness(media: bytes, *, image: bool = False) -> tuple[int, int]:
    """Luma at a point inside the mask and a point outside it, on the first decoded frame scaled to the clip size."""
    args = [shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error"]
    args += ["-f", "image2pipe"] if image else []
    args += ["-i", "pipe:0", "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT}", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    pixels = subprocess.run(args, input=media, capture_output=True, check=True, timeout=30).stdout
    return pixels[INSIDE[1] * WIDTH + INSIDE[0]], pixels[OUTSIDE[1] * WIDTH + OUTSIDE[0]]


class PrivacyTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temporary.name) / "recordings"
        (root / self.camera_id).mkdir()
        self.clip = root / self.camera_id / "white.mp4"
        white_clip(self.clip)
        self.starts = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
        with connect() as db:
            self.segment_id = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (self.camera_id, f"{self.camera_id}/white.mp4", self.starts.isoformat(), (self.starts + timedelta(seconds=2)).isoformat(), self.clip.stat().st_size),
            ).lastrowid
        self.media = f"/api/cameras/{self.camera_id}/segments/{self.segment_id}/media"
        self.snapshot = f"/api/cameras/{self.camera_id}/segments/{self.segment_id}/snapshot"
        self.at = (self.starts + timedelta(seconds=1)).isoformat()

    def mask(self, zones=(MASK,)) -> dict:
        saved = self.client.put(f"/api/cameras/{self.camera_id}/privacy", json={"zones": list(zones)})
        self.assertEqual(saved.status_code, 200, saved.text)
        return saved.json()

    def operator(self) -> TestClient:
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("watcher", hash_password(TEST_PASSWORD), "operator"))
        context = TestClient(self.client.app)
        client = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.assertEqual(client.post("/api/auth/login", json={"username": "watcher", "password": TEST_PASSWORD}).status_code, 200)
        return client

    def test_without_camera_masks_every_view_and_export_is_masked(self):
        status = self.mask()
        self.assertEqual((status["camera_side"], status["server_enforced"]), ("unsupported", True))
        self.assertIn("Recordings on disk are NOT masked", status["recordings"])

        # Playback: a masked rendering, never the file on disk.
        played = self.client.get(self.media)
        self.assertEqual(played.status_code, 200, played.text[:200])
        inside, outside = brightness(played.content)
        self.assertLess(inside, 30)
        self.assertGreater(outside, 220)
        self.assertNotEqual(played.content, self.clip.read_bytes())
        # Range requests (seeking) still work on the masked copy.
        self.assertEqual(self.client.get(self.media, headers={"Range": "bytes=0-99"}).status_code, 206)

        snapshot = self.client.get(self.snapshot, params={"at": self.at})
        self.assertEqual(snapshot.status_code, 200, snapshot.text[:200])
        inside, outside = brightness(snapshot.content, image=True)
        self.assertLess(inside, 30)
        self.assertGreater(outside, 220)

        # Export: the individual video and the grid both burn the mask in.
        job_id = create_export_record(1, [self.camera_id], self.starts, self.starts + timedelta(seconds=2))
        asyncio.run(ExportManager()._execute(job_id))
        result = self.client.get(f"/api/exports/{job_id}").json()
        self.assertEqual(result["status"], "completed", result)
        with zipfile.ZipFile(io.BytesIO(self.client.get(result["download_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertTrue(metadata["cameras"][0]["privacy_masked"])
            for name in (metadata["cameras"][0]["video_file"], "grid.mp4"):
                inside, outside = brightness(archive.read(name))
                self.assertLess(inside, 40, name)
                self.assertGreater(outside, 180, name)

    def test_live_video_is_transcoded_with_the_mask(self):
        # The live path reads a camera stream; here the shared stream reads the clip directly.
        async def first_frames() -> bytes:
            stream = live_hub.LiveStream(("test",), str(self.clip), False, video_filter=privacy.drawbox_filter([MASK]))
            viewer = live_hub.Viewer()
            stream.add(viewer)
            with patch.object(live_hub, "_PROTOCOLS", live_hub._PROTOCOLS + ",file"), \
                    patch.object(live_hub, "_manifest_bytes", lambda source: f"ffconcat version 1.0\nfile file:{source}\n".encode()):
                stream.start()
                await asyncio.wait_for(stream.ready.wait(), 20)
                fragment = await asyncio.wait_for(viewer.queue.get(), 20)
            await stream.stop()
            return stream.init + fragment

        inside, outside = brightness(asyncio.run(first_frames()))
        self.assertLess(inside, 30)
        self.assertGreater(outside, 220)

    def test_operators_never_obtain_unmasked_media(self):
        self.mask()
        operator = self.operator()
        for path, params in ((self.media, {}), (self.snapshot, {"at": self.at}), (f"/api/cameras/{self.camera_id}/live", {})):
            refused = operator.get(path, params={**params, "unmasked": "true"})
            self.assertEqual(refused.status_code, 403, path)
        self.assertEqual(operator.post(f"/api/cameras/{self.camera_id}/privacy/unmasked", json={"reason": "I would like to look please"}).status_code, 403)
        self.assertEqual(operator.put(f"/api/cameras/{self.camera_id}/privacy", json={"zones": []}).status_code, 403)
        # What an operator does get is masked.
        inside, _ = brightness(operator.get(self.media).content)
        self.assertLess(inside, 30)

    def test_unmasked_viewing_needs_a_reason_and_is_audited(self):
        self.mask()
        self.assertEqual(self.client.get(self.media, params={"unmasked": "true"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/cameras/{self.camera_id}/privacy/unmasked", json={"reason": "why"}).status_code, 422)
        reason = "Police request 2026-1123 for the doorway incident"
        granted = self.client.post(f"/api/cameras/{self.camera_id}/privacy/unmasked", json={"reason": reason})
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/privacy").json()["unmasked_until"], granted.json()["expires_at"])

        unmasked = self.client.get(self.media, params={"unmasked": "true"})
        self.assertEqual(unmasked.status_code, 200)
        self.assertEqual(unmasked.content, self.clip.read_bytes())
        inside, _ = brightness(self.client.get(self.snapshot, params={"at": self.at, "unmasked": "true"}).content, image=True)
        self.assertGreater(inside, 220)
        # Without asking, even a granted administrator sees the masked picture.
        inside, _ = brightness(self.client.get(self.media).content)
        self.assertLess(inside, 30)

        with connect() as db:
            audit = [(row["action"], json.loads(row["details_json"])) for row in db.execute(
                "SELECT action,details_json FROM audit_log WHERE resource_type='camera' AND resource_id=? AND action LIKE 'privacy_unmasked%' ORDER BY id", (self.camera_id,))]
        self.assertEqual(audit[0], ("privacy_unmasked_view_granted", {"reason": reason, "expires_at": granted.json()["expires_at"]}))
        self.assertEqual([action for action, _ in audit[1:]], ["privacy_unmasked_viewed", "privacy_unmasked_viewed"])

        # The grant lapses.
        with connect() as db:
            db.execute("UPDATE unmasked_grants SET expires_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.client.get(self.media, params={"unmasked": "true"}).status_code, 403)

        # API tokens never unmask, whatever their scopes.
        token = self.client.post("/api/admin/api-tokens", json={"name": "Automation", "scopes": ["admin"]}).json()["secret"]
        context = TestClient(self.client.app)
        machine = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(machine.post(f"/api/cameras/{self.camera_id}/privacy/unmasked", json={"reason": reason}, headers=headers).status_code, 403)

    def test_camera_side_masks_are_written_and_newer_recordings_carry_them(self):
        device = MaskDevice()
        self.addCleanup(device.close)
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (device.url, encrypt(json.dumps({"username": "onvif", "password": "pw"})), self.camera_id))
        status = self.mask()
        self.assertEqual(status["camera_side"], "applied")
        self.assertFalse(status["server_enforced"])
        self.assertIn("The camera applies these masks", status["recordings"])
        self.assertEqual(list(device.masks.values()), [[(-1.0, 1.0), (0.0, 1.0), (0.0, 0.0), (-1.0, 0.0)]])

        # Footage from before the camera took the mask is still masked by the NVR; newer footage is served as recorded.
        inside, _ = brightness(self.client.get(self.media).content)
        self.assertLess(inside, 30)
        since = datetime.fromisoformat(status["camera_masked_since"])
        shutil.copy(self.clip, self.clip.with_name("newer.mp4"))
        with connect() as db:
            newer = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                               (self.camera_id, f"{self.camera_id}/newer.mp4", (since + timedelta(seconds=1)).isoformat(),
                                (since + timedelta(seconds=3)).isoformat(), self.clip.stat().st_size)).lastrowid
        self.assertEqual(self.client.get(f"/api/cameras/{self.camera_id}/segments/{newer}/media").content, self.clip.with_name("newer.mp4").read_bytes())

        # Changing the masks replaces the camera's: the old ones are deleted by token.
        self.mask([{"x": 0.5, "y": 0.5, "width": 0.25, "height": 0.25}])
        self.assertEqual(device.deleted, ["mask-1"])
        self.assertEqual(len(device.masks), 1)

    def test_a_camera_that_refuses_masks_is_masked_by_the_nvr(self):
        device = MaskDevice(supports_masks=False)
        self.addCleanup(device.close)
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=? WHERE id=?", (device.url, self.camera_id))
        status = self.mask()
        self.assertEqual((status["camera_side"], status["server_enforced"]), ("unsupported", True))

    def test_alarm_thumbnails_and_keyframes_are_masked(self):
        from app.push import thumbnail

        self.mask()
        jpeg = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=white:s={WIDTH}x{HEIGHT}",
                               "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout
        track, alarm = str(uuid.uuid4()), str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,model_id,model_version,provider) VALUES(?,?,?,?,?,1,'m','1','CPU')",
                       (track, self.camera_id, "person", "2026-10-21T10:00:00+00:00", "2026-10-21T10:00:01+00:00"))
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, "2026-10-21T10:00:00+00:00", jpeg))
            db.execute("INSERT INTO alarms(id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (alarm, "Rule", self.camera_id, track, "person", "zone_entry", "2026-10-21T10:00:00+00:00", "{}",
                                                          "2026-10-21T09:59:50+00:00", "2026-10-21T10:00:10+00:00", "2026-10-21T10:00:00+00:00", "2026-10-21T10:00:00+00:00"))
        inside, outside = brightness(thumbnail(alarm), image=True)
        self.assertLess(inside, 30)
        self.assertGreater(outside, 220)
