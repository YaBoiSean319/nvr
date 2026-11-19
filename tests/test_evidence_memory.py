"""Verifying and packaging evidence do not hold whole packages in memory or stop the server (#359).

A verification collected the upload in memory, copied it and read every file in it whole: three times the upload at the peak. Packaging an
export hashed and copied up to 2 GiB on the exporter's event loop, where its sign of life could not be sent."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import threading
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from app import cases, exports
from app.recording import recordings_path
from tests.test_access_grants import GrantCase

MIB = 1024 * 1024


def evidence_package(path: Path, video_bytes: int, export_id: str = "job-1") -> None:
    """An evidence package as the NVR writes one, with a video of that size."""
    members = {"metadata.json": json.dumps({"export_id": export_id, "audit_reference": {"resource_type": "export", "resource_id": export_id}}).encode()}
    block, digest = os.urandom(MIB), hashlib.sha256()
    for _ in range(video_bytes // MIB):
        digest.update(block)
    integrity = {"schema_version": 1, "algorithm": "sha256", "files": [
        {"path": "metadata.json", "size_bytes": len(members["metadata.json"]), "sha256": hashlib.sha256(members["metadata.json"]).hexdigest()},
        {"path": "camera.mp4", "size_bytes": video_bytes, "sha256": digest.hexdigest()}]}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("metadata.json", members["metadata.json"])
        archive.writestr("integrity.json", json.dumps(integrity))
        with archive.open("camera.mp4", "w") as video:
            for _ in range(video_bytes // MIB):
                video.write(block)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.package = Path(self.temporary.name) / "package.zip"

    def peak(self, work) -> int:
        tracemalloc.start()
        try:
            result = work()
            return tracemalloc.get_traced_memory()[1], result
        finally:
            tracemalloc.stop()

    def test_checking_a_large_package_from_disk_takes_memory_for_a_piece_not_for_the_package(self):
        evidence_package(self.package, 96 * MIB)
        peak, result = self.peak(lambda: exports.verify_evidence_file(self.package, "job-1"))
        self.assertTrue(result["checksums_valid"], result)
        self.assertEqual(result["files"][0]["size_bytes"], 96 * MIB)
        self.assertLess(peak, 12 * MIB, f"{peak // MIB} MiB held to check a 96 MiB package")

    def test_a_case_package_with_a_large_clip_inside_is_checked_the_same_way(self):
        evidence_package(self.package, 64 * MIB, export_id="clip-export")
        outer = Path(self.temporary.name) / "case.zip"
        clip_size, clip_digest = self.package.stat().st_size, hashlib.sha256()
        with self.package.open("rb") as handle:
            while chunk := handle.read(MIB):
                clip_digest.update(chunk)
        metadata = json.dumps({"case_id": "case-1", "package_id": "package-1", "audit_reference": {"resource_type": "case", "resource_id": "case-1"}, "omitted_items": 0}).encode()
        document = json.dumps({"items": [{"kind": "clip", "id": "item-1", "export_id": "clip-export", "package_file": "clips/clip.zip"}]}).encode()
        small = {"metadata.json": metadata, "case.json": document, "notes.json": b"[]", "notes.md": b"", "audit.json": b"[]"}
        integrity = {"algorithm": "sha256", "files": [{"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in small.items()]
                     + [{"path": "clips/clip.zip", "size_bytes": clip_size, "sha256": clip_digest.hexdigest()}]}
        with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, body in small.items():
                archive.writestr(name, body)
            archive.writestr("integrity.json", json.dumps(integrity))
            archive.write(self.package, "clips/clip.zip")
        peak, result = self.peak(lambda: cases.verify_package_file(outer, "case-1", "package-1"))
        self.assertEqual((result["checksums_valid"], result["clips_valid"], result["complete"]), (True, True, True), result)
        self.assertLess(peak, 12 * MIB, f"{peak // MIB} MiB held to check a case package with a 64 MiB clip")

    def test_a_package_in_memory_gives_the_same_answer_as_the_same_package_on_disk(self):
        evidence_package(self.package, 2 * MIB)
        self.assertEqual(exports.verify_evidence_bytes(self.package.read_bytes(), "job-1"), exports.verify_evidence_file(self.package, "job-1"))
        tampered = bytearray(self.package.read_bytes())
        tampered[-MIB] ^= 0x01
        self.assertFalse(exports.verify_evidence_file(io.BytesIO(bytes(tampered)), "job-1")["checksums_valid"])


class UploadTests(GrantCase):
    def uploads_left(self) -> list[str]:
        folder = recordings_path() / exports.EXPORT_DIRECTORY
        return sorted(path.name for path in folder.glob(".verify-*")) if folder.is_dir() else []

    def completed_export(self) -> str:
        from app.store import connect
        with connect() as db:
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES('job-1',1,?,?,?,'completed',?,?)",
                       (json.dumps([self.d1]), "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        return "job-1"

    def test_the_uploaded_file_is_removed_after_a_check_that_passes_fails_or_is_refused(self):
        job_id = self.completed_export()
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "package.zip"
            evidence_package(package, MIB, export_id=job_id)
            good = self.admin.post(f"/api/exports/{job_id}/verify", content=package.read_bytes(), headers={"content-type": "application/zip"})
        self.assertEqual((good.status_code, good.json()["checksums_valid"]), (200, True))
        self.assertEqual(self.uploads_left(), [])
        bad = self.admin.post(f"/api/exports/{job_id}/verify", content=b"this is not a zip", headers={"content-type": "application/zip"})
        self.assertEqual((bad.status_code, bad.json()["checksums_valid"]), (200, False))
        self.assertEqual(self.uploads_left(), [])
        with mock.patch("app.api.export_jobs.MAX_VERIFY_PACKAGE_BYTES", 1000):
            refused = self.admin.post(f"/api/exports/{job_id}/verify", content=iter([b"x" * 600, b"y" * 600]), headers={"content-type": "application/zip"})
        self.assertEqual(refused.status_code, 413)
        self.assertEqual(self.uploads_left(), [])
        with mock.patch("app.api.export_jobs.verify_evidence_file", side_effect=RuntimeError("the check itself broke")):
            with self.assertRaises(RuntimeError):
                self.admin.post(f"/api/exports/{job_id}/verify", content=b"anything", headers={"content-type": "application/zip"})
        self.assertEqual(self.uploads_left(), [])

    def test_the_check_runs_off_the_web_servers_event_loop(self):
        job_id = self.completed_export()
        where: list[bool] = []
        real = exports.verify_evidence_file

        def watched(*args, **kwargs):
            where.append(asyncio._get_running_loop() is None)  # No event loop runs in the thread doing the check
            return real(*args, **kwargs)

        with mock.patch("app.api.export_jobs.verify_evidence_file", watched):
            self.admin.post(f"/api/exports/{job_id}/verify", content=b"not a zip", headers={"content-type": "application/zip"})
        self.assertEqual(where, [True])


class PackagingTests(unittest.TestCase):
    def test_work_given_to_a_thread_leaves_the_loop_turning(self):
        async def run() -> float:
            longest, last = 0.0, asyncio.get_running_loop().time()
            main = threading.get_ident()
            ran_on: list[int] = []

            def heavy() -> None:
                ran_on.append(threading.get_ident())
                digest = hashlib.sha256()
                for _ in range(200):
                    digest.update(b"x" * MIB)

            task = asyncio.create_task(asyncio.to_thread(heavy))
            while not task.done():
                await asyncio.sleep(0.005)
                now = asyncio.get_running_loop().time()
                longest, last = max(longest, now - last), now
            self.assertNotEqual(ran_on, [main])
            return longest

        self.assertLess(asyncio.run(run()), 0.25)
