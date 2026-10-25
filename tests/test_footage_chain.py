"""Signed footage (#116): per-camera hash chains and verification."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import backup, footage_chain
from app.exports import ExportManager, create_export_record, verify_evidence_bytes
from app.preservation import delete_segment
from app.retention import cleanup_expired_recordings
from app.store import connect, initialize
from tests.scene_case import SceneTestCase

START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class FootageChainTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        footage_chain._unreadable.clear()  # Segment ids start again in each test's database.
        self.root = Path(self.temporary.name) / "recordings"
        (self.root / self.camera_id).mkdir()
        self.segments = [self.add_segment(index) for index in range(4)]

    def add_segment(self, index: int, starts: datetime | None = None) -> int:
        clip = self.root / self.camera_id / f"segment-{index}.mp4"
        subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s=160x120:r=10",
                        "-t", "1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-metadata", f"comment={index}", "-y", str(clip)], check=True, timeout=30)
        starts = starts or START + timedelta(seconds=index * 2)
        with connect() as db:
            return db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                              (self.camera_id, f"{self.camera_id}/segment-{index}.mp4", starts.isoformat(), (starts + timedelta(seconds=1)).isoformat(),
                               clip.stat().st_size)).lastrowid

    def chain(self) -> None:
        while asyncio.run(footage_chain.append_pending()):
            pass
        footage_chain.sign_heads()

    def verify(self) -> dict:
        response = self.client.get(f"/api/cameras/{self.camera_id}/footage/verify",
                                   params={"start": (START - timedelta(minutes=1)).isoformat(), "end": (START + timedelta(minutes=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def status_of(self, result: dict) -> dict[int, str]:
        return {entry["segment_id"]: entry["status"] for entry in result["segments"]}

    def test_untouched_footage_verifies_against_the_signed_chain(self):
        self.chain()
        result = self.verify()
        self.assertTrue(result["valid"], result)
        self.assertEqual(set(self.status_of(result).values()), {"ok"})
        self.assertEqual(result["signed_upto"], 4)
        # New footage verifies before its next signature, marked as such.
        newer = self.add_segment(9, START + timedelta(seconds=30))
        asyncio.run(footage_chain.append_pending())
        self.assertEqual(self.status_of(self.verify())[newer], "ok_unsigned_yet")

    def test_altered_missing_and_reordered_segments_are_named(self):
        self.chain()
        altered, missing, first, second = self.segments
        path = self.root / self.camera_id / "segment-0.mp4"
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        (self.root / self.camera_id / "segment-1.mp4").unlink()
        with connect() as db:  # Swap two segments' places in time.
            a, b = (db.execute("SELECT starts_at,ends_at FROM recording_segments WHERE id=?", (item,)).fetchone() for item in (first, second))
            db.execute("UPDATE recording_segments SET starts_at=?,ends_at=? WHERE id=?", (b["starts_at"], b["ends_at"], first))
            db.execute("UPDATE recording_segments SET starts_at=?,ends_at=? WHERE id=?", (a["starts_at"], a["ends_at"], second))
        result = self.verify()
        self.assertFalse(result["valid"])
        self.assertEqual(self.status_of(result), {altered: "altered", missing: "missing", first: "out_of_order", second: "out_of_order"})
        self.assertEqual({problem["segment_id"] for problem in result["problems"]}, {altered, missing, first, second})

    def test_a_segment_deleted_outside_the_nvr_is_missing_but_nvr_deletions_are_expected(self):
        self.chain()
        outside, admin = self.segments[:2]
        # Deleted behind the NVR's back: no removal is recorded.
        with connect() as db:
            db.execute("DROP TRIGGER footage_chain_removal")
            db.execute("DELETE FROM recording_segments WHERE id=?", (outside,))
        (self.root / self.camera_id / "segment-0.mp4").unlink()
        self.assertEqual(self.status_of(self.verify())[outside], "missing")
        initialize()  # (The trigger comes back only through a migration; re-create it for the rest of the test.)
        with connect() as db:
            db.execute("CREATE TRIGGER IF NOT EXISTS footage_chain_removal AFTER DELETE ON recording_segments BEGIN "
                       "INSERT OR IGNORE INTO footage_removals(segment_id, camera_id) VALUES (OLD.id, OLD.camera_id); END")
            db.execute("DELETE FROM footage_chain WHERE segment_id=?", (outside,))  # Forget the planted gap.
            db.execute("DELETE FROM footage_chain_signatures")
        self.rebuild_chain()

        # Administrator deletion and retention are recorded in the chain, so they verify.
        delete_segment(self.camera_id, admin, 1)
        fresh = self.add_segment(5, datetime.now(timezone.utc) - timedelta(minutes=5))
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,1)", (self.camera_id,))
        cleanup_expired_recordings()
        self.chain()
        with connect() as db:
            remaining = [row[0] for row in db.execute("SELECT id FROM recording_segments WHERE camera_id=?", (self.camera_id,))]
        self.assertEqual(remaining, [fresh])  # Both older segments aged out.
        result = footage_chain.verify(self.camera_id, START - timedelta(days=10), datetime.now(timezone.utc) + timedelta(days=2))
        self.assertTrue(result["valid"], result)

    def rebuild_chain(self) -> None:
        with connect() as db:
            db.execute("DELETE FROM footage_chain")
            db.execute("DELETE FROM footage_chain_signatures")
        self.chain()

    def test_footage_from_before_signing_is_not_signed_rather_than_tampered(self):
        with connect() as db:
            db.execute("UPDATE footage_chain_start SET after_segment_id=?", (self.segments[1],))
        self.chain()
        result = self.verify()
        self.assertTrue(result["valid"], result)
        statuses = self.status_of(result)
        self.assertEqual([statuses[item] for item in self.segments], ["not_signed", "not_signed", "ok", "ok"])

    def test_a_rewritten_chain_fails_its_signature(self):
        self.chain()
        with connect() as db:
            db.execute("UPDATE footage_chain SET sha256=? WHERE seq=2", ("0" * 64,))
        result = self.verify()
        self.assertFalse(result["valid"])
        self.assertIn("changed after it was written", result["chain_error"])

    def test_evidence_packages_verify_offline_with_the_included_chain_and_key(self):
        self.chain()
        job_id = create_export_record(1, [self.camera_id], START, START + timedelta(seconds=8), output_modes=["individual"])
        asyncio.run(ExportManager()._execute(job_id))
        result = self.client.get(f"/api/exports/{job_id}").json()
        self.assertEqual(result["status"], "completed", result)
        package = self.client.get(result["download_url"]).content
        # Offline: only the package itself (no database, no server).
        checked = verify_evidence_bytes(package)
        self.assertTrue(checked["checksums_valid"], checked)
        self.assertEqual(checked["chain"]["cameras"][0]["status"], "valid", checked)
        self.assertEqual(checked["chain"]["cameras"][0]["public_key"], footage_chain.public_key())
        online = self.client.post(f"/api/exports/{job_id}/verify", content=package, headers={"Content-Type": "application/zip"}).json()
        self.assertTrue(online["chain"]["cameras"][0]["signed_by_this_installation"], online)

        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            chain = json.loads(archive.read("chain.json"))
        camera = chain["cameras"][0]
        self.assertEqual(len(camera["sources"]), 4)
        forged = json.loads(json.dumps(camera["chain"]))
        forged["links"][0]["sha256"] = "f" * 64
        self.assertFalse(footage_chain.verify_extract(forged, camera["sources"])["valid"])
        swapped_sources = [{**source, "sha256": "e" * 64} for source in camera["sources"]]
        self.assertIn("do not match", footage_chain.verify_extract(camera["chain"], swapped_sources)["error"])

    def test_the_signing_key_survives_backup_and_restore(self):
        self.chain()
        before = footage_chain.public_key()
        backups = Path(self.temporary.name) / "backups"
        for key, value in (("NVR_BACKUP_PATH", str(backups)), ("NVR_BACKUP_PASSPHRASE", "a long backup passphrase 1"),
                           ("NVR_CONTROL_DIR", str(Path(self.temporary.name) / "run"))):
            self.addCleanup(lambda key=key, value=os.environ.get(key): os.environ.__setitem__(key, value) if value is not None else os.environ.pop(key, None))
            os.environ[key] = value
        archive = backups / backup.create_backup("manual")["file_name"]
        database = Path(os.environ["NVR_DB_PATH"])
        for leftover in (database, Path(f"{database}-wal"), Path(f"{database}-shm"), Path(os.environ["NVR_CREDENTIAL_KEY_PATH"])):
            leftover.unlink(missing_ok=True)
        initialize()
        self.assertNotEqual(footage_chain.public_key(), before)
        backup.restore(archive, force=True)
        self.assertEqual(footage_chain.public_key(), before)
        self.assertTrue(footage_chain.verify(self.camera_id, START - timedelta(minutes=1), START + timedelta(minutes=1))["valid"])

    def test_hashing_happens_off_the_recorders_write_path(self):
        # Indexing a segment does no hashing: the chain catches up in the background worker.
        started = time.perf_counter()
        newer = self.add_segment(7, START + timedelta(seconds=40))
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM footage_chain WHERE segment_id=?", (newer,)).fetchone())
        self.assertEqual(self.status_of(self.verify())[newer], "pending")
        self.chain()
        self.assertEqual(self.status_of(self.verify())[newer], "ok")
        self.assertLess(time.perf_counter() - started, 30)

    def test_segments_whose_files_are_missing_do_not_hold_back_newer_footage(self):
        from unittest import mock

        for index in range(2):
            (self.root / self.camera_id / f"segment-{index}.mp4").unlink()
        with mock.patch.object(footage_chain, "BATCH", 2):
            self.chain()
        statuses = self.status_of(self.verify())
        # Long closed and gone before they could be hashed: missing, while newer footage is chained regardless.
        self.assertEqual([statuses[item] for item in self.segments], ["missing", "missing", "ok", "ok"])

    def export_package(self) -> tuple[str, bytes]:
        job_id = create_export_record(1, [self.camera_id], START, START + timedelta(seconds=8), output_modes=["individual"])
        asyncio.run(ExportManager()._execute(job_id))
        result = self.client.get(f"/api/exports/{job_id}").json()
        self.assertEqual(result["status"], "completed", result)
        return job_id, self.client.get(result["download_url"]).content

    def repack(self, package: bytes, chain: object | None) -> bytes:
        """The package with chain.json replaced (or removed) and integrity.json recomputed to match, as a forger would."""
        import hashlib

        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(package)) as source, zipfile.ZipFile(output, "w") as target:
            integrity = json.loads(source.read("integrity.json"))
            members = {name: source.read(name) for name in source.namelist() if name not in {"integrity.json", "chain.json"}}
            if chain is not None:
                members["chain.json"] = json.dumps(chain).encode()
            integrity["files"] = [{"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in members.items()]
            for name, data in members.items():
                target.writestr(name, data)
            target.writestr("integrity.json", json.dumps(integrity))
        return output.getvalue()

    def verify_online(self, job_id: str, package: bytes) -> dict:
        response = self.client.post(f"/api/exports/{job_id}/verify", content=package, headers={"Content-Type": "application/zip"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_footage_exported_moments_after_recording_is_signed_in_the_package(self):
        # Nothing chained yet: the export catches the chain up and signs it first.
        job_id, package = self.export_package()
        checked = verify_evidence_bytes(package)
        self.assertEqual(checked["chain"]["cameras"][0]["status"], "valid", checked)
        self.assertEqual(checked["chain"]["cameras"][0]["unsigned_segments"], [])
        self.assertTrue(self.verify_online(job_id, package)["chain"]["valid"])

    def test_a_package_re_signed_stripped_or_malformed_fails_online_verification(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        import base64

        self.chain()
        job_id, package = self.export_package()
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            chain = json.loads(archive.read("chain.json"))
        camera = chain["cameras"][0]
        # A forger's own key: rewrite a source's hash, recompute the links and sign with a key of their making.
        forger = Ed25519PrivateKey.generate()
        links = camera["chain"]["links"]
        previous = links[0]["prev_hash"]
        camera["sources"][0]["sha256"] = links[0]["sha256"] = "d" * 64
        for link in links:
            link["prev_hash"] = previous
            link["link_hash"] = previous = footage_chain.link_hash(link)
        signature = camera["chain"]["signature"]
        signature.update(head_hash=previous, public_key=base64.b64encode(forger.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
                         signature=base64.b64encode(forger.sign(footage_chain.signed_message(self.camera_id, signature["upto_seq"], previous))).decode())
        forged = self.repack(package, chain)
        self.assertEqual(verify_evidence_bytes(forged)["chain"]["cameras"][0]["status"], "valid")  # Offline, the key is all it has.
        online = self.verify_online(job_id, forged)
        self.assertFalse(online["chain"]["valid"])
        self.assertFalse(online["chain"]["cameras"][0]["signed_by_this_installation"])

        self.assertFalse(self.verify_online(job_id, self.repack(package, None))["chain"]["valid"])
        downgraded = {**chain, "cameras": [{**camera, "chain": None}]}
        self.assertFalse(self.verify_online(job_id, self.repack(package, downgraded))["chain"]["valid"])
        self.assertFalse(self.verify_online(job_id, self.repack(package, []))["chain"]["valid"])
        with connect() as db:
            outcomes = [json.loads(row[0])["outcome"] for row in db.execute("SELECT details_json FROM audit_log WHERE action LIKE 'evidence_%' ORDER BY id")]
        self.assertEqual(outcomes, ["invalid"] * 4)

    def test_ranges_in_other_timezones_select_the_same_footage(self):
        self.chain()
        offset = timezone(timedelta(hours=2))
        response = self.client.get(f"/api/cameras/{self.camera_id}/footage/verify",
                                   params={"start": (START + timedelta(seconds=1.5)).astimezone(offset).isoformat(),
                                           "end": (START + timedelta(minutes=1)).astimezone(offset).isoformat()})
        self.assertEqual([entry["segment_id"] for entry in response.json()["segments"]], self.segments[1:])

    def test_the_worker_will_not_sign_over_a_rewritten_chain(self):
        self.chain()
        with connect() as db:
            link = db.execute("SELECT * FROM footage_chain WHERE seq=4").fetchone()  # The signed head the next links build on.
            rewritten = {**dict(link), "sha256": "0" * 64}
            db.execute("UPDATE footage_chain SET sha256=?, link_hash=? WHERE id=?", (rewritten["sha256"], footage_chain.link_hash(rewritten), link["id"]))
        self.add_segment(8, START + timedelta(seconds=20))
        asyncio.run(footage_chain.append_pending())
        with self.assertLogs("app.footage_chain", "ERROR"):
            self.assertEqual(footage_chain.sign_heads(), 0)
        self.assertFalse(self.verify()["valid"])

    def test_overdue_signatures_and_footage_lost_before_hashing_are_problems(self):
        asyncio.run(footage_chain.append_pending())  # Hashed, never signed.
        lost = self.add_segment(6, START + timedelta(seconds=20))
        (self.root / self.camera_id / "segment-6.mp4").unlink()
        with connect() as db:
            db.execute("UPDATE footage_chain SET created_at=?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        result = self.verify()
        self.assertFalse(result["valid"])
        statuses = self.status_of(result)
        self.assertEqual([statuses[item] for item in self.segments], ["signature_missing"] * 4)
        self.assertEqual(statuses[lost], "missing")
