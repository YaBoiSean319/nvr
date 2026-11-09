from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.application import create_app
from app.routes import all_routes


class AllRoutesTests(unittest.TestCase):
    def test_lists_the_application_routes_however_fastapi_nests_them(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("NVR_DB_PATH")
            os.environ["NVR_DB_PATH"] = str(Path(directory) / "state.sqlite3")
            try:
                routes = all_routes(create_app(start_workers=False))
            finally:
                if previous is None:
                    os.environ.pop("NVR_DB_PATH", None)
                else:
                    os.environ["NVR_DB_PATH"] = previous
        paths = {route.path for route in routes if isinstance(route, APIRoute)}
        # Dozens of routers and hundreds of routes: a count near zero is the silent failure this guards against (#285).
        self.assertGreater(len(paths), 200)
        for expected in ("/api/cameras", "/api/auth/login", "/api/alarms/{alarm_id}/acknowledge", "/openapi.json"):
            self.assertIn(expected, paths)

    def test_a_router_included_with_a_prefix_is_refused_rather_than_listed_wrongly(self):
        app, router = FastAPI(), APIRouter()

        @router.get("/things")
        def things() -> dict[str, int]:
            return {}

        app.include_router(router, prefix="/api")
        try:
            listed = {route.path for route in all_routes(app)}
        except RuntimeError:
            return  # FastAPI 0.137 and later: nested, and the prefix is not applied by flattening, so it says so
        self.assertIn("/api/things", listed)  # Earlier versions already list the full path


if __name__ == "__main__":
    unittest.main()
