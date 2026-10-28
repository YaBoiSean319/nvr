"""Access grants: which cameras an operator may see, and until when (#119)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import access, alarms, privacy
from app.application import create_app
from app.credentials import encrypt
from app.principal import administrator, current_user, depends_on
from app.security import hash_password, new_session_token, token_digest
from app.store import connect
from tests.test_support import TEST_PASSWORD

OPERATOR_PASSWORD = "operator-password-1234"
START = datetime.now(timezone.utc) - timedelta(hours=2)
NOT_FOUND_CAMERA = {"detail": "Camera not found"}


class GrantCase(unittest.TestCase):
    """Two groups, an ungrouped camera, one operator granted only the first group and one granted everything."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"),
                            "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")}
        self.previous = {key: os.environ.get(key) for key in self.environment}
        os.environ.update(self.environment)
        (root / "recordings").mkdir()
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.admin = self.context.__enter__()
        self.assertEqual(self.admin.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 201)
        self.dock, self.yard = self.add_group("Dock"), self.add_group("Yard")
        self.d1, self.d2 = self.add_camera("Dock 1", self.dock), self.add_camera("Dock 2", self.dock)
        self.y1 = self.add_camera("Yard 1", self.yard)
        self.loose = self.add_camera("Loose")
        self.hidden = [self.y1, self.loose]
        self.limited_id = self.add_operator("limited")
        self.full_id = self.add_operator("full")
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])
        self.limited = self.sign_in("limited")
        self.full = self.sign_in("full")

    def tearDown(self):
        self.context.__exit__(None, None, None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    # --- fixtures ---

    def add_group(self, name: str) -> str:
        group_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?,?)", (group_id, name))
        return group_id

    def add_camera(self, name: str, group: str | None = None) -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,group_id,recording_mode,recording_enabled) VALUES(?,?,?,?,'continuous',1)",
                       (camera_id, name, encrypt("rtsp://camera.invalid/main"), group))
        return camera_id

    def add_operator(self, name: str) -> int:
        with connect() as db:
            return int(db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'operator')", (name, hash_password(OPERATOR_PASSWORD))).lastrowid)

    def sign_in(self, username: str) -> TestClient:
        client = TestClient(self.app)
        response = client.post("/api/auth/login", json={"username": username, "password": OPERATOR_PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def grant(self, user_id: int, grants: list[dict], subject_type: str = "user") -> None:
        response = self.admin.put("/api/admin/access-grants", json={"subject_type": subject_type, "subject": str(user_id), "grants": grants})
        self.assertEqual(response.status_code, 200, response.text)

    def audit(self, action: str) -> list[dict]:
        with connect() as db:
            return [{**dict(row), "details": json.loads(row["details_json"])} for row in db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def add_segment(self, camera_id: str, minutes: int = 0) -> int:
        start = START + timedelta(minutes=minutes)
        with connect() as db:
            return int(db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                                  (camera_id, f"{camera_id}/{minutes}.mp4", start.isoformat(), (start + timedelta(seconds=60)).isoformat())).lastrowid)

    def add_alarm(self, camera_id: str) -> str:
        with connect() as db:
            rule = str(uuid.uuid4())
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                       (rule, "Gate", json.dumps([camera_id]), "zone_entry", '{"zone":{"x":0,"y":0,"width":1,"height":1}}', '["vehicle"]', START.isoformat()))
            event = str(uuid.uuid4())
            db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (event, rule, "Gate", camera_id, "t", "vehicle", "zone_entry", START.isoformat(), '{"x":0,"y":0,"width":0.1,"height":0.1}'))
            return alarms.create_alarm(db, event)

    def add_observation(self, camera_id: str) -> None:
        with connect() as db:
            db.execute("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version) "
                       "VALUES(?,?,'person',0.9,'{}',?,?,640,480,'m','1')", (str(uuid.uuid4()), camera_id, START.isoformat(), START.isoformat()))

    def add_track(self, camera_id: str) -> str:
        track = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','CPU')",
                       (track, camera_id, START.isoformat(), START.isoformat()))
        return track

    def ids(self, response, key: str, field: str = "id") -> set[str]:
        self.assertEqual(response.status_code, 200, response.text)
        return {item[field] for item in response.json()[key]}


class ResolutionTests(GrantCase):
    def test_a_group_grant_gives_that_groups_cameras_and_an_all_grant_gives_everything(self):
        with connect() as db:
            limited = access.resolve(db, self.limited_id, "operator")
            self.assertEqual((limited.cameras, limited.groups), (frozenset({self.d1, self.d2}), frozenset({self.dock})))
            self.assertTrue(access.resolve(db, self.full_id, "operator").unrestricted)
            self.assertTrue(access.resolve(db, 1, "administrator").unrestricted)  # Administrators need no grant.

    def test_cameras_added_to_a_granted_group_later_are_included(self):
        late = self.add_camera("Dock 3", self.dock)
        self.assertIn(late, self.ids(self.limited.get("/api/cameras"), "cameras"))
        with connect() as db:  # A membership beyond the camera's own group counts too.
            db.execute("INSERT INTO camera_group_memberships(group_id,camera_id) VALUES(?,?)", (self.dock, self.loose))
        self.assertIn(self.loose, self.ids(self.limited.get("/api/cameras"), "cameras"))

    def test_an_operator_without_grants_sees_no_cameras(self):
        self.grant(self.limited_id, [])
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), set())
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments").status_code, 404)

    def test_every_operator_gets_everything_until_narrowed_and_new_operators_do_too(self):
        newcomer = self.add_operator("newcomer")
        with connect() as db:
            self.assertTrue(access.resolve(db, newcomer, "operator").unrestricted)
            self.assertEqual([row["scope_type"] for row in db.execute("SELECT scope_type FROM access_grants WHERE subject=?", (str(newcomer),))], ["all"])

    def test_a_grant_for_an_entra_role_applies_to_everyone_holding_it(self):
        entra = self.add_operator("entra-user")
        with connect() as db:
            db.execute("DELETE FROM access_grants WHERE subject=?", (str(entra),))
            db.execute("UPDATE users SET provider_roles_json=? WHERE id=?", (json.dumps(["NVR.Contractor"]), entra))
        self.grant("NVR.Contractor", [{"scope_type": "group", "group_id": self.yard}], subject_type="role")
        with connect() as db:
            self.assertEqual(access.resolve(db, entra, "operator").cameras, frozenset({self.y1}))
            db.execute("UPDATE users SET provider_roles_json='[]' WHERE id=?", (entra,))
            self.assertEqual(access.resolve(db, entra, "operator").cameras, frozenset())  # No longer holding the role.

    def test_a_new_entra_operator_whose_role_has_grants_is_not_given_everything(self):
        self.grant("NVR.Contractor", [{"scope_type": "group", "group_id": self.yard}], subject_type="role")
        with connect() as db:
            user = db.execute("INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject,provider_roles_json) VALUES('c','x','operator','https://login.microsoftonline.com/t/v2.0','s',?)",
                              (json.dumps(["NVR.Contractor"]),)).lastrowid
            self.assertEqual(access.resolve(db, user, "operator").cameras, frozenset({self.y1}))
            plain = db.execute("INSERT INTO users(username,password_hash,role,provider_issuer,provider_subject,provider_roles_json) VALUES('d','x','operator','https://login.microsoftonline.com/t/v2.0','s2','[\"Other\"]')").lastrowid
            self.assertTrue(access.resolve(db, plain, "operator").unrestricted)

    def test_a_demoted_administrator_gets_the_default_grant_and_a_promoted_operator_needs_none(self):
        with connect() as db:
            user = db.execute("INSERT INTO users(username,password_hash,role) VALUES('boss','x','administrator')").lastrowid
            self.assertEqual(db.execute("SELECT COUNT(*) FROM access_grants WHERE subject=?", (str(user),)).fetchone()[0], 0)
            db.execute("UPDATE users SET role='operator' WHERE id=?", (user,))
            access.ensure_default_grant(db, user)
            access.ensure_default_grant(db, user)  # Idempotent.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM access_grants WHERE subject=?", (str(user),)).fetchone()[0], 1)
            self.assertTrue(access.resolve(db, user, "operator").unrestricted)

    def test_deleting_a_user_or_a_group_removes_their_grants(self):
        with connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (self.limited_id,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM access_grants WHERE subject=?", (str(self.limited_id),)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM access_grants WHERE group_id=?", (self.dock,)).fetchone()[0], 0)


