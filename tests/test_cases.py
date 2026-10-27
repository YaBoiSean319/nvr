"""Case files (#129): holds, closing, who may open a case, access grants per clip, notes, and the evidence package that verifies."""
from __future__ import annotations

import hashlib
import io
import json
import unittest
import uuid
import zipfile
from datetime import timedelta
from unittest import mock

from app import cases
from app.recording import recordings_path
from app.storage import recording_file_path
from app.store import connect
from tests.test_access_grants import START, GrantCase


def iso(moment) -> str:
    return moment.isoformat()


def evidence_zip(export_id: str, tamper: bool = False) -> bytes:
    """A stand-in for an export's evidence package: a video, the metadata that names the export and a manifest of both."""
    files = {"metadata.json": json.dumps({"export_id": export_id, "audit_reference": {"resource_type": "export", "resource_id": export_id}}).encode(),
             "camera.mp4": b"footage-of-" + export_id.encode()}
    manifest = {"algorithm": "sha256", "files": [{"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in files.items()]}
    if tamper:
        files["camera.mp4"] = b"changed-footage-" + export_id.encode()[:5]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
        archive.writestr("integrity.json", json.dumps(manifest))
    return buffer.getvalue()


class CaseCase(GrantCase):
    """Dock 1, Dock 2 (group Dock), Yard 1, Loose; an operator limited to Dock; one with every camera; an administrator."""

    def setUp(self):
        super().setUp()
        self.d1_first, self.d1_second = self.add_segment(self.d1, 0), self.add_segment(self.d1, 1)
        self.y1_segment = self.add_segment(self.y1, 0)

    def add_segment(self, camera_id: str, minutes: int = 0) -> int:
        """A recorded interval with its file on disk (a segment whose file has gone cannot be held)."""
        segment = super().add_segment(camera_id, minutes)
        with connect() as db:
            path = recording_file_path(db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment,)).fetchone()[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"footage")
        return segment

    def create(self, client=None, title: str = "Break-in") -> dict:
        response = (client or self.full).post("/api/cases", json={"title": title, "description": "Back door"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add(self, case: str, client=None, **payload):
        return (client or self.full).post(f"/api/cases/{case}/items", json=payload)

    def clip(self, case: str, camera: str, minutes: int = 0, length: int = 1, client=None, label: str = ""):
        start = START + timedelta(minutes=minutes)
        return self.add(case, client, kind="clip", camera_id=camera, start=iso(start), end=iso(start + timedelta(minutes=length)), text=label)

    def holds(self, case: str | None = None, active: bool = True) -> list[dict]:
        with connect() as db:
            sql = "SELECT * FROM recording_holds WHERE " + ("case_id=?" if case else "case_id IS NOT NULL") + (" AND released_at IS NULL" if active else "")
            return [dict(row) for row in db.execute(sql, (case,) if case else ())]

    def case_audit(self, case: str, action: str | None = None) -> list[dict]:
        with connect() as db:
            return [event for event in cases.audit_extract(db, case) if action is None or event["action"] == action]


class HoldTests(CaseCase):
    def test_adding_a_clip_holds_its_footage_and_the_hold_rules_apply(self):
        case = self.create()["id"]
        response = self.clip(case, self.d1, 0, 2, label="Door opens")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual({hold["segment_id"] for hold in self.holds(case)}, {self.d1_first, self.d1_second})
        self.assertEqual(response.json()["case"]["held_segments"], 2)
        self.assertEqual(self.full.get(f"/api/cameras/{self.d1}/segments/{self.d1_first}/hold").json()["held"], True)
        self.assertEqual(self.admin.delete(f"/api/cameras/{self.d1}/segments/{self.d1_first}/hold").status_code, 409)  # Not a hold anyone placed by hand
        self.assertEqual(self.admin.delete(f"/api/cameras/{self.d1}/segments/{self.d1_first}").status_code, 409)  # Held footage is not deleted
        clip = next(item for item in response.json()["case"]["items"] if item["kind"] == "clip")
        self.assertEqual((clip["camera_name"], clip["text"], clip["data"]["segments"]), ("Dock 1", "Door opens", 2))
        self.assertEqual(clip["data"]["uncovered_seconds"], 0)

    def test_footage_whose_file_has_gone_is_not_held_and_cannot_make_a_clip(self):
        with connect() as db:
            path = recording_file_path(db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (self.d1_second,)).fetchone()[0])
        path.unlink()
        case = self.create()["id"]
        added = self.clip(case, self.d1, 0, 2)
        self.assertEqual(added.status_code, 201, added.text)
        self.assertEqual({hold["segment_id"] for hold in self.holds(case)}, {self.d1_first})
        clip = next(item for item in added.json()["case"]["items"] if item["kind"] == "clip")
        self.assertEqual((clip["data"]["segments"], clip["data"]["uncovered_seconds"]), (1, 60.0))  # Half the two minutes is not there
        self.assertEqual(self.clip(case, self.d1, 1, 1).status_code, 422)

    def test_a_clip_with_no_recording_in_range_the_future_or_too_long_is_refused(self):
        case = self.create()["id"]
        self.assertEqual(self.clip(case, self.d1, 30).status_code, 422)  # Nothing recorded then
        self.assertEqual(self.clip(case, self.d2, 0).status_code, 422)
        later = START + timedelta(days=3)
        self.assertEqual(self.add(case, kind="clip", camera_id=self.d1, start=iso(later), end=iso(later + timedelta(minutes=1))).status_code, 422)
        self.assertEqual(self.add(case, kind="clip", camera_id=self.d1, start=iso(START), end=iso(START + timedelta(hours=25))).status_code, 422)
        self.assertEqual(self.add(case, kind="clip", camera_id=self.d1, start=iso(START), end=iso(START)).status_code, 422)
        self.assertEqual(self.add(case, kind="clip", camera_id=self.d1, start="yesterday", end=iso(START)).status_code, 422)
        self.assertEqual(self.holds(case), [])

    def test_closing_needs_confirmation_and_releases_only_this_cases_holds(self):
        case = self.create()["id"]
        other = self.create(title="Other")["id"]
        self.clip(case, self.d1, 0, 2)
        self.clip(other, self.d1, 0, 1)  # Shares the first segment
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by) VALUES(?,?,?,1)", (str(uuid.uuid4()), self.d1_second, self.d1))  # By hand
        refused = self.full.post(f"/api/cases/{case}/close", json={})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("2 recorded intervals", refused.json()["detail"])
        self.assertEqual(len(self.holds(case)), 2)
        closed = self.full.post(f"/api/cases/{case}/close", json={"confirm": True})
        self.assertEqual((closed.status_code, closed.json()["status"], closed.json()["held_segments"]), (200, "closed", 0))
        self.assertEqual(self.holds(case), [])
        self.assertEqual(len(self.holds(other)), 1)  # The other case still holds the shared segment
        self.assertEqual(self.full.get(f"/api/cameras/{self.d1}/segments/{self.d1_second}/hold").json()["held"], True)  # The manual hold stays
        self.assertEqual(self.case_audit(case, "closed")[0]["detail"], {"holds_released": 2})
        self.assertEqual(self.clip(case, self.d1, 0).status_code, 409)  # A closed case takes nothing more
        self.assertEqual(self.full.post(f"/api/cases/{case}/close", json={"confirm": True}).status_code, 409)

    def test_removing_a_clip_releases_the_holds_no_other_clip_needs(self):
        case = self.create()["id"]
        wide = self.clip(case, self.d1, 0, 2).json()["item_id"]
        narrow = self.clip(case, self.d1, 0, 1).json()["item_id"]
        self.assertEqual(len(self.holds(case)), 2)
        self.assertEqual(self.full.delete(f"/api/cases/{case}/items/{wide}").status_code, 200)
        self.assertEqual({hold["segment_id"] for hold in self.holds(case)}, {self.d1_first})  # The narrow clip still needs the first
        self.assertEqual(self.full.delete(f"/api/cases/{case}/items/{narrow}").status_code, 200)
        self.assertEqual(self.holds(case), [])
        self.assertEqual(self.full.delete(f"/api/cases/{case}/items/{narrow}").status_code, 404)

    def test_only_the_owner_or_an_administrator_closes_a_case(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        self.assertEqual(self.limited.post(f"/api/cases/{case}/close", json={"confirm": True}).status_code, 403)
        self.assertEqual(self.admin.post(f"/api/cases/{case}/close", json={"confirm": True}).status_code, 200)


class AccessTests(CaseCase):
    def test_only_members_and_administrators_can_open_a_case_and_others_cannot_tell_it_exists(self):
        case = self.create()["id"]
        self.assertEqual(self.full.get(f"/api/cases/{case}").status_code, 200)
        self.assertEqual(self.admin.get(f"/api/cases/{case}").status_code, 200)
        outsider, missing = self.limited.get(f"/api/cases/{case}"), self.limited.get(f"/api/cases/{uuid.uuid4()}")
        self.assertEqual((outsider.status_code, outsider.json()), (404, missing.json()))
        for method, path, body in (("get", "/audit", None), ("post", "/items", {"kind": "note", "text": "x"}), ("post", "/close", {"confirm": True}), ("post", "/packages", None),
                                   ("patch", "", {"title": "x"}), ("post", "/members", {"username": "full"})):
            response = getattr(self.limited, method)(f"/api/cases/{case}{path}", **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.limited.get("/api/cases").json()["cases"], [])
        self.assertEqual([item["id"] for item in self.full.get("/api/cases").json()["cases"]], [case])
        self.assertEqual([item["id"] for item in self.admin.get("/api/cases").json()["cases"]], [case])

    def test_a_refused_try_is_audited_for_a_case_that_exists_and_not_for_one_that_does_not(self):
        case = self.create()["id"]
        self.limited.get(f"/api/cases/{case}")
        self.limited.get(f"/api/cases/{uuid.uuid4()}")
        denied = self.audit("case_access_denied")
        self.assertEqual([(entry["resource_id"], entry["actor_id"]) for entry in denied], [(case, self.limited_id)])

    def test_opening_a_case_is_audited_once_for_each_stretch_of_use(self):
        case = self.create()["id"]
        for _ in range(3):
            self.full.get(f"/api/cases/{case}")
        self.admin.get(f"/api/cases/{case}")
        opened = self.case_audit(case, "opened")
        self.assertEqual([event["actor"] for event in opened], ["full", "keeper"])
        with connect() as db:
            db.execute("UPDATE case_audit SET created_at=? WHERE action='opened' AND actor_id=?", (iso(START), self.full_id))
        self.full.get(f"/api/cases/{case}")
        self.assertEqual(len(self.case_audit(case, "opened")), 3)

    def test_members_are_managed_by_the_owner_and_administrators(self):
        case = self.create()["id"]
        self.assertEqual(self.full.post(f"/api/cases/{case}/members", json={"username": "limited"}).status_code, 201)
        self.assertEqual(self.limited.get(f"/api/cases/{case}").status_code, 200)
        self.assertEqual(self.limited.post(f"/api/cases/{case}/members", json={"username": "full"}).status_code, 403)  # A member is not a manager
        self.assertEqual(self.full.post(f"/api/cases/{case}/members", json={"username": "limited"}).status_code, 409)
        self.assertEqual(self.full.post(f"/api/cases/{case}/members", json={"username": "nobody"}).status_code, 404)
        self.assertEqual(self.full.delete(f"/api/cases/{case}/members/{self.full_id}").status_code, 409)  # The owner stays
        self.assertEqual(self.full.delete(f"/api/cases/{case}/members/{self.limited_id}").status_code, 200)
        self.assertEqual(self.limited.get(f"/api/cases/{case}").status_code, 404)
        self.assertEqual([event["action"] for event in self.case_audit(case) if event["action"].startswith("member")], ["member_added", "member_removed"])

    def test_a_clip_on_a_camera_a_person_may_not_see_cannot_be_added_and_is_shown_to_them_only_as_present(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        self.assertEqual(self.clip(case, self.y1, client=self.limited).status_code, 404)  # As missing as a camera that does not exist
        self.assertEqual(self.clip(case, str(uuid.uuid4()), client=self.limited).status_code, 404)
        self.assertEqual(self.clip(case, self.d1, client=self.limited).status_code, 201)
        self.assertEqual(self.clip(case, self.y1, client=self.full, label="Yard gate").status_code, 201)
        seen = self.limited.get(f"/api/cases/{case}").json()
        self.assertEqual(seen["restricted_items"], 1)
        hidden = next(item for item in seen["items"] if item["restricted"])
        self.assertEqual(set(hidden), {"id", "kind", "restricted", "added_at"})
        self.assertNotIn("Yard", json.dumps(seen))
        self.assertEqual(self.limited.delete(f"/api/cases/{case}/items/{hidden['id']}").status_code, 404)
        full = self.full.get(f"/api/cases/{case}").json()
        self.assertEqual({item["camera_name"] for item in full["items"] if item["kind"] == "clip"}, {"Dock 1", "Yard 1"})

    def test_what_the_audit_and_the_counts_say_about_a_hidden_camera_is_left_out(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        self.clip(case, self.y1, 0, 1, label="Yard gate")
        self.clip(case, self.d1, 0, 1)
        seen = self.limited.get(f"/api/cases/{case}/audit").json()["events"]
        self.assertNotIn("Yard", json.dumps(seen))
        self.assertNotIn(self.y1, json.dumps(seen))
        restricted = [event for event in seen if event["detail"] == {"restricted": True}]
        self.assertEqual([event["action"] for event in restricted], ["clip_added"])
        self.assertEqual(next(event for event in seen if event["action"] == "clip_added" and event["detail"].get("camera") == "Dock 1")["detail"]["camera"], "Dock 1")
        everything = self.full.get(f"/api/cases/{case}/audit").json()["events"]
        self.assertIn("Yard 1", json.dumps(everything))
        self.assertEqual(self.limited.get(f"/api/cases/{case}").json()["counts"]["clip"], 1)
        self.assertEqual(self.limited.get("/api/cases").json()["cases"][0]["counts"]["clip"], 1)
        self.assertEqual(self.full.get("/api/cases").json()["cases"][0]["counts"]["clip"], 2)

    def test_a_saved_search_shows_each_member_only_the_results_on_their_cameras(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        dock, yard = self.add_track(self.d1), self.add_track(self.y1)
        self.add(case, kind="search", text="People", params={}, track_ids=[dock, yard])
        shown = next(item for item in self.limited.get(f"/api/cases/{case}").json()["items"] if item["kind"] == "search")
        self.assertEqual(shown["data"]["track_ids"], [dock])
        full = next(item for item in self.full.get(f"/api/cases/{case}").json()["items"] if item["kind"] == "search")
        self.assertEqual(full["data"]["track_ids"], [dock, yard])

    def test_a_refused_try_is_audited_on_the_routes_that_change_a_case_too(self):
        case = self.create()["id"]
        self.limited.post(f"/api/cases/{case}/items", json={"kind": "note", "text": "x"})
        self.limited.post(f"/api/cases/{case}/packages")
        self.limited.post(f"/api/cases/{case}/close", json={"confirm": True})
        self.assertEqual(len(self.audit("case_access_denied")), 3)
        self.assertEqual(self.full.get(f"/api/cases/{case}").json()["status"], "open")

    def test_only_the_person_who_added_an_item_the_owner_or_an_administrator_removes_it(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        theirs = self.clip(case, self.d1, 0, 1, client=self.limited).json()["item_id"]
        owners = self.clip(case, self.d1, 1, 1).json()["item_id"]
        self.assertEqual(self.limited.delete(f"/api/cases/{case}/items/{owners}").status_code, 403)
        self.assertEqual(self.limited.delete(f"/api/cases/{case}/items/{theirs}").status_code, 200)
        again = self.clip(case, self.d1, 0, 1, client=self.limited).json()["item_id"]
        self.assertEqual(self.full.delete(f"/api/cases/{case}/items/{again}").status_code, 200)  # The owner may remove anyone's

    def test_a_person_owns_a_limited_number_of_open_cases(self):
        for index in range(cases.MAX_OPEN_CASES):
            self.create(title=f"Case {index}")
        refused = self.full.post("/api/cases", json={"title": "One too many"})
        self.assertEqual(refused.status_code, 409)
        first = self.full.get("/api/cases").json()["cases"][0]["id"]
        self.full.post(f"/api/cases/{first}/close", json={"confirm": True})
        self.assertEqual(self.full.post("/api/cases", json={"title": "Room again"}).status_code, 201)

    def test_grants_that_end_hide_the_clips_again(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        self.clip(case, self.d1)
        self.assertEqual(self.limited.get(f"/api/cases/{case}").json()["restricted_items"], 0)
        self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])
        again = self.limited.get(f"/api/cases/{case}").json()
        self.assertEqual(again["restricted_items"], 1)


class ItemTests(CaseCase):
    def test_notes_are_only_ever_added_and_marks_alarms_searches_and_exports_are_kept(self):
        case = self.create()["id"]
        note = self.add(case, kind="note", text="Door forced at 02:10").json()["item_id"]
        self.assertEqual(self.full.delete(f"/api/cases/{case}/items/{note}").status_code, 409)
        self.assertEqual(self.add(case, kind="note", text="   ").status_code, 422)
        mark = self.add(case, kind="mark", camera_id=self.d1, at=iso(START), text="Figure at the door")
        self.assertEqual(mark.status_code, 201, mark.text)
        alarm = self.add_alarm(self.d1)
        self.assertEqual(self.add(case, kind="alarm", alarm_id=alarm).status_code, 201)
        self.assertEqual(self.add(case, kind="alarm", alarm_id=str(uuid.uuid4())).status_code, 404)
        track_dock, track_yard = self.add_track(self.d1), self.add_track(self.y1)
        search = self.add(case, kind="search", text="Red tops", params={"upper_colour": "red"}, track_ids=[track_dock, track_yard, "missing"])
        self.assertEqual(search.status_code, 201, search.text)
        detail = self.full.get(f"/api/cases/{case}").json()
        self.assertEqual(detail["counts"], {"clip": 0, "mark": 1, "note": 1, "alarm": 1, "search": 1, "export": 0})
        saved = next(item for item in detail["items"] if item["kind"] == "search")
        self.assertEqual((saved["data"]["params"], saved["data"]["track_ids"]), ({"upper_colour": "red"}, [track_dock, track_yard]))
        export = self.full.post("/api/exports", json={"camera_ids": [self.d1], "start": iso(START), "end": iso(START + timedelta(minutes=1)), "output_modes": ["individual"]})
        if export.status_code == 202:
            self.assertEqual(self.add(case, kind="export", export_id=export.json()["id"]).status_code, 201)
        self.assertEqual(self.add(case, kind="export", export_id=str(uuid.uuid4())).status_code, 404)

    def test_marks_alarms_searches_and_exports_respect_the_cameras_a_person_may_see(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        self.assertEqual(self.add(case, client=self.limited, kind="mark", camera_id=self.y1, at=iso(START), text="x").status_code, 404)
        self.assertEqual(self.add(case, client=self.limited, kind="alarm", alarm_id=self.add_alarm(self.y1)).status_code, 404)
        track_dock, track_yard = self.add_track(self.d1), self.add_track(self.y1)
        saved = self.add(case, client=self.limited, kind="search", text="People", params={}, track_ids=[track_dock, track_yard]).json()["case"]
        self.assertEqual(next(item for item in saved["items"] if item["kind"] == "search")["data"]["track_ids"], [track_dock])
        yard_export = self.full.post("/api/exports", json={"camera_ids": [self.y1], "start": iso(START), "end": iso(START + timedelta(minutes=1)), "output_modes": ["individual"]})
        if yard_export.status_code == 202:
            self.assertEqual(self.add(case, client=self.limited, kind="export", export_id=yard_export.json()["id"]).status_code, 404)

    def test_opening_a_clip_from_the_case_is_recorded_for_the_case_once_for_each_stretch_of_use(self):
        case = self.create()["id"]
        self.full.post(f"/api/cases/{case}/members", json={"username": "limited"})
        clip = self.clip(case, self.d1, 0, 1).json()["item_id"]
        yard = self.clip(case, self.y1, 0, 1).json()["item_id"]
        for _ in range(3):
            self.assertEqual(self.limited.post(f"/api/cases/{case}/items/{clip}/viewed").status_code, 204)
        self.assertEqual(self.full.post(f"/api/cases/{case}/items/{clip}/viewed").status_code, 204)
        viewed = self.case_audit(case, "item_viewed")
        self.assertEqual([(event["actor"], event["detail"]["camera"], event["detail"]["kind"]) for event in viewed], [("limited", "Dock 1", "clip"), ("full", "Dock 1", "clip")])
        self.assertEqual(self.limited.post(f"/api/cases/{case}/items/{yard}/viewed").status_code, 404)  # A camera they may not see
        self.assertEqual(self.limited.post(f"/api/cases/{case}/items/{uuid.uuid4()}/viewed").status_code, 404)
        outsider = self.add_operator("outsider")
        self.assertEqual(self.sign_in("outsider").post(f"/api/cases/{case}/items/{clip}/viewed").status_code, 404)
        self.assertTrue(outsider)

    def test_a_case_audit_names_who_did_what(self):
        case = self.create()["id"]
        self.clip(case, self.d1, 0, 1)
        self.add(case, kind="note", text="A note")
        events = self.full.get(f"/api/cases/{case}/audit").json()["events"]
        self.assertEqual([event["action"] for event in events], ["created", "clip_added", "note_added"])
        self.assertEqual({event["actor"] for event in events}, {"full"})
        self.assertEqual(events[1]["detail"]["camera"], "Dock 1")

    def test_a_cases_title_and_description_change_and_titles_are_checked(self):
        case = self.create()["id"]
        self.assertEqual(self.full.patch(f"/api/cases/{case}", json={"title": "Warehouse break-in"}).json()["title"], "Warehouse break-in")
        self.assertEqual(self.full.post("/api/cases", json={"title": ""}).status_code, 422)
        self.assertEqual(self.full.post("/api/cases", json={"title": "x" * 201}).status_code, 422)
        self.assertEqual(self.full.patch(f"/api/cases/{case}", json={"title": "  "}).status_code, 422)


class PackageTests(CaseCase):
    def package(self, case: str, client=None) -> str:
        response = (client or self.full).post(f"/api/cases/{case}/packages")
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def finish_exports(self, tamper: str | None = None) -> None:
        """Run the exporter's part by hand: every queued export of the packages completes with a package file."""
        root = recordings_path()
        (root / "exports").mkdir(exist_ok=True)
        with connect() as db:
            for row in db.execute("SELECT id FROM exports WHERE status='queued'").fetchall():
                (root / "exports" / f"{row['id']}.zip").write_bytes(evidence_zip(row["id"], tamper == row["id"]))
                db.execute("UPDATE exports SET status='completed',output_relative_path=? WHERE id=?", (f"exports/{row['id']}.zip", row["id"]))

    def build(self, case: str, client=None) -> tuple[str, bytes]:
        package = self.package(case, client)
        cases.advance_packages()
        self.finish_exports()
        cases.advance_packages()
        status = (client or self.full).get(f"/api/cases/{case}/packages/{package}").json()
        self.assertEqual(status["status"], "ready", status)
        download = (client or self.full).get(f"/api/cases/{case}/packages/{package}/download")
        self.assertEqual(download.status_code, 200)
        return package, download.content

    def verify(self, case: str, package: str, content: bytes, client=None) -> dict:
        response = (client or self.full).post(f"/api/cases/{case}/packages/{package}/verify", content=content)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def setUp(self):
        super().setUp()
        self.case = self.create()["id"]
        self.clip(self.case, self.d1, 0, 1, label="Door")
        self.clip(self.case, self.d1, 1, 1, label="Corridor")
        self.add(self.case, kind="note", text="Door forced at 02:10")

    def test_the_package_holds_every_clip_the_notes_and_the_audit_and_verifies(self):
        package, content = self.build(self.case)
        names = set(zipfile.ZipFile(io.BytesIO(content)).namelist())
        self.assertTrue({"case.json", "notes.json", "notes.md", "audit.json", "metadata.json", "integrity.json"} <= names)
        self.assertEqual(len([name for name in names if name.startswith("clips/")]), 2)
        archive = zipfile.ZipFile(io.BytesIO(content))
        self.assertIn("Door forced at 02:10", archive.read("notes.md").decode())
        self.assertEqual([entry["text"] for entry in json.loads(archive.read("notes.json"))], ["Door forced at 02:10"])
        self.assertIn("clip_added", {event["action"] for event in json.loads(archive.read("audit.json"))})
        result = self.verify(self.case, package, content)
        self.assertEqual((result["checksums_valid"], result["clips_valid"], result["complete"], result["omitted_items"]), (True, True, True, 0))
        self.assertEqual([clip["valid"] for clip in result["clips"]], [True, True])
        self.assertTrue(all(item["matches"] for item in result["files"]))
        self.assertEqual(self.case_audit(self.case, "package_verified")[0]["detail"]["complete"], True)

    def test_a_changed_clip_or_file_or_a_missing_clip_fails_verification(self):
        package, content = self.build(self.case)
        clips = [name for name in zipfile.ZipFile(io.BytesIO(content)).namelist() if name.startswith("clips/")]

        def rebuilt(change) -> bytes:
            source, output = zipfile.ZipFile(io.BytesIO(content)), io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                for name in source.namelist():
                    body = change(name, source.read(name))
                    if body is not None:
                        archive.writestr(name, body)
            return output.getvalue()

        changed = self.verify(self.case, package, rebuilt(lambda name, body: body + b"x" if name == clips[0] else body))
        self.assertEqual((changed["checksums_valid"], changed["complete"]), (False, False))
        notes_edited = self.verify(self.case, package, rebuilt(lambda name, body: b"[]" if name == "notes.json" else body))
        self.assertEqual((notes_edited["checksums_valid"], notes_edited["complete"]), (False, False))
        dropped = self.verify(self.case, package, rebuilt(lambda name, body: None if name == clips[1] else body))
        self.assertFalse(dropped["checksums_valid"])  # The manifest names it
        # A manifest rewritten to match a dropped clip still shows the clip missing, because case.json names it
        def drop_and_reseal(name, body):
            if name == clips[1]:
                return None
            if name == "integrity.json":
                manifest = json.loads(body)
                manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != clips[1]]
                return json.dumps(manifest)
            return body
        resealed = self.verify(self.case, package, rebuilt(drop_and_reseal))
        self.assertEqual((resealed["checksums_valid"], resealed["clips_valid"], resealed["complete"]), (True, False, False))
        self.assertEqual([clip["present"] for clip in resealed["clips"]], [True, False])
        self.assertFalse(self.verify(self.case, package, b"not a zip")["checksums_valid"])

    def test_a_package_says_which_clips_lost_footage_since_they_were_added(self):
        with connect() as db:
            path = recording_file_path(db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (self.d1_second,)).fetchone()[0])
        # The second clip's footage is held, so it cannot be deleted by any rule; a disk fault can still lose the file
        path.unlink()
        package, content = self.build(self.case)
        result = self.verify(self.case, package, content)
        self.assertEqual((result["checksums_valid"], result["clips_valid"]), (True, True))
        document = json.loads(zipfile.ZipFile(io.BytesIO(content)).read("case.json"))
        coverage = {entry["text"]: entry["coverage"] for entry in document["items"] if entry["kind"] == "clip"}
        self.assertEqual(coverage["Door"]["uncovered_seconds"], 0)
        self.assertEqual(coverage["Corridor"]["uncovered_seconds"], 60.0)
        self.assertEqual(len(result["partial_clips"]), 1)

    def test_a_package_rewritten_to_match_itself_is_not_the_package_the_nvr_built(self):
        package, content = self.build(self.case)
        untouched = self.verify(self.case, package, content)
        self.assertEqual((untouched["matches_issued_package"], untouched["clips_match_request"], untouched["verified"]), (True, True, True))
        source = zipfile.ZipFile(io.BytesIO(content))
        clips = [name for name in source.namelist() if name.startswith("clips/")]
        # A clip dropped, with case.json and the manifest rewritten to agree: the package is consistent, and is not what was built
        document = json.loads(source.read("case.json"))
        document["items"] = [entry for entry in document["items"] if entry.get("package_file") != clips[1]]
        output = io.BytesIO()
        files = {name: source.read(name) for name in source.namelist() if name not in (clips[1], "integrity.json")}
        files["case.json"] = json.dumps(document).encode()
        manifest = {"algorithm": "sha256", "files": [{"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in files.items()]}
        with zipfile.ZipFile(output, "w") as archive:
            for name, body in files.items():
                archive.writestr(name, body)
            archive.writestr("integrity.json", json.dumps(manifest))
        forged = self.verify(self.case, package, output.getvalue())
        self.assertEqual((forged["checksums_valid"], forged["clips_valid"]), (True, True))  # Consistent with itself ...
        self.assertEqual((forged["matches_issued_package"], forged["clips_match_request"], forged["verified"], forged["complete"]), (False, False, False, False))  # ... and caught
        self.assertEqual(self.case_audit(self.case, "package_verification_failed")[0]["detail"]["matches_issued_package"], False)

    def test_a_clip_package_that_lost_the_signed_chain_it_was_exported_with_fails(self):
        package = self.package(self.case)
        cases.advance_packages()
        with connect() as db:
            for row in db.execute("SELECT export_id FROM case_package_clips WHERE package_id=?", (package,)).fetchall():
                db.execute("UPDATE exports SET metadata_json=? WHERE id=?", (json.dumps({"signed_footage": {self.d1: "signed"}}), row[0]))
        self.finish_exports()
        cases.advance_packages()
        download = self.full.get(f"/api/cases/{self.case}/packages/{package}/download").content
        result = self.verify(self.case, package, download)
        self.assertEqual((result["checksums_valid"], result["clips_valid"], result["verified"]), (True, False, False))  # The stand-in packages carry no chain
        self.assertIn("signed", result["clips"][0]["error"])

    def test_the_package_is_of_the_clips_it_was_requested_for_whatever_changes_after(self):
        package = self.package(self.case)
        self.clip(self.case, self.d1, 0, 1, label="Added after")  # After the request
        cases.advance_packages()
        self.finish_exports()
        cases.advance_packages()
        self.assertEqual(self.full.get(f"/api/cases/{self.case}/packages/{package}").json()["status"], "ready")
        content = self.full.get(f"/api/cases/{self.case}/packages/{package}/download").content
        document = json.loads(zipfile.ZipFile(io.BytesIO(content)).read("case.json"))
        self.assertEqual(sorted(entry["text"] for entry in document["items"] if entry["kind"] == "clip"), ["Corridor", "Door"])
        self.assertTrue(self.verify(self.case, package, content)["verified"])

    def test_a_package_that_never_finishes_is_failed_and_the_number_kept_is_limited_and_a_package_can_be_deleted(self):
        package = self.package(self.case)
        with connect() as db:
            db.execute("UPDATE case_packages SET created_at=? WHERE id=?", (iso(START - timedelta(days=1)), package))
        cases.advance_packages()
        failed = self.full.get(f"/api/cases/{self.case}/packages/{package}").json()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("six hours", failed["error"])
        for _ in range(cases.MAX_PACKAGES - 1):
            with connect() as db:
                db.execute("INSERT INTO case_packages(id,case_id,requested_by,status,created_at) VALUES(?,?,?, 'failed', ?)", (str(uuid.uuid4()), self.case, self.full_id, iso(START)))
        self.assertEqual(self.full.post(f"/api/cases/{self.case}/packages").status_code, 409)
        self.assertEqual(self.full.delete(f"/api/cases/{self.case}/packages/{package}").status_code, 200)
        self.assertEqual(self.full.post(f"/api/cases/{self.case}/packages").status_code, 202)
        self.assertEqual(self.limited.delete(f"/api/cases/{self.case}/packages/{package}").status_code, 404)

    def test_a_package_that_is_not_for_this_case_fails_verification(self):
        package, content = self.build(self.case)
        second = self.create(title="Another")["id"]
        self.clip(second, self.d1, 0, 1)
        second_package = self.package(second)
        result = self.verify(second, second_package, content)  # This case's package offered for the other
        self.assertEqual(result["checksums_valid"], False)
        self.assertIn("identity", result["error"])

    def test_the_package_is_built_from_what_the_person_may_see_and_says_what_was_left_out(self):
        self.full.post(f"/api/cases/{self.case}/members", json={"username": "limited"})
        self.clip(self.case, self.y1, 0, 1)
        package, content = self.build(self.case, self.limited)
        result = self.verify(self.case, package, content, self.limited)
        self.assertEqual((result["checksums_valid"], result["clips_valid"], result["omitted_items"], result["complete"]), (True, True, 1, False))
        self.assertEqual(len(result["clips"]), 2)
        self.assertNotIn("Yard", zipfile.ZipFile(io.BytesIO(content)).read("case.json").decode())
        full_package, full_content = self.build(self.case, self.full)
        self.assertEqual(len(self.verify(self.case, full_package, full_content)["clips"]), 3)
        # Built with one person's access, a package is not another member's to download (an administrator may)
        self.assertEqual(self.full.get(f"/api/cases/{self.case}/packages/{package}/download").status_code, 404)
        self.assertEqual(self.admin.get(f"/api/cases/{self.case}/packages/{package}/download").status_code, 200)

    def test_a_person_with_no_clips_they_may_see_cannot_ask_for_a_package(self):
        only_yard = self.create(title="Yard")["id"]
        self.clip(only_yard, self.y1)
        self.full.post(f"/api/cases/{only_yard}/members", json={"username": "limited"})
        self.assertEqual(self.limited.post(f"/api/cases/{only_yard}/packages").status_code, 422)
        self.assertEqual(self.limited.post("/api/cases/" + str(uuid.uuid4()) + "/packages").status_code, 404)

    def test_a_full_export_queue_waits_and_a_failed_export_fails_the_package(self):
        package = self.package(self.case)
        with mock.patch("app.exports.create_export_record", side_effect=OverflowError("full")):
            cases.advance_packages()
        self.assertEqual(self.full.get(f"/api/cases/{self.case}/packages/{package}").json()["status"], "building")
        self.assertEqual(self.package_exports(package), [None, None])
        cases.advance_packages()
        self.assertTrue(all(self.package_exports(package)))
        with connect() as db:
            db.execute("UPDATE exports SET status='failed',error='disk full' WHERE status='queued' AND id=?", (self.package_exports(package)[0],))
        cases.advance_packages()
        failed = self.full.get(f"/api/cases/{self.case}/packages/{package}").json()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("disk full", failed["error"])
        self.assertEqual(self.full.get(f"/api/cases/{self.case}/packages/{package}/download").status_code, 409)

    def package_exports(self, package: str) -> list[str | None]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT export_id FROM case_package_clips WHERE package_id=? ORDER BY rowid", (package,))]

    def test_only_one_package_is_built_at_a_time_and_downloads_and_verifications_are_audited(self):
        self.package(self.case)
        self.assertEqual(self.full.post(f"/api/cases/{self.case}/packages").status_code, 409)
        cases.advance_packages()
        self.finish_exports()
        cases.advance_packages()
        package = self.full.get(f"/api/cases/{self.case}").json()["packages"][0]["id"]
        self.full.get(f"/api/cases/{self.case}/packages/{package}/download")
        self.assertEqual(self.case_audit(self.case, "package_downloaded")[0]["actor"], "full")
        with connect() as db:
            viewed = [row["kind"] for row in db.execute("SELECT kind FROM viewing_audit WHERE reference=?", (package,))]
        self.assertEqual(viewed, ["download"])
        self.assertEqual(self.full.get(f"/api/cases/{self.case}/packages/{uuid.uuid4()}").status_code, 404)
        self.assertEqual(self.limited.get(f"/api/cases/{self.case}/packages/{package}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
