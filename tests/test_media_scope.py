"""Pictures, audio and video need the ``media`` scope: a read-only token cannot fetch them (#295)."""
from __future__ import annotations

import ast
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.store import connect
from tests.test_plates import PlateCase

WIDTH, HEIGHT = 64, 36


def synthetic_jpeg() -> bytes:
    return subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=gray:s={WIDTH}x{HEIGHT}",
                           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout


class MediaScopeCase(PlateCase):
    def setUp(self):
        super().setUp()
        self.track = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,model_id,model_version,provider) VALUES(?,?,?,?,?,1,'m','1','CPU')",
                       (self.track, self.d1, "person", "2026-10-21T10:00:00+00:00", "2026-10-21T10:00:01+00:00"))
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (self.track, "2026-10-21T10:00:00+00:00", synthetic_jpeg()))
        self.plate_track = self.read(self.d1, "ABC123")
        with connect() as db:
            self.read_id = db.execute("SELECT id FROM plate_reads LIMIT 1").fetchone()["id"]
        self.routes = {"track thumbnail": f"/api/ai/tracks/{self.track}/thumbnail", "plate crop": f"/api/plates/reads/{self.read_id}/crop"}

    def secret(self, *scopes: str) -> str:
        created = self.admin.post("/api/admin/api-tokens", json={"name": "-".join(scopes), "scopes": list(scopes)})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["secret"]

    def with_token(self, path: str, *scopes: str):
        with TestClient(self.app) as bare:  # No cookie: only the token authenticates
            return bare.get(path, headers={"Authorization": f"Bearer {self.secret(*scopes)}"})


class RealRouteTests(MediaScopeCase):
    def test_only_the_media_scope_or_admin_fetches_the_pictures(self):
        for name, path in self.routes.items():
            for scopes in (("read",), ("export",), ("metrics",), ("read", "export", "metrics")):
                denied = self.with_token(path, *scopes)
                self.assertEqual(denied.status_code, 403, f"{name} with {scopes}: {denied.status_code}")
                self.assertIn("'media' scope", denied.json()["detail"])
            for scopes in (("media",), ("admin",)):
                allowed = self.with_token(path, *scopes)
                self.assertEqual((allowed.status_code, allowed.headers["content-type"]), (200, "image/jpeg"), f"{name} with {scopes}")

    def test_a_person_still_sees_them_and_camera_grants_still_apply(self):
        for name, path in self.routes.items():
            self.assertEqual(self.limited.get(path).status_code, 200, name)  # Dock operator, camera in Dock
        hidden = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,detection_count,model_id,model_version,provider) VALUES(?,?,?,?,?,1,'m','1','CPU')",
                       (hidden, self.y1, "person", "2026-10-21T10:00:00+00:00", "2026-10-21T10:00:01+00:00"))
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (hidden, "2026-10-21T10:00:00+00:00", synthetic_jpeg()))
        self.assertEqual(self.limited.get(f"/api/ai/tracks/{hidden}/thumbnail").status_code, 404, "a camera the person cannot see is still not found")
        self.assertEqual(self.admin.get(f"/api/ai/tracks/{hidden}/thumbnail").status_code, 200)

    def test_no_credentials_at_all_is_refused(self):
        with TestClient(self.app) as bare:
            for path in self.routes.values():
                self.assertEqual(bare.get(path).status_code, 401)

    def test_the_published_description_agrees_with_what_runs(self):
        schema = self.admin.get("/openapi.json").json()["paths"]
        self.assertEqual(schema["/api/ai/tracks/{track_id}/thumbnail"]["get"]["x-required-scope"], "media")
        self.assertEqual(schema["/api/plates/reads/{read_id}/crop"]["get"]["x-required-scope"], "media")


# Routes that return picture, audio or video bytes without the media scope on purpose, and why.
PUBLIC_OR_SIGNED = {
    "get_thumbnail": "push notification picture: a short-lived signed link, fetched without a session",
    "get_public_branding_asset": "the organisation's logo, shown on the sign-in page",
    "app_icon": "installable-app icons",
    "manifest": "the web app manifest: JSON that names the icons, no picture bytes",
    "footage_file": "failover peer, signed with the shared secret",
    "backup_file": "failover peer, signed with the shared secret",
}


def _handlers_returning_media():
    for path in sorted(Path("app/api").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(item) for item in node.decorator_list]
            routed = [item for item in decorators if item.startswith("router.get(") or (item.startswith("router.api_route(") and "GET" in item)]
            source = ast.get_source_segment(path.read_text(), node) or ""
            if routed and any(f'"{kind}/' in source or f"'{kind}/" in source for kind in ("image", "video", "audio")):
                yield path.name, node, decorators


class GuardTests(MediaScopeCase):
    def test_every_handler_that_returns_media_needs_media_export_or_admin(self):
        checked = 0
        for module, node, decorators in _handlers_returning_media():
            if node.name in PUBLIC_OR_SIGNED:
                continue
            checked += 1
            scoped = any(item.startswith("requires_scope(") and any(f'"{scope}"' in item or f"'{scope}'" in item for scope in ("media", "export", "admin")) for item in decorators)
            administrator = any("Administrator" in ast.unparse(argument.annotation) for argument in node.args.args if argument.annotation)
            self.assertTrue(scoped or administrator, f"{module}: {node.name} returns picture, audio or video bytes but a read-only token could fetch it; add @requires_scope(\"media\")")
        self.assertGreater(checked, 8, "the guard found almost nothing: it is no longer looking at the routes")