class ExpiryTests(GrantCase):
    def test_an_expired_grant_gives_nothing_from_that_moment(self):
        soon = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock, "expires_at": soon.isoformat()}])
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), {self.d1, self.d2})
        later = soon + timedelta(seconds=1)
        with mock.patch.object(access, "_now", return_value=later):
            self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), set())
            self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments").status_code, 404)

    def test_an_expired_grant_is_listed_as_expired_and_a_lapsed_all_grant_narrows_to_the_rest(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with connect() as db:
            db.execute("DELETE FROM access_grants WHERE subject=?", (str(self.full_id),))
            db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,expires_at) VALUES('user',?,'all',?)", (str(self.full_id), past))
            db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,group_id) VALUES('user',?,'group',?)", (str(self.full_id), self.yard))
        self.assertEqual(self.ids(self.full.get("/api/cameras"), "cameras"), {self.y1})
        grants = self.admin.get("/api/admin/access-grants").json()["grants"]
        self.assertEqual([grant["expired"] for grant in grants if grant["subject"] == str(self.full_id)], [True, False])

    def test_an_open_stream_stops_when_access_ends(self):
        guard = privacy.StreamGuard(type("Viewer", (), {"id": self.limited_id})(), self.d1, unmasked=False)
        with mock.patch.object(privacy, "ACCESS_RECHECK_SECONDS", 0):
            self.assertTrue(guard.still_valid())
            self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
            self.assertFalse(guard.still_valid())
            self.assertEqual(guard.reason, "access")
            self.assertFalse(guard.still_valid())  # And it stays stopped.

    def test_a_stream_for_a_camera_that_is_still_allowed_carries_on(self):
        guard = privacy.StreamGuard(type("Viewer", (), {"id": self.limited_id})(), self.d1, unmasked=False)
        with mock.patch.object(privacy, "ACCESS_RECHECK_SECONDS", 0):
            self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])
            self.assertTrue(guard.still_valid())

    def test_grants_must_expire_in_the_future_with_a_time_zone(self):
        for expiry in ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "2099-01-01T00:00:00"):
            response = self.admin.put("/api/admin/access-grants", json={"subject_type": "user", "subject": str(self.limited_id),
                                                                       "grants": [{"scope_type": "group", "group_id": self.dock, "expires_at": expiry}]})
            self.assertEqual(response.status_code, 422, expiry)


class ManagementTests(GrantCase):
    def test_grants_are_replaced_in_one_audited_step(self):
        changed = self.audit("access_grants_changed")
        self.assertEqual(len(changed), 1)  # The setUp grant.
        before = {(item["scope_type"], item["group_id"]) for item in changed[0]["details"]["before"]}
        self.assertEqual(before, {("all", None)})  # What the operator had by default.
        self.assertEqual(changed[0]["details"]["after"][0]["group_id"], self.dock)
        self.assertEqual((changed[0]["actor_id"], changed[0]["details"]["subject_name"]), (1, "limited"))

    def test_the_listing_names_operators_roles_and_groups(self):
        self.grant("NVR.Contractor", [{"scope_type": "all"}], subject_type="role")
        listing = self.admin.get("/api/admin/access-grants").json()
        self.assertEqual({user["username"] for user in listing["operators"]}, {"limited", "full"})
        self.assertIn("NVR.Contractor", listing["roles"])
        self.assertEqual({group["name"] for group in listing["groups"]}, {"Dock", "Yard"})
        self.assertIn("Dock", {grant["group_name"] for grant in listing["grants"]})

    def test_one_grant_can_be_deleted_and_is_audited(self):
        grant = next(grant for grant in self.admin.get("/api/admin/access-grants").json()["grants"] if grant["subject"] == str(self.limited_id))
        self.assertEqual(self.admin.delete(f"/api/admin/access-grants/{grant['id']}").status_code, 204)
        self.assertEqual(self.admin.delete(f"/api/admin/access-grants/{grant['id']}").status_code, 404)
        self.assertEqual(self.audit("access_grant_deleted")[0]["details"]["group_id"], self.dock)
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), set())

    def test_invalid_grants_are_refused_and_change_nothing(self):
        before = self.admin.get("/api/admin/access-grants").json()["grants"]
        bad = [
            {"subject_type": "user", "subject": "1", "grants": [{"scope_type": "all"}]},  # An administrator.
            {"subject_type": "user", "subject": "999", "grants": []},
            {"subject_type": "user", "subject": "nope", "grants": []},
            {"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "group", "group_id": "missing"}]},
            {"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "group"}]},
            {"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "all", "group_id": self.dock}]},
            {"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "group", "group_id": self.dock}] * 2},
            {"subject_type": "role", "subject": "  ", "grants": []},
        ]
        for payload in bad:
            self.assertIn(self.admin.put("/api/admin/access-grants", json=payload).status_code, {404, 422}, payload)
        self.assertEqual(self.admin.get("/api/admin/access-grants").json()["grants"], before)

    def test_only_administrators_manage_grants(self):
        self.assertEqual(self.limited.get("/api/admin/access-grants").status_code, 403)
        self.assertEqual(self.limited.put("/api/admin/access-grants", json={"subject_type": "user", "subject": str(self.limited_id), "grants": [{"scope_type": "all"}]}).status_code, 403)
        self.assertEqual(self.limited.delete("/api/admin/access-grants/1").status_code, 403)

    def test_narrowing_takes_effect_on_the_next_request_of_a_signed_in_operator(self):
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), {self.d1, self.d2})
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), {self.y1})


