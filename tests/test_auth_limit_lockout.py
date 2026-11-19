"""One outside address cannot lock a local account out, and a right password does not block the second-factor step (#365)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import auth_limits as limits
from app.store import connect, initialize

ACCOUNT = "admin"
OUTSIDER, OWNER = "198.51.100.7", "192.0.2.20"


class LockoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()
        self.now = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
        clock = patch.object(limits, "_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def wrong_password(self, address: str) -> bool:
        """One wrong guess; False when it was refused for waiting."""
        try:
            limits.begin(ACCOUNT, address)
            return True
        except HTTPException:
            return False

    def failures(self, scope: str, key: str) -> int:
        with connect() as db:
            row = db.execute("SELECT failures FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        return int(row[0]) if row else 0

    def guess_for(self, address: str, minutes: int) -> None:
        """A guess every five minutes, as fast as the slowed address is allowed, for this long."""
        for _ in range(8):  # First, quickly, until it is being slowed
            self.wrong_password(address)
            self.now += timedelta(seconds=1)
        for _ in range(minutes // 5):
            self.now += timedelta(minutes=5, seconds=1)
            self.wrong_password(address)

    def test_one_address_guessing_for_hours_does_not_make_the_owner_wait(self):
        self.guess_for(OUTSIDER, minutes=180)
        self.assertGreater(self.failures("pair", f"{ACCOUNT}|{OUTSIDER}"), 30)
        self.assertLessEqual(self.failures("account", ACCOUNT), limits.LIMITS["pair"][0], "one address filled the count that slows everyone")
        limits.begin(ACCOUNT, OWNER)  # The owner, from their own address: let in at once
        limits.release(ACCOUNT, OWNER)

    def test_a_right_password_leaves_no_wait_for_the_second_factor_step(self):
        for _ in range(6):  # The owner's own typos: enough to be slowed for a moment
            self.wrong_password(OWNER)
            self.now += timedelta(seconds=40)
        limits.begin(ACCOUNT, OWNER)        # The right password
        limits.release(ACCOUNT, OWNER)      # ... is taken back, as the sign-in route does before asking for the code
        limits.begin_account(ACCOUNT, OWNER)  # The code step, a moment later: not refused
        limits.succeeded(ACCOUNT, OWNER)
        self.assertEqual(self.failures("account", ACCOUNT), 0)

    def test_an_outsider_cannot_block_the_owners_second_factor_step(self):
        self.guess_for(OUTSIDER, minutes=120)
        limits.begin(ACCOUNT, OWNER)
        limits.release(ACCOUNT, OWNER)
        self.now += timedelta(seconds=20)   # Typing the code
        limits.begin_account(ACCOUNT, OWNER)
        limits.succeeded(ACCOUNT, OWNER)

    def test_guesses_spread_over_many_addresses_still_slow_the_account(self):
        refused = 0
        for number in range(40):  # Three guesses from each of many addresses: none is slowed on its own
            for _ in range(3):
                refused += not self.wrong_password(f"203.0.113.{number}")
                self.now += timedelta(seconds=1)
        self.assertGreater(refused, 0, "spreading guesses over addresses was never slowed")
        with self.assertRaises(HTTPException) as caught:
            limits.begin(ACCOUNT, OWNER)
        self.assertEqual(caught.exception.status_code, 429)

    def test_the_guessing_address_itself_is_still_slowed_to_a_few_tries_an_hour(self):
        allowed = 0
        for _ in range(3600 // 5):  # An hour of trying every five seconds
            allowed += self.wrong_password(OUTSIDER)
            self.now += timedelta(seconds=5)
        self.assertLess(allowed, 30)


if __name__ == "__main__":
    unittest.main()
