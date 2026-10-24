from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app import api
from app.application import create_app
from app.principal import COOKIE_NAME, Administrator, CurrentUser, Principal
from app.security import token_digest
from app.store import connect


class ApplicationFactoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        self.probe = APIRouter()

        @self.probe.get("/api/test-probe/operator")
        def operator_probe(user: CurrentUser) -> dict[str, object]:
            return {"id": user["id"], "role": user.role}

        @self.probe.get("/api/test-probe/administrator")
        def administrator_probe(user: Administrator) -> dict[str, object]:
            return {"username": user["username"]}

        self.routers = api.ROUTERS
        api.ROUTERS = (*api.ROUTERS, self.probe)

    def tearDown(self):
        api.ROUTERS = self.routers
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def _session(self, user_id: int, username: str, role: str, *, expired: bool = False) -> str:
        token = f"fixture-session-{username}"
        expires = datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1))
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(?,?,?,?)", (user_id, username, "unused", role))
            db.execute(
                "INSERT INTO sessions(session_id,token_hash,user_id,expires_at) VALUES(?,?,?,?)",
                (f"session-{user_id}", token_digest(token), user_id, expires.isoformat()),
            )
        return token

    def test_factory_builds_isolated_apps_without_workers(self):
        first = create_app(start_workers=False)
        second = create_app(start_workers=False)
        self.assertIsNot(first, second)
        with TestClient(first) as client:
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
            self.assertFalse(hasattr(first.state, "recorders"))
        with connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_principal_dependencies_enforce_roles_on_registered_routers(self):
        with TestClient(create_app(start_workers=False)) as client:
            self.assertEqual(client.get("/api/test-probe/operator").status_code, 401)

            operator = self._session(1, "watcher", "operator")
            client.cookies.set(COOKIE_NAME, operator)
            self.assertEqual(client.get("/api/test-probe/operator").json(), {"id": 1, "role": "operator"})
            denied = client.get("/api/test-probe/administrator")
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.headers["cache-control"], "no-store")

            admin = self._session(2, "keeper", "administrator")
            client.cookies.set(COOKIE_NAME, admin)
            self.assertEqual(client.get("/api/test-probe/administrator").json(), {"username": "keeper"})

            expired = self._session(3, "former", "administrator", expired=True)
            client.cookies.set(COOKIE_NAME, expired)
            self.assertEqual(client.get("/api/test-probe/operator").status_code, 401)

    def test_principal_keeps_row_style_access(self):
        principal = Principal(id=4, username="reader", role="operator")
        self.assertEqual((principal["id"], principal["username"], principal["role"]), (4, "reader", "operator"))
        self.assertFalse(principal.is_administrator)


if __name__ == "__main__":
    unittest.main()