class RouteTests(GrantCase):
    """Every route an operator can reach is either gated by the camera or group it names, filters what it returns, or touches no camera."""

    # Routes that return or act on camera data without naming a camera in the path or query.
    FILTERED = {
        ("GET", "/api/cameras"), ("GET", "/api/camera-groups"), ("GET", "/api/ai/events"), ("GET", "/api/ai/tracks/{track_id}"),
        ("GET", "/api/alarms"), ("GET", "/api/alarms/{alarm_id}"), ("POST", "/api/alarms/{alarm_id}/acknowledge"), ("POST", "/api/alarms/{alarm_id}/assign"),
        ("POST", "/api/alarms/{alarm_id}/comments"), ("POST", "/api/alarms/{alarm_id}/resolve"),
        ("GET", "/api/exports"), ("POST", "/api/exports"), ("GET", "/api/exports/{job_id}"), ("POST", "/api/exports/{job_id}/cancel"),
        ("GET", "/api/exports/{job_id}/download"), ("POST", "/api/exports/{job_id}/retry"), ("POST", "/api/exports/{job_id}/verify"),
        ("GET", "/api/incidents"), ("POST", "/api/incidents/{incident_id}/acknowledge"),
        ("GET", "/api/live/layouts"), ("DELETE", "/api/live/layouts/{name}"), ("PUT", "/api/live/layouts/{name}"), ("GET", "/api/live/views"),
        ("GET", "/api/live/workspace"), ("PUT", "/api/live/workspace"), ("GET", "/api/live/alarm-pop-ins"), ("POST", "/api/playback/timeline"),
        ("GET", "/api/ai/search"), ("GET", "/api/insights"), ("GET", "/api/digests"), ("GET", "/api/digests/preview"), ("GET", "/api/digests/{digest_id}"), ("GET", "/api/insights/export.csv"), ("GET", "/api/ai/search/options"), ("GET", "/api/ai/tracks/{track_id}/thumbnail"), ("GET", "/api/ai/tracks/{track_id}/similar"), ("GET", "/api/ai/search/ask"),
        ("GET", "/api/cases"), ("POST", "/api/cases"), ("GET", "/api/cases/{case_id}"), ("PATCH", "/api/cases/{case_id}"), ("POST", "/api/cases/{case_id}/members"),
        ("DELETE", "/api/cases/{case_id}/members/{user_id}"), ("POST", "/api/cases/{case_id}/items"), ("DELETE", "/api/cases/{case_id}/items/{item_id}"), ("POST", "/api/cases/{case_id}/items/{item_id}/viewed"),
        ("POST", "/api/cases/{case_id}/close"), ("GET", "/api/cases/{case_id}/audit"), ("POST", "/api/cases/{case_id}/packages"),
        ("GET", "/api/cases/{case_id}/packages/{package_id}"), ("DELETE", "/api/cases/{case_id}/packages/{package_id}"), ("GET", "/api/cases/{case_id}/packages/{package_id}/download"),
        ("POST", "/api/cases/{case_id}/packages/{package_id}/verify"), ("GET", "/api/playback/overlay"), ("GET", "/api/playback/overlay/tracks/{track_id}"),
        ("POST", "/api/redaction/reviews"), ("GET", "/api/redaction/reviews/{review_id}"), ("PATCH", "/api/redaction/reviews/{review_id}"),
        ("POST", "/api/redaction/reviews/{review_id}/confirm"),
    }
    # Routes that name no camera and return none: the person's own settings, sign-in and public information.
    NO_CAMERA_DATA = {
        ("GET", "/api/ai/settings"), ("GET", "/api/alarm-settings"), ("GET", "/api/alarms/assignees"), ("GET", "/api/auth/me"),
        ("GET", "/api/footage/signing-key"), ("GET", "/api/me/preferences"), ("PUT", "/api/me/preferences"), ("GET", "/api/me/security"),
        ("PUT", "/api/me/security/passkey-sign-in"), ("POST", "/api/me/security/passkeys"), ("POST", "/api/me/security/passkeys/options"),
        ("DELETE", "/api/me/security/passkeys/{passkey_id}"), ("POST", "/api/me/security/recovery-codes"), ("DELETE", "/api/me/security/totp"),
        ("POST", "/api/me/security/totp/begin"), ("POST", "/api/me/security/totp/confirm"), ("GET", "/api/push/preferences"),
        ("PUT", "/api/push/preferences"), ("GET", "/api/push/public-key"), ("POST", "/api/push/subscriptions"), ("POST", "/api/push/subscriptions/remove"),
        ("GET", "/api/settings/display"), ("GET", "/api/redaction/models"),
    }

    def query_gated(self, route) -> bool:
        return any(param.name == "camera_id" for param in route.dependant.query_params)

    def reachable(self) -> list[tuple[str, str, object]]:
        found = []
        for route in self.app.routes:
            dependant = getattr(route, "dependant", None)
            if dependant is None or not route.path.startswith("/api") or not depends_on(dependant, current_user) or depends_on(dependant, administrator):
                continue
            found.extend((method, route.path, route) for method in sorted(route.methods - {"HEAD", "OPTIONS"}))
        return found

    def test_every_route_an_operator_can_reach_is_accounted_for(self):
        reachable = {(method, path) for method, path, _ in self.reachable()}
        by_path = {(method, path) for method, path in reachable if "{camera_id}" in path or "{group_id}" in path}
        by_query = {(method, path) for method, path, route in self.reachable() if self.query_gated(route)}
        accounted = by_path | by_query | self.FILTERED | self.NO_CAMERA_DATA
        self.assertEqual(reachable - accounted, set(), "A route an operator can reach is not classified: gate it by camera or group, filter what it returns, or list it as touching no camera")
        self.assertEqual(accounted - reachable, set(), "A classified route no longer exists or is no longer reachable by operators")
        # The OpenAPI document and the router agree, so the walk below covers what clients can see.
        documented = {(method.upper(), path) for path, item in self.app.openapi()["paths"].items() for method in item}
        self.assertTrue(by_path <= documented)

    def test_every_route_naming_a_camera_or_group_treats_hidden_ones_as_missing(self):
        checked = 0
        for method, path, _ in self.reachable():
            if "{camera_id}" not in path and "{group_id}" not in path:
                continue
            hidden = path.replace("{camera_id}", self.y1).replace("{group_id}", self.yard)
            missing = path.replace("{camera_id}", str(uuid.uuid4())).replace("{group_id}", str(uuid.uuid4()))
            for filler in ("{segment_id}", "{preset_id}", "{tour_id}"):
                hidden, missing = hidden.replace(filler, "1"), missing.replace(filler, "1")
            kwargs = {"json": {}} if method in {"POST", "PUT", "PATCH", "DELETE"} else {}
            response_hidden = self.limited.request(method, hidden, **kwargs)
            response_missing = self.limited.request(method, missing, **kwargs)
            self.assertEqual(response_hidden.status_code, 404, f"{method} {path}: {response_hidden.text[:200]}")
            self.assertEqual((response_hidden.status_code, response_hidden.json()), (response_missing.status_code, response_missing.json()), f"{method} {path}")
            checked += 1
        window = {"start": (START - timedelta(hours=1)).isoformat(), "end": (START + timedelta(hours=1)).isoformat()}
        for method, path, route in self.reachable():
            if self.query_gated(route) and "{camera_id}" not in path:
                hidden = self.limited.request(method, path, params={"camera_id": self.y1, **window})
                missing = self.limited.request(method, path, params={"camera_id": str(uuid.uuid4()), **window})
                self.assertEqual((hidden.status_code, hidden.json()), (missing.status_code, missing.json()), f"{method} {path}")
                self.assertEqual(hidden.status_code, 404, f"{method} {path}")
                checked += 1
        self.assertGreater(checked, 25)

    def test_the_same_routes_still_work_for_a_permitted_camera_and_for_everyone_else(self):
        segment = self.add_segment(self.d1)
        for client, camera in ((self.limited, self.d1), (self.full, self.y1), (self.admin, self.loose)):
            self.assertNotEqual(client.get(f"/api/cameras/{camera}/segments").status_code, 404)
            self.assertNotEqual(client.get(f"/api/cameras/{camera}/live/status").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/segments/{segment}/media").status_code in {200, 404, 410, 503}, True)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/coverage").status_code, 200)

    def test_a_camera_named_in_the_query_is_checked_too(self):
        start, end = (START - timedelta(hours=1)).isoformat(), (START + timedelta(hours=1)).isoformat()
        for path in ("/api/ai/detections", "/api/ai/tracks"):
            self.assertEqual(self.limited.get(path, params={"camera_id": self.y1, "start": start, "end": end}).status_code, 404, path)
            self.assertEqual(self.limited.get(path, params={"camera_id": self.d1, "start": start, "end": end}).status_code, 200, path)
        self.assertEqual(self.limited.get("/api/alarms", params={"camera_id": self.y1}).status_code, 404)
        self.assertEqual(self.limited.get("/api/ai/events", params={"camera_id": self.y1}).status_code, 404)

    def test_administrators_and_all_cameras_operators_see_everything(self):
        for client in (self.admin, self.full):
            self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.d1, self.d2, self.y1, self.loose})
            self.assertEqual(self.ids(client.get("/api/camera-groups"), "groups"), {self.dock, self.yard})


