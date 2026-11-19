"""The alarm list stays fast with a large history, and resolved alarms and rule events are kept for a set period (#349).

The owner's decision: resolved alarms and rule events are kept 90 days by default, as a setting; alarms nobody resolves are closed after
30 days, except those in a case or over held footage."""
from __future__ import annotations

import json
import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import alarms
from app.store import connect
from tests.test_access_grants import GrantCase

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


class AlarmCase(GrantCase):
    def alarm(self, *, days: float, state: str = "new", resolved_days: float | None = None, camera: str | None = None, rule_event: str | None = None) -> str:
        alarm_id = str(uuid.uuid4())
        when = ago(days)
        with connect() as db:
            db.execute(
                "INSERT INTO alarms(id,rule_event_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,"
                "resolved_at,created_at,updated_at) VALUES(?,?,'Gate',?,'t','person','zone_entry',?,'{}',?,?,?,?,?,?)",
                (alarm_id, rule_event, camera or self.d1, when.isoformat(), (when - timedelta(seconds=10)).isoformat(), (when + timedelta(seconds=20)).isoformat(),
                 state, alarms._stamp(ago(resolved_days)) if resolved_days is not None else None, when.isoformat(), when.isoformat()))
        return alarm_id

    def rule_event(self, *, days: float) -> str:
        event_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rule_events(id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,'Gate',?,?,'person','zone_entry',?,'{}')",
                       (event_id, self.d1, event_id, ago(days).isoformat()))
        return event_id

    def exists(self, table: str, row_id: str) -> bool:
        with connect() as db:
            return db.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone() is not None

    def state(self, alarm_id: str) -> str:
        with connect() as db:
            return db.execute("SELECT state FROM alarms WHERE id=?", (alarm_id,)).fetchone()[0]


