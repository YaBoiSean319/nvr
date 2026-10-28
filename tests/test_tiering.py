"""Storage tiering (#144): the object store client, moving footage to a local cold path and to an S3-compatible bucket, playback from both, holds and exports, interrupted moves,
the footage chain across tiers, retention and deletion."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app import footage_chain, retention, s3, tiering
from app.recording import recordings_path
from app.store import connect
from tests.fixtures.s3_stub import S3Stub
from tests.test_access_grants import GrantCase

NOW = datetime.now(timezone.utc)


class SignatureTests(unittest.TestCase):
    def test_signing_reproduces_the_examples_aws_publishes(self):
        # The "get-vanilla" case of AWS's Signature Version 4 test suite
        vanilla = s3.authorization("GET", "/", [], {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}, s3.EMPTY_SHA256, access_key="AKIDEXAMPLE",
                                   secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", region="us-east-1", service="service", amz_date="20150830T123600Z")
        self.assertTrue(vanilla.endswith("Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"))
        # "GET Object" with a Range header, from Amazon S3's documentation
        headers = {"Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9", "x-amz-content-sha256": s3.EMPTY_SHA256, "x-amz-date": "20130524T000000Z"}
        documented = s3.authorization("GET", "/test.txt", [], headers, s3.EMPTY_SHA256, access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                      region="us-east-1", service="s3", amz_date="20130524T000000Z")
        self.assertTrue(documented.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date", documented)

    def test_a_path_is_encoded_once_and_a_query_is_sorted(self):
        self.assertEqual(s3.canonical_uri("/bucket/cam era/a+b.mp4"), "/bucket/cam%20era/a%2Bb.mp4")
        self.assertEqual(s3.canonical_query([("b", "2"), ("a", "x y")]), "a=x%20y&b=2")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.stub = S3Stub().start()
        self.addCleanup(self.stub.stop)
        self.client = s3.S3Client(self.stub.url, "footage", self.stub.access_key, self.stub.secret_key, prefix="nvr/cold")
        self.addCleanup(self.client.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def file(self, data: bytes) -> Path:
        path = Path(self.directory.name) / f"{uuid.uuid4()}.bin"
        path.write_bytes(data)
        return path

    def test_an_object_is_put_found_read_and_deleted_under_a_prefix(self):
        data = os.urandom(3 * 1024 * 1024 + 17)
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_file("cam/one.mp4", self.file(data), digest, len(data))
        self.assertEqual(self.stub.objects["nvr/cold/cam/one.mp4"], data)
        self.assertEqual(self.client.head("cam/one.mp4"), len(data))
        self.assertIsNone(self.client.head("cam/missing.mp4"))
        self.assertEqual(self.client.read_digest("cam/one.mp4"), (digest, len(data)))
        target = Path(self.directory.name) / "back.bin"
        self.assertEqual(self.client.download("cam/one.mp4", target), (digest, len(data)))
        self.assertEqual(target.read_bytes(), data)
        self.client.delete("cam/one.mp4")
        self.client.delete("cam/one.mp4")  # Already gone is gone
        self.assertEqual(self.stub.objects, {})

    def test_the_store_refuses_a_wrong_secret_a_wrong_bucket_and_an_upload_that_is_not_the_file_signed(self):
        data = b"footage" * 100
        digest = hashlib.sha256(data).hexdigest()
        bad = s3.S3Client(self.stub.url, "footage", self.stub.access_key, "another-secret-0123456789")
        with self.assertRaises(s3.S3Error) as caught:
            bad.put_file("x", self.file(data), digest, len(data))
        self.assertEqual(caught.exception.status, 403)
        bad.close()
        other = s3.S3Client(self.stub.url, "elsewhere", self.stub.access_key, self.stub.secret_key)
        with self.assertRaises(s3.S3Error) as caught:
            other.put_file("x", self.file(data), digest, len(data))
        self.assertEqual(caught.exception.status, 404)
        other.close()
        with self.assertRaises(s3.S3Error) as caught:  # Signed as one file, sent as another
            self.client.put_file("x", self.file(b"something else entirely"), digest, len(b"something else entirely"))
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.stub.objects, {})

    def test_a_store_that_cannot_be_reached_or_answers_badly_is_an_error_not_a_hang(self):
        down = s3.S3Client("http://127.0.0.1:1", "footage", "a", "b", timeout=httpx.Timeout(2.0, connect=1.0))
        with self.assertRaises(s3.S3Error):
            down.head("x")
        down.close()
        self.stub.fail["HEAD"] = 500
        with self.assertRaises(s3.S3Error) as caught:
            self.client.head("x")
        self.assertEqual(caught.exception.status, 500)

    def test_the_check_writes_reads_and_removes_a_small_object(self):
        self.client.check()
        self.assertEqual(self.stub.objects, {})
        self.assertEqual([method for method, _, _ in self.stub.requests], ["PUT", "HEAD", "DELETE"])


class TierCase(GrantCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cold = Path(self.directory.name) / "cold"
        self.stub = S3Stub().start()
        self.addCleanup(self.stub.stop)
        patch = mock.patch("app.api.tiering.check_endpoint", lambda url, allow_private: url.rstrip("/"))
        patch.start()
        self.addCleanup(patch.stop)

    def segment(self, camera_id: str, age_days: float = 40, data: bytes | None = None, seconds: int = 60) -> tuple[int, bytes]:
        payload = data or os.urandom(2048) * 4
        start = datetime.now(timezone.utc) - timedelta(days=age_days)
        name = f"{uuid.uuid4()}.mp4"
        path = recordings_path() / camera_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        with connect() as db:
            segment_id = int(db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                        (camera_id, f"{camera_id}/{name}", start.isoformat(), (start + timedelta(seconds=seconds)).isoformat(), len(payload))).lastrowid)
        return segment_id, payload

    def local_tier(self, name: str = "Archive disk") -> str:
        response = self.admin.post("/api/admin/tiering/tiers", json={"name": name, "kind": "local", "local_path": str(self.cold)})
        self.assertEqual(response.status_code, 201, response.text)
        return next(tier["id"] for tier in response.json()["tiers"] if tier["name"] == name)

    def s3_tier(self, name: str = "Bucket") -> str:
        response = self.admin.post("/api/admin/tiering/tiers", json={"name": name, "kind": "s3", "s3_endpoint": self.stub.url, "s3_bucket": self.stub.bucket, "s3_prefix": "nvr",
                                                                     "access_key": self.stub.access_key, "secret_key": self.stub.secret_key, "allow_private": True})
        self.assertEqual(response.status_code, 201, response.text)
        return next(tier["id"] for tier in response.json()["tiers"] if tier["name"] == name)

    def policy(self, tier_id: str, after_days: int = 30, **extra) -> None:
        response = self.admin.put("/api/admin/tiering/policies/default", json={"tier_id": tier_id, "after_days": after_days, "rate_kbps": 1_000_000, **extra})
        self.assertEqual(response.status_code, 200, response.text)

    def row(self, segment_id: int):
        with connect() as db:
            return db.execute("SELECT * FROM recording_segments WHERE id=?", (segment_id,)).fetchone()

    def hot(self, segment_id: int) -> Path:
        return recordings_path() / self.row(segment_id)["relative_path"]

    def media(self, camera_id: str, segment_id: int, **headers):
        return self.admin.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media", headers=headers)

    def chain(self) -> None:
        asyncio.run(self.append())
        footage_chain.sign_heads()

    async def append(self) -> None:
        while await footage_chain.append_pending():
            pass


class LocalTierTests(TierCase):
    def test_old_footage_moves_to_a_local_cold_path_and_plays_from_there_with_ranges(self):
        old, payload = self.segment(self.d1, 40)
        young, young_payload = self.segment(self.d1, 5)
        self.policy(self.local_tier())
        self.assertEqual(tiering.run_once().get("moved"), 1)
        row = self.row(old)
        self.assertIsNotNone(row["tier_id"])
        self.assertEqual(row["tier_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertFalse((recordings_path() / row["relative_path"]).exists())  # The hot copy went after the verified copy
        self.assertEqual((self.cold / row["relative_path"]).read_bytes(), payload)
        self.assertIsNone(self.row(young)["tier_id"])  # Not old enough
        self.assertTrue(self.hot(young).is_file())
        whole = self.media(self.d1, old)
        self.assertEqual((whole.status_code, whole.content), (200, payload))
        part = self.media(self.d1, old, Range="bytes=10-19")
        self.assertEqual((part.status_code, part.content), (206, payload[10:20]))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tier_moves").fetchone()[0], 0)
        self.assertEqual(tiering.run_once().get("moved"), None)  # Nothing more to do

    def test_the_timeline_and_the_segment_list_still_show_footage_that_moved(self):
        old, _ = self.segment(self.d1, 40)
        self.policy(self.local_tier())
        tiering.run_once()
        row = self.row(old)
        window = {"start": (datetime.fromisoformat(row["starts_at"]) - timedelta(minutes=5)).isoformat(), "end": (datetime.fromisoformat(row["ends_at"]) + timedelta(minutes=5)).isoformat()}
        timeline = self.admin.post("/api/playback/timeline", json={"camera_ids": [self.d1], **window}).json()["cameras"][0]
        self.assertEqual([item["id"] for item in timeline["segments"]], [old])

    def test_footage_that_is_held_or_in_an_export_is_not_moved_until_it_is_released(self):
        held, _ = self.segment(self.d1, 40)
        exporting, _ = self.segment(self.d1, 41)
        free, _ = self.segment(self.d1, 42)
        self.policy(self.local_tier())
        response = self.admin.post(f"/api/cameras/{self.d1}/segments/{held}/hold", json={})
        self.assertIn(response.status_code, (200, 201), response.text)
        self.start_export(exporting, "running")
        outcomes = tiering.run_once()
        self.assertEqual(outcomes, {"moved": 1, "held": 1, "exporting": 1})
        self.assertEqual([bool(self.row(item)["tier_id"]) for item in (held, exporting, free)], [False, False, True])
        self.assertTrue(self.hot(held).is_file() and self.hot(exporting).is_file())
        # Released, they move.
        self.assertEqual(self.admin.delete(f"/api/cameras/{self.d1}/segments/{held}/hold").status_code, 204)
        with connect() as db:
            db.execute("UPDATE exports SET status='completed'")
        self.assertEqual(tiering.run_once(), {"moved": 2})
        self.assertTrue(all(self.row(item)["tier_id"] for item in (held, exporting, free)))

    def start_export(self, segment_id: int, status: str) -> None:
        with connect() as db:
            export = str(uuid.uuid4())
            db.execute("INSERT INTO exports(id,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES(?,'[]',?,?,?,?,?)",
                       (export, NOW.isoformat(), NOW.isoformat(), status, NOW.isoformat(), NOW.isoformat()))
            db.execute("INSERT INTO export_sources(export_id,segment_id) VALUES(?,?)", (export, segment_id))

    def test_a_move_gives_way_if_the_footage_is_held_while_it_is_being_copied(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.local_tier())
        real = tiering.LocalStore.put

        def put_then_hold(store, source, key, sha, size, rate=None):
            real(store, source, key, sha, size, rate)
            self.admin.post(f"/api/cameras/{self.d1}/segments/{segment_id}/hold", json={})

        with mock.patch.object(tiering.LocalStore, "put", put_then_hold):
            self.assertEqual(tiering.run_once(), {"held": 1})
        self.assertIsNone(self.row(segment_id)["tier_id"])
        self.assertEqual(self.hot(segment_id).read_bytes(), payload)
        self.assertFalse((self.cold / self.row(segment_id)["relative_path"]).exists())  # The copy was taken back

    def test_an_interrupted_move_resumes_without_a_duplicate_or_a_lost_segment(self):
        first, first_payload = self.segment(self.d1, 40)
        second, second_payload = self.segment(self.d1, 41)
        third, third_payload = self.segment(self.d1, 42)
        self.policy(self.local_tier())
        # 1. The copy dies half way: a partial file on the tier, the segment still whole and playable on the hot storage.
        real_put = tiering.LocalStore.put
        broke = []

        def die_midway(store, source, key, sha, size, rate=None):
            target = store.path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_name(f".{target.name}.partial").write_bytes(source.read_bytes()[:100])
            broke.append(key)
            raise OSError("The disk went away")

        with mock.patch.object(tiering.LocalStore, "put", die_midway):
            outcomes = tiering.run_once()
        self.assertEqual(outcomes, {"failed": 3})
        self.assertTrue(all(self.row(item)["tier_id"] is None for item in (first, second, third)))
        self.assertEqual(self.media(self.d1, first).content, first_payload)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tier_moves WHERE state='copying'").fetchone()[0], 3)
        # 2. It resumes (after the retry pause, and with the disk back): each moves once.
        with connect() as db:
            db.execute("UPDATE tier_moves SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(tiering.run_once(), {"moved": 3})
        # 3. The switch happened but the process died before the hot file was removed: the segment is readable, once, and the next pass cleans up.
        late, late_payload = self.segment(self.d1, 43)
        with mock.patch("app.retention.unlink_if_unleased", lambda path: False):
            self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertTrue(self.hot(late).exists())
        self.assertEqual(self.row(late)["tier_id"] is not None, True)
        self.assertEqual(self.media(self.d1, late).content, late_payload)
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM tier_moves WHERE segment_id=?", (late,)).fetchone()[0], "switched")
        tiering.run_once()
        self.assertFalse(self.hot(late).exists())
        self.assertEqual((self.cold / self.row(late)["relative_path"]).read_bytes(), late_payload)
        self.assertEqual(self.media(self.d1, late).content, late_payload)
        # Nothing is duplicated or lost: every segment has one row and one copy, and the tier holds no partial file.
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*),COUNT(tier_id) FROM recording_segments").fetchone()[:], (4, 4))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tier_moves").fetchone()[0], 0)
        self.assertEqual(sorted(path.name for path in self.cold.rglob("*") if path.is_file() and path.name.startswith(".")), [])
        self.assertEqual(len([path for path in self.cold.rglob("*.mp4")]), 4)
        del real_put, second_payload, third_payload

    def test_a_segment_being_played_keeps_its_hot_file_until_it_is_released(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.local_tier())
        hot = self.hot(segment_id)
        retention.acquire_playback(hot)
        try:
            self.assertEqual(tiering.run_once(), {"moved": 1})
            self.assertTrue(hot.exists())  # Someone is playing it
            self.assertEqual(self.media(self.d1, segment_id).content, payload)
        finally:
            retention.release_playback(hot)
        tiering.run_once()
        self.assertFalse(hot.exists())
        self.assertEqual(self.media(self.d1, segment_id).content, payload)

    def test_a_copy_that_does_not_match_is_never_trusted_and_the_hot_copy_stays(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.local_tier())
        real = tiering.LocalStore.read_digest
        with mock.patch.object(tiering.LocalStore, "read_digest", lambda store, key, rate=None: ("0" * 64, 1)):
            self.assertEqual(tiering.run_once(), {"failed": 1})
        self.assertIsNone(self.row(segment_id)["tier_id"])
        self.assertEqual(self.hot(segment_id).read_bytes(), payload)
        self.assertFalse((self.cold / self.row(segment_id)["relative_path"]).exists())
        del real

    def test_footage_that_no_longer_matches_its_signed_chain_is_not_carried_over(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.chain()
        self.hot(segment_id).write_bytes(payload[:-1] + bytes([payload[-1] ^ 0xFF]))  # Altered after it was signed
        self.policy(self.local_tier())
        self.assertEqual(tiering.run_once(), {"altered": 1})
        self.assertIsNone(self.row(segment_id)["tier_id"])
        self.assertEqual(tiering.run_once(), {})  # And not tried again

    def test_hash_chain_verification_passes_across_the_tiers(self):
        near, _ = self.segment(self.d1, 5)
        far, _ = self.segment(self.d1, 40)
        local_one, _ = self.segment(self.d1, 41)
        self.chain()
        self.policy(self.local_tier())
        self.assertEqual(tiering.run_once(), {"moved": 2})
        self.assertEqual([bool(self.row(item)["tier_id"]) for item in (near, far, local_one)], [False, True, True])
        for age, wanted in ((5, near), (40, far), (41, local_one)):
            centre = NOW - timedelta(days=age)
            report = footage_chain.verify(self.d1, centre - timedelta(hours=1), centre + timedelta(hours=1))
            self.assertEqual([(entry["segment_id"], entry["status"]) for entry in report["segments"]], [(wanted, "ok")], report)
            self.assertTrue(report["valid"], report)
        api = self.admin.get(f"/api/cameras/{self.d1}/footage/verify", params={"start": (NOW - timedelta(days=41, hours=1)).isoformat(), "end": (NOW - timedelta(days=40) + timedelta(hours=1)).isoformat()})
        self.assertEqual(api.status_code, 200, api.text)
        self.assertTrue(api.json()["valid"], api.text)
        # A cold copy that is altered is found
        cold = self.cold / self.row(far)["relative_path"]
        data = cold.read_bytes()
        cold.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
        centre = NOW - timedelta(days=40)
        again = footage_chain.verify(self.d1, centre - timedelta(hours=1), centre + timedelta(hours=1))
        self.assertFalse(again["valid"])
        self.assertEqual([entry["status"] for entry in again["segments"]], ["altered"])

    def test_retention_removes_the_cold_copy_with_the_footage(self):
        segment_id, _ = self.segment(self.d1, 100)
        keep, _ = self.segment(self.d1, 50)
        self.policy(self.local_tier(), after_days=30)
        tiering.run_once()
        cold = self.cold / self.row(segment_id)["relative_path"]
        self.assertTrue(cold.is_file())
        result = retention.cleanup_expired_recordings()  # The default retention is 30 days: both are older, both go
        self.assertEqual(result["removed_segments"], 2)
        self.assertFalse(cold.exists())
        self.assertIsNone(self.row(segment_id))
        self.assertIsNone(self.row(keep))
        self.assertEqual(list(self.cold.rglob("*.mp4")), [])

    def test_an_administrators_delete_of_a_footage_segment_and_of_a_cameras_footage_removes_cold_copies(self):
        one, _ = self.segment(self.d1, 40)
        two, _ = self.segment(self.d2, 40)
        self.policy(self.local_tier())
        tiering.run_once()
        self.assertEqual(self.admin.delete(f"/api/cameras/{self.d1}/segments/{one}").status_code, 204)
        self.assertEqual(len(list(self.cold.rglob("*.mp4"))), 1)
        with connect() as db:
            db.execute("UPDATE cameras SET archived_at=? WHERE id=?", (NOW.isoformat(), self.d2))
            name = db.execute("SELECT name FROM cameras WHERE id=?", (self.d2,)).fetchone()[0]
        response = self.admin.request("DELETE", f"/api/cameras/{self.d2}/footage", json={"confirm_camera_name": name})
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(list(self.cold.rglob("*.mp4")), [])
        self.assertIsNone(self.row(two))


class ObjectStoreTierTests(TierCase):
    def test_old_footage_moves_to_a_bucket_and_plays_back_through_the_server(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.s3_tier())
        self.assertEqual(tiering.run_once(), {"moved": 1})
        row = self.row(segment_id)
        self.assertEqual(self.stub.objects[f"nvr/{row['relative_path']}"], payload)
        self.assertFalse(self.hot(segment_id).exists())
        whole = self.media(self.d1, segment_id)
        self.assertEqual((whole.status_code, whole.content), (200, payload))
        part = self.media(self.d1, segment_id, Range="bytes=5-14")
        self.assertEqual((part.status_code, part.content), (206, payload[5:15]))
        gets = [entry for entry in self.stub.requests if entry[0] == "GET" and entry[2] == 200]
        self.assertEqual(len(gets), 2)  # One to verify the copy, one to fetch it into the cache: the second request is served from the cache
        self.assertEqual(len(list(tiering.cache_dir().glob("*.mp4"))), 1)

    def test_what_comes_back_is_checked_and_a_changed_object_is_refused(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.s3_tier())
        tiering.run_once()
        self.stub.corrupt_gets = True
        response = self.media(self.d1, segment_id)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(list(tiering.cache_dir().glob("*.mp4")), [])
        self.stub.corrupt_gets = False
        self.assertEqual(self.media(self.d1, segment_id).content, payload)

    def test_a_store_that_is_down_is_a_failed_move_with_the_hot_copy_kept_and_an_incident_after_three(self):
        segment_id, payload = self.segment(self.d1, 40)
        tier = self.s3_tier()
        self.policy(tier)
        self.stub.stop()
        for _ in range(3):
            with connect() as db:
                db.execute("UPDATE tier_moves SET updated_at='2000-01-01T00:00:00+00:00'")
            self.assertEqual(tiering.run_once(), {"failed": 1})
        self.assertEqual(self.hot(segment_id).read_bytes(), payload)
        with connect() as db:
            incident = db.execute("SELECT state,fault_type FROM incidents WHERE incident_key=?", (f"tiering:{tier}",)).fetchone()
            self.assertEqual((incident["state"], incident["fault_type"]), ("active", "tiering"))
            self.assertEqual(db.execute("SELECT failures FROM cold_tiers").fetchone()[0], 3)
        self.stub = S3Stub(access_key=self.stub.access_key, secret_key=self.stub.secret_key).start()
        self.addCleanup(self.stub.stop)
        with connect() as db:
            db.execute("UPDATE cold_tiers SET s3_endpoint=?", (self.stub.url,))
            db.execute("UPDATE tier_moves SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(tiering.run_once(), {"moved": 1})
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM incidents WHERE incident_key=?", (f"tiering:{tier}",)).fetchone()[0], "recovered")

    def test_an_interrupted_upload_resumes_and_leaves_one_object(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.s3_tier())
        self.stub.fail["PUT"] = 503
        self.assertEqual(tiering.run_once(), {"failed": 1})
        self.assertEqual(self.stub.objects, {})
        self.assertEqual(self.hot(segment_id).read_bytes(), payload)
        with connect() as db:
            db.execute("UPDATE tier_moves SET updated_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertEqual(len(self.stub.objects), 1)

    def test_the_cache_stays_within_its_size_and_never_drops_footage_being_played(self):
        ids = [self.segment(self.d1, 40 + index, data=bytes([index]) * 3 * 1024 * 1024)[0] for index in range(3)]
        self.policy(self.s3_tier())
        self.assertEqual(tiering.run_once(), {"moved": 3})
        with mock.patch.dict(os.environ, {"NVR_TIER_CACHE_BYTES": str(64 * 1024 * 1024)}):
            for segment_id in ids:
                self.assertEqual(len(self.media(self.d1, segment_id).content), 3 * 1024 * 1024)
            self.assertEqual(len(list(tiering.cache_dir().glob("*.mp4"))), 3)  # Under the limit: all kept
        small = 4 * 1024 * 1024
        files = sorted(tiering.cache_dir().glob("*.mp4"), key=lambda path: path.stat().st_mtime)
        retention.acquire_playback(files[0])
        try:
            tiering.evict(limit=small)
            remaining = list(tiering.cache_dir().glob("*.mp4"))
            self.assertIn(files[0], remaining)  # In use: not dropped even though it is the oldest
            self.assertEqual(len(remaining), 1)  # The rest went; what is in use is all that is left, over the limit as it is
        finally:
            retention.release_playback(files[0])
        tiering.evict(limit=small)
        self.assertEqual(len(list(tiering.cache_dir().glob("*.mp4"))), 1)
        self.assertEqual(len(self.media(self.d1, ids[0]).content), 3 * 1024 * 1024)  # Back from the bucket when asked for again

    def test_hash_chain_verification_passes_for_footage_in_a_bucket(self):
        first, _ = self.segment(self.d1, 40)
        second, _ = self.segment(self.d1, 41)
        self.chain()
        self.policy(self.s3_tier())
        self.assertEqual(tiering.run_once(), {"moved": 2})
        for path in tiering.cache_dir().glob("*.mp4"):
            path.unlink()
        report = footage_chain.verify(self.d1, NOW - timedelta(days=42), NOW - timedelta(days=39))
        self.assertTrue(report["valid"], report)
        self.assertEqual([entry["status"] for entry in report["segments"]], ["ok", "ok"])

    def test_retention_removes_the_object_and_one_that_cannot_be_removed_is_not_forgotten(self):
        segment_id, _ = self.segment(self.d1, 100)
        self.policy(self.s3_tier())
        tiering.run_once()
        key = next(iter(self.stub.objects))
        self.stub.fail["DELETE"] = 500
        self.assertEqual(retention.cleanup_expired_recordings()["removed_segments"], 1)
        self.assertIsNone(self.row(segment_id))
        self.assertIn(key, self.stub.objects)  # The store would not delete it now
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tier_orphans").fetchone()[0], 1)
        tiering.run_once()
        self.assertEqual(self.stub.objects, {})
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tier_orphans").fetchone()[0], 0)


@unittest.skipUnless(os.environ.get("NVR_TEST_S3_ENDPOINT"), "needs a real S3-compatible store (the CI workflow starts MinIO and sets NVR_TEST_S3_ENDPOINT)")
class RealObjectStoreTests(TierCase):
    """The same journeys against a real store (MinIO in CI), proving the client's signing and addressing against software that is not ours."""

    def setUp(self):
        super().setUp()
        self.stub.stop()
        self.stub = mock.Mock(url=os.environ["NVR_TEST_S3_ENDPOINT"], bucket=os.environ.get("NVR_TEST_S3_BUCKET", "nvr-test"), access_key=os.environ["NVR_TEST_S3_ACCESS_KEY"],
                              secret_key=os.environ["NVR_TEST_S3_SECRET_KEY"])
        self.client = s3.S3Client(self.stub.url, self.stub.bucket, self.stub.access_key, self.stub.secret_key, prefix=f"nvr-test-{uuid.uuid4()}")
        self.addCleanup(self.client.close)

    def test_the_client_round_trips_an_object_and_its_check_passes(self):
        data = os.urandom(1_500_000)
        digest = hashlib.sha256(data).hexdigest()
        path = Path(self.directory.name) / "object.bin"
        path.write_bytes(data)
        self.client.check()
        self.client.put_file("a/b c.mp4", path, digest, len(data))
        self.assertEqual(self.client.head("a/b c.mp4"), len(data))
        self.assertEqual(self.client.read_digest("a/b c.mp4"), (digest, len(data)))
        self.client.delete("a/b c.mp4")
        self.assertIsNone(self.client.head("a/b c.mp4"))

    def test_footage_moves_to_the_bucket_plays_back_and_is_removed_with_retention(self):
        segment_id, payload = self.segment(self.d1, 100)
        tier = self.s3_tier()
        with connect() as db:
            db.execute("UPDATE cold_tiers SET s3_prefix=?", (self.client.prefix,))
        self.policy(tier)
        self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertFalse(self.hot(segment_id).exists())
        self.assertEqual(self.media(self.d1, segment_id).content, payload)
        self.assertEqual(self.media(self.d1, segment_id, Range="bytes=3-9").content, payload[3:10])
        key = self.row(segment_id)["relative_path"]
        self.assertEqual(self.client.head(key), len(payload))
        self.assertEqual(retention.cleanup_expired_recordings()["removed_segments"], 1)
        self.assertIsNone(self.client.head(key))