class ContentTests(GrantCase):
    def test_lists_show_only_permitted_cameras_and_groups(self):
        self.assertEqual(self.ids(self.limited.get("/api/cameras"), "cameras"), {self.d1, self.d2})
        groups = self.limited.get("/api/camera-groups").json()["groups"]
        self.assertEqual([(group["id"], sorted(group["camera_ids"]), group["camera_count"]) for group in groups], [(self.dock, sorted([self.d1, self.d2]), 2)])
        self.assertEqual(self.limited.get(f"/api/camera-groups/{self.yard}/members").status_code, 404)

    def test_ai_search_across_all_cameras_returns_only_permitted_ones(self):
        for camera in (self.d1, self.y1, self.loose):
            self.add_observation(camera)
        events = self.limited.get("/api/ai/events").json()["events"]
        self.assertEqual({event["camera_id"] for event in events}, {self.d1})
        self.assertEqual({event["camera_id"] for event in self.admin.get("/api/ai/events").json()["events"]}, {self.d1, self.y1, self.loose})
        self.grant(self.limited_id, [])
        self.assertEqual(self.limited.get("/api/ai/events").json()["events"], [])

    def test_a_track_of_a_hidden_camera_is_missing(self):
        hidden, shown = self.add_track(self.y1), self.add_track(self.d1)
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{hidden}").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{shown}").status_code, 200)
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{uuid.uuid4()}").json(), self.limited.get(f"/api/ai/tracks/{hidden}").json())

    def test_alarms_of_hidden_cameras_are_missing_everywhere(self):
        shown, hidden = self.add_alarm(self.d1), self.add_alarm(self.y1)
        listed = self.limited.get("/api/alarms", params={"state": "all"}).json()
        self.assertEqual([alarm["id"] for alarm in listed["alarms"]], [shown])
        self.assertEqual(listed["counts"]["new"], 1)  # The count does not give the hidden one away.
        self.assertEqual([rule["name"] for rule in listed["rules"]], ["Gate"])
        self.assertEqual(self.limited.get(f"/api/alarms/{hidden}").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/alarms/{shown}").status_code, 200)
        for action, body in (("acknowledge", None), ("assign", {"user_id": self.limited_id}), ("comments", {"body": "hi"}), ("resolve", {"note": "x"})):
            self.assertEqual(self.limited.post(f"/api/alarms/{hidden}/{action}", json=body).status_code, 404, action)
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM alarms WHERE id=?", (hidden,)).fetchone()[0], "new")
        self.assertEqual(self.limited.post(f"/api/alarms/{shown}/acknowledge").status_code, 200)

    def test_alarms_of_rules_seen_only_through_hidden_cameras_do_not_show_in_the_rule_filter(self):
        self.add_alarm(self.y1)
        self.assertEqual(self.limited.get("/api/alarms").json()["rules"], [])
        self.assertEqual(len(self.admin.get("/api/alarms").json()["rules"]), 1)

    def test_incidents_show_only_those_of_permitted_cameras(self):
        from app.incidents import sync_incident
        with connect() as db:
            for camera, key in ((self.d1, "a"), (self.y1, "b"), (None, "c")):
                sync_incident(db, incident_key=f"test:{key}", camera_id=camera, fault_type="recording_failure", summary=f"fault {key}", active=True)
            ids = {row["incident_key"]: row["id"] for row in db.execute("SELECT id,incident_key FROM incidents")}
        self.assertEqual({incident["summary"] for incident in self.limited.get("/api/incidents").json()["incidents"]}, {"fault a"})
        self.assertEqual({incident["summary"] for incident in self.admin.get("/api/incidents").json()["incidents"]}, {"fault a", "fault b", "fault c"})
        self.assertEqual(self.limited.post(f"/api/incidents/{ids['test:b']}/acknowledge").status_code, 404)
        self.assertEqual(self.limited.post(f"/api/incidents/{ids['test:c']}/acknowledge").status_code, 404)  # A system incident.
        self.assertEqual(self.limited.post(f"/api/incidents/{ids['test:a']}/acknowledge").status_code, 200)
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM incidents WHERE incident_key='test:b'").fetchone()[0], "active")

    def test_playback_treats_a_hidden_camera_as_unknown(self):
        body = {"camera_ids": [self.d1, self.y1], "start": (START - timedelta(minutes=5)).isoformat(), "end": (START + timedelta(minutes=5)).isoformat()}
        hidden = self.limited.post("/api/playback/timeline", json=body)
        unknown = self.limited.post("/api/playback/timeline", json={**body, "camera_ids": [self.d1, str(uuid.uuid4())]})
        self.assertEqual((hidden.status_code, hidden.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(hidden.status_code, 422)
        self.assertEqual(self.limited.post("/api/playback/timeline", json={**body, "camera_ids": [self.d1]}).status_code, 200)
        self.assertEqual(self.full.post("/api/playback/timeline", json=body).status_code, 200)

    def test_exports_of_hidden_cameras_are_unknown_to_create_and_missing_afterwards(self):
        window = {"start": (START - timedelta(minutes=5)).isoformat(), "end": (START + timedelta(minutes=5)).isoformat(), "output_modes": ["individual"]}
        refused = self.limited.post("/api/exports", json={**window, "camera_ids": [self.y1]})
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "Export request contains an unknown camera"))
        self.assertEqual(refused.json(), self.limited.post("/api/exports", json={**window, "camera_ids": [str(uuid.uuid4())]}).json())
        # An export made while the operator could see everything disappears when access narrows.
        self.grant(self.limited_id, [{"scope_type": "all"}])
        self.add_segment(self.d1)
        self.add_segment(self.y1)
        self.app.state.exports = type("Queue", (), {"enqueue": staticmethod(lambda job: None)})()  # No worker runs in this test.
        created = self.limited.post("/api/exports", json={**window, "camera_ids": [self.d1, self.y1]})
        self.assertEqual(created.status_code, 202, created.text)
        job = created.json()["id"]
        self.assertIn(job, self.ids(self.limited.get("/api/exports"), "exports"))
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])
        self.assertNotIn(job, self.ids(self.limited.get("/api/exports"), "exports"))
        for method, path in (("GET", f"/api/exports/{job}"), ("GET", f"/api/exports/{job}/download"), ("POST", f"/api/exports/{job}/cancel"),
                             ("POST", f"/api/exports/{job}/retry"), ("POST", f"/api/exports/{job}/verify")):
            response = self.limited.request(method, path)
            self.assertEqual((response.status_code, response.json()["detail"]), (404, "Export job not found"), path)
        self.assertEqual(self.admin.get(f"/api/exports/{job}").status_code, 200)  # Administrators see every export.

    def test_saved_views_layouts_and_workspace_show_only_permitted_cameras(self):
        self.grant(self.limited_id, [{"scope_type": "all"}])
        layout = {"camera_ids": [self.d1, self.y1], "grid_size": 4, "tile_camera_ids": [self.d1, self.y1, None, None], "layout_mode": "manual"}
        self.assertEqual(self.limited.put("/api/live/layouts/Mixed", json=layout).status_code, 200)
        self.assertEqual(self.limited.put("/api/live/layouts/Hidden only", json={**layout, "camera_ids": [self.y1], "tile_camera_ids": [self.y1, None, None, None]}).status_code, 200)
        self.assertEqual(self.limited.put("/api/live/workspace", json={"camera_ids": [self.d1, self.y1], "grid_size": 4, "tile_camera_ids": [self.d1, self.y1, None, None]}).status_code, 200)
        self.assertEqual(self.admin.put("/api/live/shared-layouts/Everything", json=layout).status_code, 200)
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])

        views = self.limited.get("/api/live/views").json()
        self.assertEqual([(item["name"], item["camera_ids"], item["tile_camera_ids"]) for item in views["layouts"]], [("Mixed", [self.d1], [self.d1, None, None, None])])
        self.assertEqual([(item["name"], item["camera_ids"]) for item in views["shared_views"]], [("Everything", [self.d1])])
        self.assertEqual([(item["name"], item["camera_ids"]) for item in self.limited.get("/api/live/layouts").json()["layouts"]], [("Mixed", [self.d1])])
        workspace = self.limited.get("/api/live/workspace").json()
        self.assertEqual((workspace["camera_ids"], workspace["tile_camera_ids"]), ([self.d1], [self.d1, None, None, None]))
        # What is stored is untouched, so access restored brings the view back.
        self.grant(self.limited_id, [{"scope_type": "all"}])
        self.assertEqual(self.limited.get("/api/live/workspace").json()["camera_ids"], [self.d1, self.y1])

    def test_saving_a_view_with_a_hidden_camera_is_refused_like_an_unknown_one(self):
        layout = {"camera_ids": [self.y1], "grid_size": 4, "tile_camera_ids": [self.y1, None, None, None], "layout_mode": "manual"}
        stranger = str(uuid.uuid4())
        unknown = {**layout, "camera_ids": [stranger], "tile_camera_ids": [stranger, None, None, None]}
        for path, key in (("/api/live/layouts/Mixed", None),):
            hidden, missing = self.limited.put(path, json=layout), self.limited.put(path, json=unknown)
            self.assertEqual((hidden.status_code, hidden.json()), (missing.status_code, missing.json()))
        workspace = {"camera_ids": [self.y1], "grid_size": 4, "tile_camera_ids": [self.y1, None, None, None]}
        self.assertEqual(self.limited.put("/api/live/workspace", json=workspace).status_code, 422)

    def test_neighbours_show_only_permitted_cameras_and_pop_ins_respect_access(self):
        self.assertEqual(self.admin.put(f"/api/cameras/{self.d1}/neighbours", json={"camera_ids": [self.d2, self.y1]}).status_code, 200)
        self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/neighbours").json()["camera_ids"], [self.d2])
        self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/neighbours").json()["camera_ids"], [self.d2, self.y1])
        self.assertTrue(access.may_view_camera(connect().__enter__(), self.limited_id, self.d1))
        with connect() as db:
            self.assertFalse(access.may_view_camera(db, self.limited_id, self.y1))
            self.assertFalse(access.may_view_camera(db, self.limited_id, str(uuid.uuid4())))
            self.assertTrue(access.may_view_camera(db, self.full_id, self.y1))
            self.assertTrue(access.may_view_camera(db, 1, self.loose))


