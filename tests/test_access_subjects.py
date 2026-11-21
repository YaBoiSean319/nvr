"""Which grants a person holds, and how a token is read with its owner: pinned before their repeated queries become one each (#387).

Three functions build the same "the person, or an Entra role they hold" clause (live_grants, live_site_grants, ensure_default_grant), and two
read a token with its owner the same way (authenticate, usable_token). These tests pass on the code as it was and must pass unchanged after.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


class AccessSubjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        from app.store import connect, initialize

        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role,provider_roles_json) VALUES(1,'ada','unused','operator',?)", (json.dumps(["Guards", "Nights"]),))
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'bo','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(3,'cy','unused','administrator')")
            db.execute("INSERT INTO camera_groups(id,name) VALUES('g1','Dock'),('g2','Gate'),('g3','Yard'),('g4','Roof')")
            for site in ("north", "south", "east", "west"):
                db.execute("INSERT INTO console_sites(id,name,base_url,token_ciphertext,created_at) VALUES(?,?,'https://site.example.test',X'00',?)", (site, site, PAST))
            # A new operator is given the default "every camera" grant by a trigger; these tests set the grants themselves.
            db.execute("DELETE FROM access_grants")

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def grant(self, subject_type, subject, group, expires_at=None):
        from app.store import connect
        with connect() as db:
            db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,group_id,expires_at) VALUES(?,?,'group',?,?)", (subject_type, subject, group, expires_at))

    def site_grant(self, subject_type, subject, site, expires_at=None):
        from app.store import connect
        with connect() as db:
            db.execute("INSERT INTO site_grants(subject_type,subject,site_id,expires_at) VALUES(?,?,?,?)", (subject_type, subject, site, expires_at))

    def test_a_person_holds_their_own_grants_and_those_of_their_roles_that_have_not_expired(self):
        from app import access
        from app.store import connect
        self.grant("user", "1", "g1")
        self.grant("role", "Guards", "g2")
        self.grant("role", "Nights", "g3", expires_at=PAST)  # Expired
        self.grant("role", "Drivers", "g4")  # A role ada does not hold
        self.grant("user", "2", "g4")  # Someone else's
        with connect() as db:
            self.assertEqual(sorted(row["group_id"] for row in access.live_grants(db, 1)), ["g1", "g2"])
            self.assertEqual(sorted(row["group_id"] for row in access.live_grants(db, 2)), ["g4"])  # No roles: only their own
            self.assertEqual(access.live_grants(db, 99), [])  # No such person

    def test_sites_follow_the_same_rule(self):
        from app import access
        from app.store import connect
        self.site_grant("user", "1", "north")
        self.site_grant("role", "Nights", "south")
        self.site_grant("role", "Guards", "east", expires_at=PAST)
        self.site_grant("role", "Drivers", "west")
        with connect() as db:
            self.assertEqual(access.live_site_grants(db, 1), frozenset({"north", "south"}))
            self.assertEqual(access.live_site_grants(db, 2), frozenset())
            self.assertEqual(access.live_site_grants(db, 99), frozenset())

    def test_an_operator_without_any_grant_gets_the_default_and_one_with_a_role_grant_does_not(self):
        from app import access
        from app.store import connect

        def defaults(user_id):
            with connect() as db:
                return db.execute("SELECT COUNT(*) FROM access_grants WHERE subject_type='user' AND subject=? AND default_grant=1", (str(user_id),)).fetchone()[0]

        self.grant("role", "Guards", "g2")
        with connect() as db:
            access.ensure_default_grant(db, 1)  # A grant through a role is a grant
            access.ensure_default_grant(db, 2)  # Nothing at all: given every camera
            access.ensure_default_grant(db, 3)  # An administrator needs none
        self.assertEqual((defaults(1), defaults(2), defaults(3)), (0, 1, 0))
        with connect() as db:
            access.ensure_default_grant(db, 2)  # Never a second one
        self.assertEqual(defaults(2), 1)

    def test_a_token_is_read_with_its_owner_whichever_way_it_is_asked_for(self):
        from app import api_tokens
        from app.store import connect
        token, secret = api_tokens.create_token("Wall", ["media"], None, 3)  # Tokens belong to administrators
        token_id = secret.split("_", 2)[1]
        by_secret = api_tokens.authenticate(secret)
        by_id = api_tokens.usable_token(token_id)
        for row in (by_secret, by_id):
            self.assertEqual((row["id"], row["name"], row["owner_username"], row["owner_role"], row["owner_disabled_at"]), (token_id, "Wall", "cy", "administrator", None))
        with connect() as db:
            self.assertEqual(api_tokens.usable_token(token_id, db)["owner_username"], "cy")  # On a connection the caller has open
            db.execute("UPDATE users SET disabled_at=? WHERE id=3", (PAST,))
        for ask in (lambda: api_tokens.authenticate(secret), lambda: api_tokens.usable_token(token_id)):
            with self.assertRaisesRegex(api_tokens.TokenRejected, "disabled account"):
                ask()
        for ask in (lambda: api_tokens.authenticate(f"{secret[:-1]}x"), lambda: api_tokens.usable_token("no-such-token")):
            with self.assertRaisesRegex(api_tokens.TokenRejected, "Invalid API token"):
                ask()


if __name__ == "__main__":
    unittest.main()