class AdministrationTests(TierCase):
    def test_a_tier_is_kept_only_if_it_works_and_its_credentials_are_never_shown(self):
        bad_path = self.admin.post("/api/admin/tiering/tiers", json={"name": "Nowhere", "kind": "local", "local_path": "/proc/nvr-cold"})
        self.assertEqual(bad_path.status_code, 422, bad_path.text)
        inside = self.admin.post("/api/admin/tiering/tiers", json={"name": "Inside", "kind": "local", "local_path": str(recordings_path() / "cold")})
        self.assertEqual(inside.status_code, 422)
        relative = self.admin.post("/api/admin/tiering/tiers", json={"name": "Relative", "kind": "local", "local_path": "cold"})
        self.assertEqual(relative.status_code, 422)
        wrong = self.admin.post("/api/admin/tiering/tiers", json={"name": "Bucket", "kind": "s3", "s3_endpoint": self.stub.url, "s3_bucket": self.stub.bucket, "access_key": self.stub.access_key,
                                                                  "secret_key": "the-wrong-secret-0123456789", "allow_private": True})
        self.assertEqual(wrong.status_code, 422)
        self.assertIn("403", wrong.text)
        self.assertEqual(self.admin.get("/api/admin/tiering").json()["tiers"], [])  # Neither was kept
        tier = self.s3_tier()
        shown = self.admin.get("/api/admin/tiering").text
        self.assertNotIn(self.stub.secret_key, shown)
        self.assertNotIn(self.stub.access_key, shown)
        with connect() as db:
            ciphertext = bytes(db.execute("SELECT credentials_ciphertext FROM cold_tiers WHERE id=?", (tier,)).fetchone()[0])
        self.assertNotIn(self.stub.secret_key.encode(), ciphertext)
        self.assertEqual(self.s3_tier_count(), 1)
        for row in self.audit_rows():
            self.assertNotIn(self.stub.secret_key, row)

    def s3_tier_count(self) -> int:
        return len(self.admin.get("/api/admin/tiering").json()["tiers"])

    def audit_rows(self) -> list[str]:
        with connect() as db:
            return [row["details_json"] for row in db.execute("SELECT details_json FROM audit_log")]

    def test_policies_are_checked_and_a_tier_in_use_cannot_be_removed(self):
        tier = self.local_tier()
        self.assertEqual(self.admin.put("/api/admin/tiering/policies/nowhere", json={"tier_id": tier, "after_days": 30}).status_code, 404)
        self.assertEqual(self.admin.put("/api/admin/tiering/policies/default", json={"tier_id": "nope", "after_days": 30}).status_code, 422)
        self.assertEqual(self.admin.put("/api/admin/tiering/policies/default", json={"tier_id": tier, "after_days": 0}).status_code, 422)
        self.policy(tier, after_days=30)
        self.assertEqual(self.admin.delete(f"/api/admin/tiering/tiers/{tier}").status_code, 409)  # A policy uses it
        segment_id, _ = self.segment(self.d1, 40)
        tiering.run_once()
        self.assertEqual(self.admin.delete("/api/admin/tiering/policies/default").status_code, 204)
        self.assertEqual(self.admin.delete(f"/api/admin/tiering/tiers/{tier}").status_code, 409)  # Footage lives there
        self.assertEqual(self.admin.put(f"/api/admin/tiering/tiers/{tier}", json={"name": "Archive disk", "kind": "local", "local_path": str(self.cold.parent / "elsewhere")}).status_code, 409)
        self.assertEqual(self.media(self.d1, segment_id).status_code, 200)
        view = self.admin.get("/api/admin/tiering").json()
        self.assertEqual((view["tiers"][0]["segments"], view["tiers"][0]["bytes"] > 0, view["volumes"]), (1, True, ["default"]))

    def test_no_policy_moves_nothing_and_a_disabled_one_moves_nothing(self):
        segment_id, _ = self.segment(self.d1, 40)
        self.assertEqual(tiering.run_once(), {})
        self.policy(self.local_tier(), enabled=False)
        self.assertEqual(tiering.run_once(), {})
        self.assertIsNone(self.row(segment_id)["tier_id"])

    def test_the_rate_limit_slows_a_move(self):
        waits = []
        wait = tiering.throttle(64)
        with mock.patch("app.tiering.time.sleep", lambda seconds: waits.append(seconds)):
            wait(1024 * 1024)  # A megabyte at 64 KiB a second is sixteen seconds ahead: it waits (at most five at a time)
        self.assertEqual(waits, [5.0])

    def test_only_administrators_manage_tiers(self):
        for client in (self.limited, self.full):
            self.assertEqual(client.get("/api/admin/tiering").status_code, 403)
            self.assertEqual(client.post("/api/admin/tiering/tiers", json={"name": "x", "kind": "local", "local_path": str(self.cold)}).status_code, 403)
            self.assertEqual(client.put("/api/admin/tiering/policies/default", json={"tier_id": "x", "after_days": 3}).status_code, 403)
            self.assertEqual(client.post("/api/admin/tiering/run").status_code, 403)

    def test_run_now_moves_a_few_segments_at_once(self):
        ids = [self.segment(self.d1, 40 + index)[0] for index in range(7)]
        self.policy(self.local_tier())
        result = self.admin.post("/api/admin/tiering/run")
        self.assertEqual((result.status_code, result.json()["moved"]), (200, 5))
        self.assertEqual(sum(1 for item in ids if self.row(item)["tier_id"]), 5)


class MigrationTests(unittest.TestCase):
    def test_the_tier_columns_and_tables_exist(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(directory) / "s.db"), "NVR_RECORDINGS_PATH": directory,
                                                                                        "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "k")}):
            from app.store import initialize

            initialize()
            with connect() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(recording_segments)")}
                self.assertTrue({"tier_id", "tier_key", "tier_sha256", "tiered_at"} <= columns)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"cold_tiers", "tier_policies", "tier_moves", "tier_orphans"} <= tables)


if __name__ == "__main__":
    unittest.main()