class TokenTests(GrantCase):
    def test_a_token_has_its_creators_access_and_ends_with_their_administrator_role(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "feed", "scopes": ["read"]})
        self.assertEqual(created.status_code, 201, created.text)
        headers = {"Authorization": f"Bearer {created.json()['secret']}"}
        client = TestClient(self.app)
        self.assertEqual(self.ids(client.get("/api/cameras", headers=headers), "cameras"), {self.d1, self.d2, self.y1, self.loose})
        # Tokens are made by administrators, who see everything; a creator who is no longer one takes the token with them.
        with connect() as db:
            db.execute("UPDATE users SET role='operator' WHERE id=1")
            db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,group_id) VALUES('user','1','group',?)", (self.dock,))
        self.assertEqual(client.get("/api/cameras", headers=headers).status_code, 401)

    def test_a_token_principal_resolves_its_creators_grants(self):
        from app import principal
        with connect() as db:
            self.assertEqual(principal.resolve_access(db, self.limited_id, "operator").cameras, frozenset({self.d1, self.d2}))


class LiveSocketTests(GrantCase):
    """The live WebSocket, with a stub hub in place of a real camera."""

    class StubStream:
        mime = 'video/mp4; codecs="avc1.64001f"'
        init = b"init"
        failed = None
        accelerated = False

        class ready:  # noqa: N801 - mimics asyncio.Event for the one thing the socket uses it for
            @staticmethod
            def is_set() -> bool:
                return True

            @staticmethod
            async def wait() -> bool:
                return True

    def stub_hub(self):
        stream = self.StubStream()

        def join(key, source, include_audio, viewer, on_fragment=None, video_filter=None, transcode=False, hevc_copy=False):
            viewer.offer(b"fragment")
            return stream

        hub = self.app.state.live_hub
        return mock.patch.multiple(hub, join=join, leave=lambda stream, viewer: None)

    def refusal(self, client: TestClient, camera: str) -> tuple[dict, int]:
        from starlette.websockets import WebSocketDisconnect
        with client.websocket_connect(f"/api/cameras/{camera}/live/stream") as websocket:
            message = websocket.receive_json()
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        return message, closed.exception.code

    def test_a_hidden_camera_is_refused_exactly_like_one_that_does_not_exist(self):
        hidden = self.refusal(self.limited, self.y1)
        missing = self.refusal(self.limited, str(uuid.uuid4()))
        self.assertEqual(hidden, missing)
        self.assertEqual(hidden, ({"type": "not_found", "detail": "Camera not found"}, 4404))

    def test_a_viewer_whose_access_ends_is_cut_off_within_seconds_even_on_a_quiet_camera(self):
        import time
        from starlette.websockets import WebSocketDisconnect
        with self.stub_hub(), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
            self.assertEqual(websocket.receive_bytes(), b"init")
            self.assertEqual(websocket.receive_bytes(), b"fragment")
            self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
            began = time.monotonic()
            self.assertEqual(websocket.receive_json(), {"type": "forbidden", "detail": "Your access to this camera has ended"})
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            self.assertEqual(closed.exception.code, 4403)
            self.assertLess(time.monotonic() - began, 5)

    def test_a_viewer_who_keeps_their_access_keeps_streaming(self):
        import time
        with self.stub_hub(), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            websocket.receive_bytes()
            self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])  # Changed, but still allowed.
            time.sleep(2.5)  # Two idle wake-ups pass without closing the socket.
            self.assertEqual(self.limited.get(f"/api/cameras/{self.d1}/live/status").status_code, 200)


