"""The Python requirements are locked with hashes (#285): the image builds exactly what was locked, and the lock cannot drift from the packages listed by hand."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)")


def pins(path: str) -> dict[str, str]:
    """name (lower case, - for _) to version, for each ``name==version`` line (extras and markers ignored)."""
    found = {}
    for line in (ROOT / path).read_text().splitlines():
        match = PIN.match(line.strip())
        if match:
            found[match.group(1).lower().replace("_", "-")] = match.group(2)
    return found


class LockTests(unittest.TestCase):
    def test_every_direct_pin_is_in_the_lock_at_the_same_version(self):
        lock = pins("requirements.txt")
        direct = pins("requirements.in")
        self.assertGreater(len(direct), 5)
        self.assertEqual({name: lock.get(name) for name in direct}, direct, "run scripts/lock-requirements.sh after changing requirements.in")

    def test_the_dev_lock_has_the_tools_and_everything_the_app_needs_at_the_same_versions(self):
        dev, lock = pins("requirements-dev.txt"), pins("requirements.txt")
        self.assertIn("ruff", dev)
        self.assertEqual({name: dev.get(name) for name in lock}, lock)
        self.assertEqual(dev["ruff"], pins("requirements-dev.in")["ruff"])

    def test_every_locked_package_is_pinned_and_carries_hashes(self):
        for path in ("requirements.txt", "requirements-dev.txt", "requirements-ci-s3.txt", "requirements-ci-audit.txt"):
            text = (ROOT / path).read_text()
            blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+(?:\[[^\]]*\])?==)", text)
            packages = [block for block in blocks if PIN.match(block)]
            self.assertGreater(len(packages), 20, path)
            for block in packages:
                self.assertIn("--hash=sha256:", block, f"{path}: {block.splitlines()[0]} has no hash")

    def test_nothing_in_the_lock_is_a_loose_requirement_or_a_url(self):
        for path in ("requirements.txt", "requirements-dev.txt", "requirements-ci-s3.txt", "requirements-ci-audit.txt"):
            for line in (ROOT / path).read_text().splitlines():
                text = line.strip()
                if text and not text.startswith(("#", "--hash=")):
                    self.assertRegex(text, PIN, f"{path}: {text}")
                    self.assertNotIn("://", text)

    def test_the_ci_s3_store_is_locked_and_installed_only_from_its_lock(self):
        self.assertEqual(pins("requirements-ci-s3.txt")["moto"], pins("requirements-ci-s3.in")["moto"], "run scripts/lock-requirements.sh after changing requirements-ci-s3.in")
        script = (ROOT / ".github/scripts/start-s3-store.sh").read_text()
        self.assertIn("--require-hashes -r requirements-ci-s3.txt", script)
        self.assertIn("requirements-ci-s3", (ROOT / "scripts/lock-requirements.sh").read_text())

    def test_the_ci_vulnerability_checker_is_locked_and_installed_only_from_its_lock(self):
        self.assertEqual(pins("requirements-ci-audit.txt")["pip-audit"], pins("requirements-ci-audit.in")["pip-audit"])
        self.assertIn("--require-hashes -r requirements-ci-audit.txt", (ROOT / ".github/workflows/ci.yml").read_text())
        self.assertIn("requirements-ci-audit", (ROOT / "scripts/lock-requirements.sh").read_text())

    def test_the_image_installs_only_what_was_locked(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("--require-hashes -r requirements.txt", dockerfile)


if __name__ == "__main__":
    unittest.main()