class HistoryTests(AlarmCase):
    def test_resolved_alarms_and_rule_events_are_kept_ninety_days_by_default(self):
        old, recent = self.alarm(days=120, state="resolved", resolved_days=91), self.alarm(days=100, state="resolved", resolved_days=89)
        old_event, recent_event = self.rule_event(days=91), self.rule_event(days=89)
        self.assertEqual(alarms.prune_history(NOW), {"closed": 0, "removed_alarms": 1, "removed_rule_events": 1})
        self.assertEqual((self.exists("alarms", old), self.exists("alarms", recent)), (False, True))
        self.assertEqual((self.exists("rule_events", old_event), self.exists("rule_events", recent_event)), (False, True))

    def test_the_period_is_a_setting(self):
        self.assertEqual(self.admin.get("/api/alarm-settings").json(), {"resolved_footage_days": 30, "history_days": 90})
        saved = self.admin.put("/api/alarm-settings", json={"history_days": 30})
        self.assertEqual(saved.json(), {"resolved_footage_days": 30, "history_days": 30})
        for bad in ({"history_days": 6}, {"history_days": 3651}, {}):
            self.assertEqual(self.admin.put("/api/alarm-settings", json=bad).status_code, 422, bad)
        self.assertEqual(self.limited.put("/api/alarm-settings", json={"history_days": 365}).status_code, 403)
        with connect() as db:
            audited = json.loads(db.execute("SELECT details_json FROM audit_log WHERE action='alarm_settings_updated' ORDER BY id DESC").fetchone()[0])
        self.assertEqual(audited, {"history_days": 30, "before": {"history_days": 90}})
        gone = self.alarm(days=40, state="resolved", resolved_days=31)
        alarms.prune_history(NOW)
        self.assertFalse(self.exists("alarms", gone))

    def test_an_alarm_is_kept_as_long_as_it_keeps_its_footage(self):
        self.admin.put("/api/alarm-settings", json={"resolved_footage_days": 120})
        kept = self.alarm(days=150, state="resolved", resolved_days=100)
        alarms.prune_history(NOW)
        self.assertTrue(self.exists("alarms", kept))

    def test_a_rule_event_an_alarm_still_points_to_is_kept(self):
        event = self.rule_event(days=200)
        self.alarm(days=200, state="new", rule_event=event)
        with connect() as db:  # In a case, so it is neither closed nor removed
            db.execute("INSERT INTO cases(id,title,created_at) VALUES('k','Theft',?)", (NOW.isoformat(),))
            alarm_id = db.execute("SELECT id FROM alarms WHERE rule_event_id=?", (event,)).fetchone()[0]
            db.execute("INSERT INTO case_items(id,case_id,kind,ref,added_at) VALUES('i','k','alarm',?,?)", (alarm_id, NOW.isoformat()))
        alarms.prune_history(NOW)
        self.assertTrue(self.exists("rule_events", event))

    def test_an_alarm_nobody_handled_is_closed_after_thirty_days_and_said_so(self):
        stale, fresh, worked = self.alarm(days=31), self.alarm(days=29), self.alarm(days=40, state="assigned")
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,enabled,created_at,updated_at) "
                       "VALUES('w','Receiver',X'00',X'00','[\"alarm.resolved\"]',1,?,?)", (NOW.isoformat(), NOW.isoformat()))
        self.assertEqual(alarms.prune_history(NOW)["closed"], 2)
        self.assertEqual((self.state(stale), self.state(fresh), self.state(worked)), ("resolved", "new", "resolved"))
        with connect() as db:
            history = db.execute("SELECT action,actor_id FROM alarm_history WHERE alarm_id=?", (stale,)).fetchall()
            sent = [json.loads(row[0])["alarm_id"] for row in db.execute("SELECT payload_json FROM webhook_deliveries WHERE event_type='alarm.resolved'")]
        self.assertEqual([tuple(row) for row in history], [("auto_resolved", None)])
        self.assertEqual(sorted(sent), sorted([stale, worked]))
        listed = self.admin.get(f"/api/alarms/{stale}").json()
        self.assertEqual((listed["state"], listed["resolved_by"]), ("resolved", None))

    def test_alarms_in_a_case_or_over_held_footage_are_left_alone(self):
        in_case, held = self.alarm(days=200), self.alarm(days=200, camera=self.d2)
        resolved_in_case = self.alarm(days=300, state="resolved", resolved_days=250)
        with connect() as db:
            db.execute("INSERT INTO cases(id,title,created_at) VALUES('k','Theft',?)", (NOW.isoformat(),))
            for number, alarm_id in enumerate((in_case, resolved_in_case)):
                db.execute("INSERT INTO case_items(id,case_id,kind,ref,added_at) VALUES(?,'k','alarm',?,?)", (f"i{number}", alarm_id, NOW.isoformat()))
            when = ago(200)
            segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,100)",
                                 (self.d2, "d2/held.mp4", (when - timedelta(minutes=1)).isoformat(), (when + timedelta(minutes=1)).isoformat())).lastrowid
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) VALUES(?,?,?)", (str(uuid.uuid4()), segment, self.d2))
        self.assertEqual(alarms.prune_history(NOW), {"closed": 0, "removed_alarms": 0, "removed_rule_events": 0})
        self.assertEqual((self.state(in_case), self.state(held)), ("new", "new"))
        self.assertTrue(self.exists("alarms", resolved_in_case))

    def test_with_nothing_due_the_pass_takes_no_write_lock(self):
        self.alarm(days=1)
        other = sqlite3.connect(str(__import__("app.store", fromlist=["database_path"]).database_path()), timeout=0, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")  # Someone else holds the write lock: a pass that wanted it would fail at once
        try:
            with patch("app.store._Connection", sqlite3.Connection):
                self.assertEqual(alarms.prune_history(NOW), {"closed": 0, "removed_alarms": 0, "removed_rule_events": 0})
        finally:
            other.execute("ROLLBACK")
            other.close()


class PollCostTests(AlarmCase):
    def test_one_poll_makes_the_same_few_queries_however_many_alarms_there_are(self):
        with connect() as db:
            rows = [(str(uuid.uuid4()), self.d1 if number % 2 else self.d2, ago(number / 100).isoformat(), ("new", "acknowledged", "assigned", "resolved")[number % 4])
                    for number in range(5000)]
            db.executemany("INSERT INTO alarms(id,rule_name,camera_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at) "
                           "VALUES(?,'Gate',?,'person','zone_entry',?,'{}','x','y',?,'z','z')", [(a, c, t, s) for a, c, t, s in rows])
        statements: list[str] = []
        real = connect

        def counting():
            db = real()
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
            return db

        for client in (self.admin, self.limited):
            for params in ({}, {"state": "all"}, {"state": "resolved"}, {"camera_id": self.d1}):
                statements.clear()
                with patch("app.api.alarms.connect", counting):
                    answer = client.get("/api/alarms", params={**params, "limit": 100})
                self.assertEqual(answer.status_code, 200, answer.text)
                self.assertEqual(len(answer.json()["alarms"]), 100)
                self.assertLessEqual(len(statements), 5, statements)  # List, counts, rules, people's names, camera names

    def test_the_lists_are_read_in_order_from_an_index_not_sorted(self):
        with connect() as db:
            for sql, params in (
                ("SELECT * FROM alarms INDEXED BY alarms_open_order_idx WHERE state IN ('new','acknowledged','assigned') ORDER BY occurred_at DESC,id LIMIT ?", (100,)),
                ("SELECT * FROM alarms WHERE state=? ORDER BY occurred_at DESC,id LIMIT ?", ("resolved", 100)),
                ("SELECT * FROM alarms ORDER BY occurred_at DESC,id LIMIT ?", (100,)),
                ("SELECT * FROM alarms WHERE camera_id=? AND state IN ('new','acknowledged','assigned') ORDER BY occurred_at DESC,id LIMIT ?", (self.d1, 100)),
                ("SELECT e.*,(SELECT c.name FROM cameras c WHERE c.id=e.camera_id) AS camera_name FROM rule_events e ORDER BY e.occurred_at DESC,e.id LIMIT ?", (200,)),
                ("SELECT e.* FROM rule_events e WHERE e.rule_id=? ORDER BY e.occurred_at DESC,e.id LIMIT ?", ("r", 200)),
                ("SELECT e.* FROM rule_events e WHERE e.camera_id=? ORDER BY e.occurred_at DESC,e.id LIMIT ?", (self.d1, 200)),
            ):
                plan = " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))
                self.assertNotIn("TEMP B-TREE", plan, sql)


if __name__ == "__main__":
    unittest.main()