class EntraAndDefaultsTests(GrantCase):
    def sign_in_with_entra(self, claims_roles: list[str], subject: str = "object-1") -> dict:
        from unittest.mock import AsyncMock, patch
        from tests.test_bootstrap import OIDC_TEST_TENANT
        from tests.test_support import TEST_OIDC_CLIENT_SECRET, TEST_OIDC_SESSION_SECRET
        os.environ.update({"NVR_PUBLIC_URL": "https://nvr.example.test", "NVR_OIDC_TENANT_ID": OIDC_TEST_TENANT, "NVR_OIDC_CLIENT_ID": "client-id",
                           "NVR_OIDC_CLIENT_SECRET": TEST_OIDC_CLIENT_SECRET, "NVR_OIDC_SESSION_SECRET": TEST_OIDC_SESSION_SECRET,
                           "NVR_OIDC_OPERATOR_ROLES": "NVR.Operator,NVR.Contractor", "NVR_OIDC_ADMIN_ROLES": "NVR.Admin"})
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_OPERATOR_ROLES", "NVR_OIDC_ADMIN_ROLES"):
            self.addCleanup(os.environ.pop, key, None)
        claims = {"sub": subject, "tid": OIDC_TEST_TENANT, "iss": f"https://login.microsoftonline.com/{OIDC_TEST_TENANT}/v2.0", "preferred_username": f"{subject}@example.test", "roles": claims_roles}
        fake = type("FakeOIDC", (), {"authorize_access_token": AsyncMock(return_value={"userinfo": claims})})()
        client = TestClient(self.app, base_url="https://testserver.local")  # The session cookie is Secure under an https public URL.
        with patch("app.api.identity._oidc_client", return_value=fake):
            self.assertEqual(client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False).status_code, 303)
        return client

    def entra_user(self, subject: str = "object-1") -> int:
        with connect() as db:
            return int(db.execute("SELECT id FROM users WHERE provider_subject=?", (subject,)).fetchone()[0])

    def test_an_entra_operators_roles_are_stored_and_their_role_grants_apply(self):
        self.grant("NVR.Contractor", [{"scope_type": "group", "group_id": self.yard}], subject_type="role")
        client = self.sign_in_with_entra(["NVR.Contractor"])
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.y1})  # Their role's grant, not everything.
        with connect() as db:
            self.assertEqual(json.loads(db.execute("SELECT provider_roles_json FROM users WHERE id=?", (self.entra_user(),)).fetchone()[0]), ["NVR.Contractor"])

    def test_a_role_grant_given_later_narrows_existing_entra_operators_who_only_have_the_default(self):
        client = self.sign_in_with_entra(["NVR.Contractor"])  # No role grants yet: the default, every camera.
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.d1, self.d2, self.y1, self.loose})
        self.grant("NVR.Contractor", [{"scope_type": "group", "group_id": self.dock}], subject_type="role")
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.d1, self.d2})  # The default yields to the role's grant.
        # A grant an administrator chose for the person themselves does not.
        self.grant(self.entra_user(), [{"scope_type": "all"}])
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.d1, self.d2, self.y1, self.loose})

    def test_an_operator_an_administrator_left_with_no_cameras_stays_that_way_at_next_sign_in(self):
        self.sign_in_with_entra(["NVR.Operator"])
        self.grant(self.entra_user(), [])
        client = self.sign_in_with_entra(["NVR.Operator"])
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), set())

    def test_a_role_dropped_in_entra_stops_applying_at_next_sign_in_without_a_default_taking_its_place(self):
        self.grant("NVR.Contractor", [{"scope_type": "group", "group_id": self.yard}], subject_type="role")
        self.sign_in_with_entra(["NVR.Contractor"])
        client = self.sign_in_with_entra(["NVR.Operator"])  # Still an operator, no longer a contractor.
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), set())

    def test_a_demoted_administrator_gets_the_default_once(self):
        admin = self.sign_in_with_entra(["NVR.Admin"], subject="object-2")
        self.assertEqual(admin.get("/api/admin/access-grants").status_code, 200)
        client = self.sign_in_with_entra(["NVR.Operator"], subject="object-2")
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), {self.d1, self.d2, self.y1, self.loose})
        self.grant(self.entra_user("object-2"), [])
        client = self.sign_in_with_entra(["NVR.Operator"], subject="object-2")
        self.assertEqual(self.ids(client.get("/api/cameras"), "cameras"), set())

    def test_the_listing_marks_automatic_grants_as_defaults(self):
        grants = {grant["subject"]: grant for grant in self.admin.get("/api/admin/access-grants").json()["grants"]}
        self.assertTrue(grants[str(self.full_id)]["default"])
        self.assertFalse(grants[str(self.limited_id)]["default"])  # Chosen by an administrator.


