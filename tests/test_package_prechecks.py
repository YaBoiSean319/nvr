"""The checks made before an uploaded package's files are read, for an export's evidence package and for a case package (#387).

Both verifiers refuse the same malformed archives the same way before they read any file: a path that is absolute or climbs out, a name
given twice, more uncompressed content than the limit, an integrity manifest missing, given twice or of an unknown kind. A manifest that
lists one path twice is refused by each in its own words. These tests pin that, before the checks the two write out become one.
"""
from __future__ import annotations

import io
import json
import unittest
import warnings
import zipfile
from unittest.mock import patch

from app import cases, exports

MANIFEST = {"algorithm": "sha256", "files": [{"path": "clip.mp4", "size_bytes": 4, "sha256": "0" * 64}]}


def package(members: list[tuple[str, bytes]]) -> bytes:
    content = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # A name given twice is the point of one of these
        with zipfile.ZipFile(content, "w") as archive:
            for name, data in members:
                archive.writestr(name, data)
    return content.getvalue()


def manifest(value) -> tuple[str, bytes]:
    return ("integrity.json", json.dumps(value).encode())


def both(data: bytes) -> dict[str, str]:
    """What each verifier says is wrong with this package."""
    return {"export": exports.verify_evidence_bytes(data)["error"], "case": cases.verify_package_bytes(data, "case-1", "package-1")["error"]}


class PackagePrecheckTests(unittest.TestCase):
    def assert_refused(self, data: bytes, reason: str) -> None:
        self.assertEqual(both(data), {"export": f"invalid evidence package: {reason}", "case": f"invalid case package: {reason}"})

    def test_a_path_that_is_absolute_or_climbs_out_is_refused(self):
        for name in ("/etc/clip.mp4", "../clip.mp4", "media/../../clip.mp4"):
            with self.subTest(name):
                self.assert_refused(package([manifest(MANIFEST), (name, b"clip")]), "invalid archive paths")

    def test_a_name_given_twice_is_refused(self):
        self.assert_refused(package([manifest(MANIFEST), ("clip.mp4", b"clip"), ("clip.mp4", b"clip")]), "invalid archive paths")

    def test_more_content_than_the_limit_is_refused_before_anything_is_read(self):
        with patch.object(exports, "MAX_VERIFY_PACKAGE_BYTES", 16):
            self.assert_refused(package([manifest(MANIFEST), ("clip.mp4", b"x" * 64)]), "uncompressed package exceeds the verification size limit")

    def test_the_manifest_missing_or_given_twice_is_refused(self):
        self.assert_refused(package([("clip.mp4", b"clip")]), "integrity manifest is missing or duplicated")
        self.assert_refused(package([manifest(MANIFEST), manifest(MANIFEST), ("clip.mp4", b"clip")]), "invalid archive paths")  # Caught as a name given twice

    def test_a_manifest_of_another_kind_is_refused(self):
        for value in ({**MANIFEST, "algorithm": "md5"}, {"algorithm": "sha256"}, {"algorithm": "sha256", "files": {}}):
            with self.subTest(value=value):
                self.assert_refused(package([manifest(value), ("clip.mp4", b"clip")]), "unsupported integrity manifest")

    def test_a_manifest_that_lists_one_path_twice_is_refused_by_each_in_its_own_words(self):
        twice = {"algorithm": "sha256", "files": [MANIFEST["files"][0], MANIFEST["files"][0]]}
        self.assertEqual(both(package([manifest(twice), ("clip.mp4", b"clip")])), {
            "export": "invalid evidence package: integrity manifest contains duplicate paths",
            "case": "package contents do not match the integrity manifest",
        })

    def test_files_that_differ_from_the_manifest_are_reported(self):
        self.assertEqual(both(package([manifest(MANIFEST), ("other.mp4", b"clip")])), {
            "export": "package contents do not match the integrity manifest", "case": "package contents do not match the integrity manifest"})

    def test_what_is_not_a_zip_at_all(self):
        self.assertEqual(both(b"not a zip"), {"export": "invalid evidence package: File is not a zip file", "case": "invalid case package: File is not a zip file"})


if __name__ == "__main__":
    unittest.main()