class LeakTests(GrantCase):
    """What a restricted operator's responses say about cameras they cannot see, route by route."""

    def test_no_listing_names_or_identifies_a_hidden_camera_group_rule_or_incident(self):
        from app.incidents import sync_incident
        hidden_alarm = self.add_alarm(self.y1)
        self.add_alarm(self.loose)
        self.add_observation(self.y1)
        self.add_track(self.y1)
        self.add_segment(self.y1)
        self.admin.put(f"/api/cameras/{self.d1}/neighbours", json={"camera_ids": [self.d2, self.y1]})
        with connect() as db:
            sync_incident(db, incident_key="leak:y", camera_id=self.y1, fault_type="recording_failure", summary="Yard 1 is failing", active=True)
            db.execute("UPDATE cameras SET group_id=? WHERE id=?", (self.yard, self.d2))  # Visible through Dock membership, primary group Yard.
            db.execute("INSERT INTO camera_group_memberships(group_id,camera_id) VALUES(?,?)", (self.dock, self.d2))
        self.grant(self.limited_id, [{"scope_type": "all"}])
        layout = {"camera_ids": [self.d1, self.y1], "grid_size": 4, "tile_camera_ids": [self.d1, self.y1, None, None], "layout_mode": "manual"}
        self.limited.put("/api/live/layouts/Mixed", json=layout)
        self.limited.put("/api/live/workspace", json={"camera_ids": [self.d1, self.y1], "grid_size": 4, "tile_camera_ids": [self.d1, self.y1, None, None]})
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.dock}])

        forbidden = [self.y1, self.loose, "Yard 1", "Loose", "Yard", hidden_alarm, "leak:y", "Yard 1 is failing"]
        start, end = (START - timedelta(hours=1)).isoformat(), (START + timedelta(hours=1)).isoformat()
        calls = [("GET", "/api/cameras", {}), ("GET", "/api/camera-groups", {}), (  "GET", f"/api/camera-groups/{self.dock}/members", {}), ("GET", "/api/ai/events", {}),
                 ("GET", "/api/alarms", {"state": "all"}), ("GET", "/api/incidents", {}), ("GET", "/api/exports", {}), ("GET", "/api/live/layouts", {}),
                 ("GET", "/api/live/views", {}), ("GET", "/api/live/workspace", {}), ("GET", "/api/live/alarm-pop-ins", {}), ("GET", "/api/push/preferences", {}),
                 ("GET", f"/api/cameras/{self.d1}/neighbours", {}), ("GET", f"/api/cameras/{self.d1}/segments", {}), ("GET", f"/api/cameras/{self.d1}/coverage", {}),
                 ("GET", "/api/alarms/assignees", {"camera_id": self.d1}), ("GET", "/api/ai/tracks", {"camera_id": self.d1, "start": start, "end": end})]
        for method, path, params in calls:
            response = self.limited.request(method, path, params=params)
            self.assertEqual(response.status_code, 200, f"{path}: {response.text[:200]}")
            for word in forbidden:
                self.assertNotIn(word, response.text, f"{path} mentions {word!r}")
        timeline = self.limited.post("/api/playback/timeline", json={"camera_ids": [self.d1], "start": start, "end": end})
        for word in forbidden:
            self.assertNotIn(word, timeline.text)
        # Dock 2 is visible but its primary group, Yard, is not: the camera shows without Yard's name.
        cameras = {camera["id"]: camera for camera in self.limited.get("/api/cameras").json()["cameras"]}
        self.assertIsNone(cameras[self.d2]["group_name"])

    def test_push_options_hide_groups_and_rules_that_are_not_theirs(self):
        self.add_alarm(self.y1)  # Creates a rule about the Yard camera only.
        self.add_alarm(self.d1)
        options = self.limited.get("/api/push/preferences").json()
        self.assertEqual([group["name"] for group in options["group_options"]], ["Dock"])
        self.assertEqual(len(options["rule_options"]), 1)  # The rule about the Dock camera, not the Yard one.
        self.assertEqual(len(self.admin.get("/api/push/preferences").json()["rule_options"]), 2)

    def test_incidents_and_exports_are_filtered_before_the_page_is_cut(self):
        from app.incidents import sync_incident
        with connect() as db:
            for index in range(230):  # Newer incidents of hidden cameras than the one the operator may see.
                sync_incident(db, incident_key=f"noise:{index}", camera_id=self.y1, fault_type="recording_failure", summary=f"noise {index}", active=True)
            sync_incident(db, incident_key="mine", camera_id=self.d1, fault_type="recording_failure", summary="mine", active=True)
            db.execute("UPDATE incidents SET last_seen_at='2000-01-01T00:00:00+00:00' WHERE incident_key='mine'")  # The oldest of all.
        self.assertEqual([incident["summary"] for incident in self.limited.get("/api/incidents").json()["incidents"]], ["mine"])

    def test_an_alarm_can_only_be_assigned_to_someone_who_can_see_its_camera(self):
        alarm = self.add_alarm(self.d1)
        self.grant(self.full_id, [{"scope_type": "group", "group_id": self.yard}])
        refused = self.limited.post(f"/api/alarms/{alarm}/assign", json={"user_id": self.full_id})
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "That person cannot see this camera"))
        self.assertEqual(self.limited.post(f"/api/alarms/{alarm}/assign", json={"user_id": self.limited_id}).status_code, 200)
        names = {user["username"] for user in self.limited.get("/api/alarms/assignees", params={"camera_id": self.d1}).json()["users"]}
        self.assertEqual(names, {"keeper", "limited"})  # Not the operator who cannot see the Dock.

    def test_an_export_of_a_hidden_camera_fails_exactly_as_one_of_a_missing_camera_even_when_the_queue_is_full(self):
        window = {"start": (START - timedelta(minutes=5)).isoformat(), "end": (START + timedelta(minutes=5)).isoformat(), "output_modes": ["individual"]}
        with mock.patch("app.api.export_jobs.create_export_record", side_effect=OverflowError("The export queue is full")):
            hidden = self.limited.post("/api/exports", json={**window, "camera_ids": [self.y1]})
            missing = self.limited.post("/api/exports", json={**window, "camera_ids": [str(uuid.uuid4())]})
        self.assertEqual((hidden.status_code, hidden.json()), (missing.status_code, missing.json()))
        self.assertEqual(hidden.status_code, 429)

    def test_an_open_stream_ends_when_a_grants_expiry_passes(self):
        import time
        from starlette.websockets import WebSocketDisconnect
        soon = (datetime.now(timezone.utc) + timedelta(seconds=2.5)).isoformat()
        with connect() as db:  # Written directly: the API takes expiries a minute or more ahead in practice, and this one is seconds.
            db.execute("DELETE FROM access_grants WHERE subject=?", (str(self.limited_id),))
            db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,group_id,expires_at) VALUES('user',?,'group',?,?)", (str(self.limited_id), self.dock, soon))
        stream = LiveSocketTests.StubStream()

        def join(key, source, include_audio, viewer, on_fragment=None, video_filter=None, transcode=False, hevc_copy=False):
            viewer.offer(b"fragment")
            return stream

        began = time.monotonic()
        with mock.patch.multiple(self.app.state.live_hub, join=join, leave=lambda stream, viewer: None), \
                self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream") as websocket:
            websocket.receive_json()
            websocket.receive_bytes()
            websocket.receive_bytes()
            self.assertEqual(websocket.receive_json()["type"], "forbidden")
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        self.assertEqual(closed.exception.code, 4403)
        self.assertGreater(time.monotonic() - began, 1.5)  # It ran until the expiry, not before.
        self.assertLess(time.monotonic() - began, 8)


class UpgradeTests(unittest.TestCase):
    def test_upgrading_gives_existing_operators_everything_and_administrators_nothing_to_need(self):
        from app import migrations
        from app.store import initialize

        with tempfile.TemporaryDirectory() as directory:
            previous = {key: os.environ.get(key) for key in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_RECORDINGS_PATH")}
            os.environ.update({"NVR_DB_PATH": str(Path(directory) / "s.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "k"), "NVR_RECORDINGS_PATH": directory})
            try:
                before = [(version, module) for version, module in migrations.MIGRATIONS if version <= 17]
                with mock.patch.object(migrations, "MIGRATIONS", before):
                    initialize()
                with connect() as db:
                    db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'boss','x','administrator'),(2,'op1','x','operator'),(3,'op2','x','operator')")
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','One',x'00')")
                initialize()
                with connect() as db:
                    grants = [(row["subject"], row["scope_type"]) for row in db.execute("SELECT subject,scope_type FROM access_grants ORDER BY subject")]
                    self.assertEqual(grants, [("2", "all"), ("3", "all")])
                    self.assertEqual(access.resolve(db, 2, "operator"), access.EVERYTHING)
                    self.assertEqual(access.resolve(db, 1, "administrator"), access.EVERYTHING)
                    self.assertEqual(db.execute("SELECT provider_roles_json FROM users WHERE id=2").fetchone()[0], "[]")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
