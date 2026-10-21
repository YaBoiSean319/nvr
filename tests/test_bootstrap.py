import asyncio
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid
import base64
import hashlib
import time as clock
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

OIDC_TEST_TENANT = "11111111-1111-4111-8111-111111111111"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NVR_DB_PATH"] = str(Path(self.tmp.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(Path(self.tmp.name) / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.tmp.name) / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        os.environ["NVR_SEGMENT_SECONDS"] = "10"
        os.environ["NVR_SESSION_HOURS"] = "12"
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)
        from app.main import app
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self):
        self.client_ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        os.environ.pop("NVR_DB_PATH", None)
        os.environ.pop("NVR_RECORDINGS_PATH", None)
        os.environ.pop("NVR_CREDENTIAL_KEY_PATH", None)
        os.environ.pop("NVR_COOKIE_SECURE", None)
        os.environ.pop("NVR_SEGMENT_SECONDS", None)
        os.environ.pop("NVR_SESSION_HOURS", None)
        for key in ("NVR_PUBLIC_URL", "NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_ADMIN_ROLES", "NVR_OIDC_OPERATOR_ROLES"):
            os.environ.pop(key, None)

    def configure_oidc(self, operator_roles="NVR.Operator"):
        os.environ.update({
            "NVR_PUBLIC_URL":"https://nvr.example.test",
            "NVR_OIDC_TENANT_ID":OIDC_TEST_TENANT,
            "NVR_OIDC_CLIENT_ID":"client-id",
            "NVR_OIDC_CLIENT_SECRET":"client-secret",
            "NVR_OIDC_SESSION_SECRET":"test-session-signing-secret-long-enough",
            "NVR_OIDC_OPERATOR_ROLES":operator_roles,
        })

    def test_bootstrap_closes_setup_and_authenticates_admin(self):
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": True})
        response = self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "administrator")
        self.assertEqual(self.client.get("/api/setup/status").json(), {"setup_required": False})
        self.assertEqual(self.client.get("/api/system/status").status_code, 200)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"second-admin", "password":"AnotherStrongPass2026"}).status_code, 409)

    def test_ip_range_discovery_is_bounded_and_reports_scan_targets(self):
        from app.discovery import DiscoveredDevice, DiscoveryResult
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        result=DiscoveryResult((DiscoveredDevice("ipscan:" + "a" * 32, ("http://10.45.50.25:80/onvif/device_service",), ()),), 0, 254)
        with patch("app.main.discover_onvif_ip_range", new=AsyncMock(return_value=result)) as scan:
            response=self.client.post("/api/discovery/scan",json={"ip_range":"10.45.50.0/24"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["scan_mode"],"ip_range")
        self.assertEqual(response.json()["targets_scanned"],254)
        self.assertEqual(response.json()["discovered_count"],1)
        scan.assert_awaited_once_with("10.45.50.0/24")

    def test_onvif_profiles_require_admin_and_return_only_profile_labels(self):
        from app.onvif_stream import OnvifStreamProfile
        self.assertEqual(self.client.post("/api/cameras/onvif-profiles",json={"camera_ip":"10.45.50.25"}).status_code,401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        with patch("app.main.list_camera_stream_profiles",new=AsyncMock(return_value=("http://10.45.50.25:80/onvif/device_service",(OnvifStreamProfile("profile1","Main stream"),)))):
            response=self.client.post("/api/cameras/onvif-profiles",json={"camera_ip":"10.45.50.25","camera_brand":"hikvision","camera_username":"admin","camera_password":"example"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["profiles"],[{"token":"profile1","name":"Main stream"}])
        self.assertNotIn("example",response.text)

    def test_admin_can_capture_and_download_bounded_redacted_diagnostics(self):
        self.assertEqual(self.client.get("/api/diagnostics/status").status_code, 401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(self.client.get("/api/diagnostics/status").json()["enabled"], False)
        self.assertEqual(self.client.post("/api/diagnostics/capture", json={"enabled":True,"hours":25}).status_code, 422)
        enabled=self.client.post("/api/diagnostics/capture",json={"enabled":True,"hours":2})
        self.assertEqual(enabled.status_code,200)
        from app.diagnostics import record_event
        record_event("discovery_scan",outcome="completed",elapsed_ms=123,interface_configured=True,
                     discovered_count=0,ignored_responses=2,ip="192.168.1.30",url="rtsp://user:secret@host/stream",
                     camera_id="private-camera-id",password="secret")
        bundle=self.client.get("/api/diagnostics/bundle")
        self.assertEqual(bundle.status_code,200)
        self.assertIn("attachment",bundle.headers["content-disposition"])
        self.assertEqual(bundle.headers["cache-control"],"no-store")
        text=bundle.text
        self.assertIn('"discovered_count": 0',text)
        self.assertIn('"ignored_responses": 2',text)
        for secret in ("192.168.1.30","rtsp://","private-camera-id","secret","password"):
            self.assertNotIn(secret,text)
        self.assertEqual(self.client.post("/api/diagnostics/capture",json={"enabled":False,"hours":2}).json()["enabled"],False)

    def test_operator_cannot_manage_diagnostics(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",("operator1",hash_password("OperatorPassword2026"),"operator"))
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login",json={"username":"operator1","password":"OperatorPassword2026"})
        self.assertEqual(self.client.get("/api/diagnostics/status").status_code,403)
        self.assertEqual(self.client.post("/api/diagnostics/capture",json={"enabled":True,"hours":2}).status_code,403)
        self.assertEqual(self.client.get("/api/diagnostics/bundle").status_code,403)

    def test_api_denies_status_and_invalid_credentials(self):
        self.assertEqual(self.client.get("/api/system/status").status_code, 401)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client.post("/api/auth/logout")
        bad = self.client.post("/api/auth/login", json={"username":"local-admin", "password":"wrong"})
        self.assertEqual(bad.status_code, 401)

    def test_restart_preserves_account_and_health(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.client_ctx.__exit__(None, None, None)
        from app.main import app
        with TestClient(app) as restarted:
            self.assertEqual(restarted.get("/healthz").status_code, 200)
            self.assertEqual(restarted.get("/api/setup/status").json(), {"setup_required": False})
            self.assertEqual(restarted.get("/api/system/status").status_code, 401)
            self.assertEqual(restarted.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_password_policy_and_username_validation(self):
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"a", "password":"short"}).status_code, 422)
        self.assertEqual(self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"longpasswordonly"}).status_code, 422)

    def test_session_lifetime_is_clamped_to_bounded_range(self):
        from app.main import _session_hours
        os.environ["NVR_SESSION_HOURS"]="1000"
        self.assertEqual(_session_hours(),168)
        os.environ["NVR_SESSION_HOURS"]="0"
        self.assertEqual(_session_hours(),1)
        os.environ["NVR_SESSION_HOURS"]="1"
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)

    def test_operator_can_view_but_cannot_change_camera_configuration(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("operator1", hash_password("OperatorPassword2026"), "operator"))
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"operator1", "password":"OperatorPassword2026"}).status_code, 200)
        self.assertEqual(self.client.get("/api/cameras").status_code, 200)
        self.assertEqual(self.client.get("/api/system/status").status_code, 403)
        self.assertEqual(self.client.post("/api/cameras/test", json={"stream_url":"rtsp://camera/stream"}).status_code, 403)
        self.assertEqual(self.client.post("/api/cameras", json={"name":"Unauthorized", "stream_url":"rtsp://camera/stream"}).status_code, 403)
        self.assertEqual(self.client.patch("/api/cameras/missing", json={"recording_mode":"disabled"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/cameras/missing").status_code, 403)
        self.assertEqual(self.client.post("/api/cameras/bulk-update",json={"camera_ids":["missing"],"group_name":"Ops"}).status_code,403)
        self.assertEqual(self.client.post("/api/camera-groups",json={"name":"Ops"}).status_code,403)
        self.assertEqual(self.client.post("/api/cameras/missing/credentials",json={"stream_url":"rtsp://camera/new"}).status_code,403)
        self.assertEqual(self.client.post("/api/discovery/scan", json={"timeout_seconds":0.5}).status_code, 403)
        self.assertEqual(self.client.get("/api/discovery/candidates").status_code, 403)

    def test_onvif_discovery_candidates_are_reviewed_deduplicated_and_added_explicitly(self):
        from unittest.mock import AsyncMock, patch
        from app.discovery import DiscoveredDevice, DiscoveryResult
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        device=DiscoveredDevice("5a30e8d2-1108-4c1e-9b57-76a30b1d8087", ("http://10.1.2.30:80/onvif/device_service",), ("onvif://www.onvif.org/name/Loading Dock",))
        with patch("app.main.discover_onvif", new=AsyncMock(return_value=DiscoveryResult((device,),1))):
            first=self.client.post("/api/discovery/scan",json={"timeout_seconds":1})
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json()["discovered_count"],1)
        self.assertEqual(first.json()["ignored_responses"],1)
        self.assertEqual(first.json()["candidates"][0]["capabilities"]["video_stream"],"untested")
        candidate_id=first.json()["candidates"][0]["id"]
        self.assertEqual(self.client.get("/api/cameras").json()["cameras"],[])

        with patch("app.main.probe_stream",new=AsyncMock(return_value={"reachable":False,"reason":"Camera stream could not be opened"})):
            failed=self.client.post(f"/api/discovery/candidates/{candidate_id}/add",json={"name":"Loading Dock","stream_url":"rtsp://10.1.2.30/stream"})
        self.assertEqual(failed.status_code,422)
        failed_candidate=self.client.get("/api/discovery/candidates").json()["candidates"][0]
        self.assertEqual(failed_candidate["capabilities"]["video_stream"],"unavailable")

        capability={"reachable":True,"audio":False,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream",new=AsyncMock(return_value=capability)):
            added=self.client.post(f"/api/discovery/candidates/{candidate_id}/add",json={"name":"Loading Dock","stream_url":"rtsp://user:secret@10.1.2.30/stream"})
        self.assertEqual(added.status_code,201,added.text)
        self.assertNotIn("secret",added.text)
        added_candidate=self.client.get("/api/discovery/candidates").json()["candidates"][0]
        self.assertEqual(added_candidate["capabilities"]["video_stream"],"supported")
        self.assertEqual(added_candidate["capabilities"]["audio"],"unsupported")
        self.assertEqual(added_candidate["capabilities"]["ptz"],"untested")
        with patch("app.main.discover_onvif",new=AsyncMock(return_value=DiscoveryResult((device,),0))):
            repeated=self.client.post("/api/discovery/scan",json={"timeout_seconds":0.5})
        self.assertEqual(repeated.status_code,200)
        candidates=repeated.json()["candidates"]
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]["linked_camera_id"],added.json()["id"])
        self.assertEqual(candidates[0]["capabilities"]["video_stream"],"supported")
        self.assertEqual(candidates[0]["capabilities"]["audio"],"unsupported")
        self.assertEqual(self.client.post(f"/api/discovery/candidates/{candidate_id}/add",json={"name":"Duplicate","stream_url":"rtsp://10.1.2.30/stream"}).status_code,409)

    def test_onvif_probe_parser_rejects_unsafe_service_urls_and_malformed_xml(self):
        from app.discovery import parse_probe_response
        payload=b'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:a="http://www.w3.org/2005/08/addressing"><s:Body><d:ProbeMatches><d:ProbeMatch><a:EndpointReference><a:Address>urn:uuid:5a30e8d2-1108-4c1e-9b57-76a30b1d8087</a:Address></a:EndpointReference><d:XAddrs>http://user:secret@10.1.2.30/onvif?token=hidden ftp://10.1.2.30/service</d:XAddrs><d:Scopes>onvif://www.onvif.org/name/Loading%20Dock?secret=hidden https://evil.example/private</d:Scopes></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>'''
        parsed=parse_probe_response(payload)
        self.assertEqual(len(parsed),1)
        self.assertEqual(parsed[0].device_uuid,"5a30e8d2-1108-4c1e-9b57-76a30b1d8087")
        self.assertEqual(parsed[0].service_urls,("http://10.1.2.30/onvif",))
        self.assertEqual(parsed[0].scopes,("onvif://www.onvif.org/name/Loading%20Dock",))
        self.assertEqual(parse_probe_response(b"<!DOCTYPE foo><foo/>") ,[])
        self.assertEqual(parse_probe_response(b"<broken"),[])

    def test_discovered_identity_links_existing_manual_camera_without_losing_footage(self):
        from unittest.mock import AsyncMock, patch
        from app.discovery import DiscoveredDevice, DiscoveryResult
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        capability={"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream",new=AsyncMock(return_value=capability)):
            camera=self.client.post("/api/cameras",json={"name":"Existing manual camera","stream_url":"rtsp://10.1.2.40/stream","recording_mode":"disabled"}).json()
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",(camera["id"],f"{camera['id']}/kept.mp4","2026-10-20T12:00:00+00:00","2026-10-20T12:01:00+00:00",123))
        device=DiscoveredDevice("9d153a1f-d693-4380-8410-0b2c47011ee2",("http://10.1.2.40/onvif/device_service",),())
        with patch("app.main.discover_onvif",new=AsyncMock(return_value=DiscoveryResult((device,),0))):
            candidates=self.client.post("/api/discovery/scan",json={"timeout_seconds":0.5}).json()["candidates"]
        linked=self.client.post(f"/api/discovery/candidates/{candidates[0]['id']}/link",json={"camera_id":camera["id"]})
        self.assertEqual(linked.status_code,200,linked.text)
        self.assertEqual(linked.json()["camera"]["id"],camera["id"])
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?",(camera["id"],)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT onvif_uuid FROM cameras WHERE id=?",(camera["id"],)).fetchone()[0],device.device_uuid)

    def test_camera_import_preview_reports_duplicates_and_row_errors_without_secrets(self):
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        identity="bc1b3433-82bd-4bd9-a0a6-6d78e227553a"
        text=("name,stream_url,recording_mode,group_name,onvif_uuid\n"
              f"Loading Dock,rtsp://operator:supersecret@10.1.2.10/stream,disabled,East,{identity}\n"
              f"Loading Dock,rtsp://10.1.2.11/stream,disabled,East,{identity}\n"
              "Broken,http://10.1.2.12/video,continuous,East,\n")
        response=self.client.post("/api/cameras/import/preview",json={"csv_text":text})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([row["status"] for row in response.json()["rows"]],["accepted","duplicate","invalid"])
        self.assertNotIn("supersecret",response.text)
        self.assertNotIn("rtsp://",response.text)
        self.assertEqual(response.json()["accepted_count"],1)
        self.assertEqual(self.client.get("/api/cameras").json()["cameras"],[])

    def test_camera_import_apply_groups_successful_rows_and_reports_probe_failure(self):
        from unittest.mock import AsyncMock, patch
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        text=("name,stream_url,recording_mode,group_name\n"
              "North,rtsp://10.1.2.10/stream,disabled,North Wing\n"
              "South,rtsp://10.1.2.11/unavailable,continuous,South Wing\n")
        async def probe(url):
            if "unavailable" in url:
                return {"reachable":False,"reason":"Camera stream could not be opened"}
            return {"reachable":True,"video":{"codec":"h264","width":1280,"height":720}}
        with patch("app.main.probe_stream",new=AsyncMock(side_effect=probe)):
            applied=self.client.post("/api/cameras/import/apply",json={"csv_text":text})
        self.assertEqual(applied.status_code,200,applied.text)
        result=applied.json()
        self.assertEqual(result["created_count"],1)
        self.assertEqual([row["status"] for row in result["rows"]],["created","failed"])
        cameras=self.client.get("/api/cameras").json()["cameras"]
        self.assertEqual(len(cameras),1)
        self.assertEqual(cameras[0]["name"],"North")
        self.assertEqual(cameras[0]["group_name"],"North Wing")
        groups=self.client.get("/api/camera-groups").json()["groups"]
        self.assertEqual([group["name"] for group in groups],["North Wing"])
        changed=self.client.post("/api/cameras/bulk-update",json={"camera_ids":[cameras[0]["id"]],"group_name":"Operations"})
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(changed.json()["cameras"][0]["group_name"],"Operations")

    def test_camera_credential_rotation_validates_before_replacing_credentials_and_preserves_identity(self):
        from unittest.mock import AsyncMock, patch
        from app.credentials import decrypt
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        good={"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream",new=AsyncMock(return_value=good)):
            created=self.client.post("/api/cameras",json={"name":"Rotating","stream_url":"rtsp://old:oldsecret@10.1.2.20/stream","recording_mode":"disabled"}).json()
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",(created["id"],f"{created['id']}/kept.mp4","2026-10-20T12:00:00+00:00","2026-10-20T12:01:00+00:00",123))
        with patch("app.main.probe_stream",new=AsyncMock(return_value={"reachable":False,"reason":"Camera stream could not be opened"})):
            rejected=self.client.post(f"/api/cameras/{created['id']}/credentials",json={"stream_url":"rtsp://bad:newsecret@10.1.2.20/stream"})
        self.assertEqual(rejected.status_code,422)
        self.assertNotIn("newsecret",rejected.text)
        with patch("app.main.probe_stream",new=AsyncMock(return_value=good)), patch.object(self.client_ctx.app.state.recorders,"sync_camera",new=AsyncMock()):
            rotated=self.client.post(f"/api/cameras/{created['id']}/credentials",json={"stream_url":"rtsp://new:newsecret@10.1.2.20/stream"})
        self.assertEqual(rotated.status_code,200,rotated.text)
        self.assertEqual(rotated.json()["id"],created["id"])
        self.assertNotIn("newsecret",rotated.text)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            stored=db.execute("SELECT stream_url_ciphertext FROM cameras WHERE id=?",(created["id"],)).fetchone()[0]
            footage=db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?",(created["id"],)).fetchone()[0]
        self.assertEqual(decrypt(stored),"rtsp://new:newsecret@10.1.2.20/stream")
        self.assertEqual(footage,1)

    def test_administrator_can_terminate_another_session(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", ("operator1", hash_password("OperatorPassword2026"), "operator"))
        operator = TestClient(self.client_ctx.app)
        with operator:
            operator.post("/api/auth/login", json={"username":"operator1", "password":"OperatorPassword2026"})
            operator_me = operator.get("/api/auth/me")
            self.assertEqual(operator_me.status_code, 200)
            sessions = self.client.get("/api/admin/sessions").json()["sessions"]
            target = next(s for s in sessions if s["username"] == "operator1")
            self.assertEqual(self.client.delete(f"/api/admin/sessions/{target['session_id']}").status_code, 204)
            self.assertEqual(operator.get("/api/auth/me").status_code, 401)

    def test_existing_database_migrates_roles_and_preserves_live_sessions(self):
        legacy_path=Path(self.tmp.name)/"legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
              CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL COLLATE NOCASE UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('administrator')),created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z');
              CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z');
              CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
              INSERT INTO users(username,password_hash,role) VALUES('legacy-admin','unused','administrator');
              INSERT INTO sessions(token_hash,user_id,expires_at) VALUES('legacy-token',1,'2999-01-01T00:00:00Z');
            """)
        old_path=os.environ["NVR_DB_PATH"]
        os.environ["NVR_DB_PATH"]=str(legacy_path)
        from app.store import initialize
        initialize()
        with sqlite3.connect(legacy_path) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('new-operator','unused','operator')")
            migrated=db.execute("SELECT session_id,token_hash,user_id FROM sessions").fetchone()
        self.assertTrue(migrated[0])
        self.assertEqual(migrated[1:], ("legacy-token",1))
        os.environ["NVR_DB_PATH"]=old_path

    def test_oidc_maps_roles_denies_unmapped_users_and_keeps_local_login_available(self):
        self.configure_oidc("NVR-Operator")
        from unittest.mock import AsyncMock, patch
        from app.main import _mapped_role
        self.assertIsNone(_mapped_role({"roles":["Unmapped"]}))
        self.assertEqual(_mapped_role({"roles":["NVR-Operator"]}), "operator")
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        tenant_id=OIDC_TEST_TENANT
        claims={"sub":"object-123", "tid":tenant_id, "iss":f"https://login.microsoftonline.com/{tenant_id}/v2.0", "preferred_username":"operator@example.test", "roles":["NVR-Operator"]}
        fake=type("FakeOIDC", (), {"authorize_access_token":AsyncMock(return_value={"userinfo":claims})})()
        with patch("app.main._oidc_client", return_value=fake):
            callback=self.client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False)
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(self.client.get("https://testserver.local/api/auth/me").json()["role"], "operator")
        self.client.post("/api/auth/logout")
        claims["roles"]=["Unmapped"]
        with patch("app.main._oidc_client", return_value=fake):
            denied=self.client.get("/api/auth/oidc/callback?code=fake&state=fake", follow_redirects=False)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_authlib_authorization_request_uses_state_nonce_and_pkce_s256(self):
        import asyncio
        from urllib.parse import parse_qs, urlsplit
        from authlib.integrations.starlette_client import OAuth
        oauth=OAuth()
        oauth.register("test", client_id="id", client_secret="secret", authorize_url="https://idp.test/authorize", access_token_url="https://idp.test/token", client_kwargs={"scope":"openid profile email", "code_challenge_method":"S256", "trust_env":False})
        data=asyncio.run(oauth.create_client("test").create_authorization_url("https://nvr.test/callback"))
        query=parse_qs(urlsplit(data["url"]).query)
        self.assertTrue(query.get("state"))
        self.assertTrue(query.get("nonce"))
        self.assertTrue(query.get("code_challenge"))
        self.assertEqual(query.get("code_challenge_method"), ["S256"])

    def test_oidc_callback_validates_state_nonce_signature_and_pkce_with_fake_provider(self):
        import httpx
        from authlib.integrations.httpx_client import AsyncOAuth2Client
        from authlib.integrations.starlette_client.apps import StarletteOAuth2App
        from authlib.jose import JsonWebKey, jwt
        from unittest.mock import patch

        tenant=OIDC_TEST_TENANT
        client_id="client-id"
        issuer=f"https://login.microsoftonline.com/{tenant}/v2.0"
        jwks_url=f"https://login.microsoftonline.com/{tenant}/keys"
        private_key=JsonWebKey.generate_key("RSA",2048,options={"kid":"fake-key"},is_private=True)
        observed={"challenge":None,"nonce":None,"bad_nonce":False}

        def provider(request):
            if request.url.path.endswith(".well-known/openid-configuration"):
                return httpx.Response(200,json={
                    "issuer":issuer,
                    "authorization_endpoint":"https://idp.test/authorize",
                    "token_endpoint":"https://idp.test/token",
                    "jwks_uri":jwks_url,
                    "response_types_supported":["code"],
                    "subject_types_supported":["public"],
                    "id_token_signing_alg_values_supported":["RS256"],
                    "token_endpoint_auth_methods_supported":["client_secret_basic"],
                })
            if request.url.path == "/token":
                form=parse_qs(request.content.decode())
                verifier=form["code_verifier"][0]
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if challenge != observed["challenge"]:
                    return httpx.Response(400,json={"error":"invalid_grant"})
                now=int(clock.time())
                token=jwt.encode({"alg":"RS256","kid":"fake-key"},{
                    "iss":issuer,"aud":client_id,"sub":"fake-subject","tid":tenant,
                    "iat":now,"exp":now+600,
                    "nonce":"wrong-nonce" if observed["bad_nonce"] else observed["nonce"],
                    "preferred_username":"fake-operator@example.test","roles":["NVR.Operator"],
                },private_key).decode()
                return httpx.Response(200,json={"access_token":"fake-access","token_type":"Bearer","expires_in":600,"id_token":token})
            if request.url == jwks_url:
                return httpx.Response(200,json={"keys":[private_key.as_dict(is_private=False)]})
            return httpx.Response(404)

        class FakeProviderClient(AsyncOAuth2Client):
            def __init__(self,*args,**kwargs):
                kwargs["transport"]=httpx.MockTransport(provider)
                kwargs["trust_env"]=False
                super().__init__(*args,**kwargs)

        self.configure_oidc()
        self.client.post("/api/setup/admin",json={"username":"local-admin","password":"StrongPassphrase2026"})
        with patch.object(StarletteOAuth2App,"client_cls",FakeProviderClient):
            login=self.client.get("https://testserver.local/api/auth/oidc/login?format=json",follow_redirects=False)
            self.assertEqual(login.status_code,200,login.text)
            query=parse_qs(urlsplit(login.json()["authorization_url"]).query)
            self.assertEqual(query["redirect_uri"],["https://nvr.example.test/api/auth/oidc/callback"])
            observed["challenge"]=query["code_challenge"][0]
            observed["nonce"]=query["nonce"][0]
            wrong_state=self.client.get(f"https://testserver.local/api/auth/oidc/callback?code=valid&state=incorrect",follow_redirects=False)
            self.assertEqual(wrong_state.status_code,401)
            valid=self.client.get(f"https://testserver.local/api/auth/oidc/callback?code=valid&state={query['state'][0]}",follow_redirects=False)
            self.assertEqual(valid.status_code,303,valid.text)
            self.assertEqual(self.client.get("https://testserver.local/api/auth/me").json()["role"],"operator")
            replay=self.client.get(f"https://testserver.local/api/auth/oidc/callback?code=valid&state={query['state'][0]}",follow_redirects=False)
            self.assertEqual(replay.status_code,401)
            self.client.post("/api/auth/logout")
            second=self.client.get("https://testserver.local/api/auth/oidc/login?format=json",follow_redirects=False)
            second_query=parse_qs(urlsplit(second.json()["authorization_url"]).query)
            observed["challenge"]=second_query["code_challenge"][0]
            observed["nonce"]=second_query["nonce"][0]
            observed["bad_nonce"]=True
            invalid_nonce=self.client.get(f"https://testserver.local/api/auth/oidc/callback?code=badnonce&state={second_query['state'][0]}",follow_redirects=False)
            self.assertEqual(invalid_nonce.status_code,401)

    def test_oidc_provider_outage_does_not_disable_local_administrator(self):
        self.configure_oidc()
        from unittest.mock import AsyncMock, patch
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        fake=type("FakeOIDC", (), {"create_authorization_url":AsyncMock(side_effect=OSError("provider offline"))})()
        with patch("app.main._oidc_client", return_value=fake):
            unavailable=self.client.get("/api/auth/oidc/login?format=json", follow_redirects=False)
            self.assertEqual(unavailable.status_code, 503)
            self.assertIn("local administrator sign-in remains available", unavailable.json()["detail"])
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"}).status_code, 200)

    def test_camera_credentials_are_encrypted_and_not_returned(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        stream_url = "rtsp://viewer:CameraSecret2026@127.0.0.1:554/stream?token=NeverReturnThis"
        probe = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=probe)):
            added = self.client.post("/api/cameras", json={"name":"Test Camera", "stream_url":stream_url})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.json()["recording_mode"], "motion")
        self.assertFalse(added.json()["recording_enabled"])
        self.assertEqual(added.json()["recording_status"], "stopped")
        response_text = added.text + self.client.get("/api/cameras").text
        self.assertNotIn("CameraSecret2026", response_text)
        self.assertNotIn("NeverReturnThis", response_text)
        self.assertIn("rtsp://127.0.0.1:554", response_text)
        self.assertNotIn("/stream", response_text)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            ciphertext = db.execute("SELECT stream_url_ciphertext FROM cameras").fetchone()[0]
        self.assertNotIn(b"CameraSecret2026", ciphertext)
        self.assertEqual(Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.delete(f"/api/cameras/{added.json()['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/cameras").json()["cameras"], [])

    def test_motion_recording_requires_enablement_and_encrypts_event_credentials(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            response = self.client.post("/api/cameras", json={
                "name":"Motion policy camera", "stream_url":"rtsp://127.0.0.1/stream",
                "recording_mode":"motion", "motion_service_url":"http://127.0.0.1/onvif/device_service",
                "motion_username":"onvif-user", "motion_password":"MotionSecret2026",
            })
        self.assertEqual(response.status_code, 201, response.text)
        camera = response.json()
        self.assertFalse(camera["recording_enabled"])
        self.assertEqual(camera["recording_status"], "stopped")
        self.assertTrue(camera["motion_source_configured"])
        self.assertNotIn("MotionSecret2026", response.text)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            ciphertext = db.execute("SELECT motion_credentials_ciphertext FROM cameras WHERE id=?", (camera["id"],)).fetchone()[0]
        self.assertNotIn(b"MotionSecret2026", ciphertext)
        malformed_auth = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={
            "service_url":"http://127.0.0.1/onvif/device_service", "username":"only-user"
        })
        self.assertEqual(malformed_auth.status_code, 422)
        malformed_port = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={
            "service_url":"http://127.0.0.1:70000/onvif/device_service"
        })
        self.assertEqual(malformed_port.status_code, 422)
        enabled = self.client.patch(f"/api/cameras/{camera['id']}", json={"recording_enabled":True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["recording_enabled"])
        self.assertEqual(enabled.json()["recording_status"], "connecting")
        disabled = self.client.patch(f"/api/cameras/{camera['id']}", json={"recording_mode":"disabled"})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["recording_enabled"])

    def test_bulk_recording_enablement_updates_only_selected_cameras(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            first = self.client.post("/api/cameras", json={"name":"Bulk first","stream_url":"rtsp://127.0.0.1/one"}).json()
            second = self.client.post("/api/cameras", json={"name":"Bulk second","stream_url":"rtsp://127.0.0.1/two"}).json()
        updated = self.client.post("/api/cameras/bulk-update", json={"camera_ids":[first["id"]],"recording_enabled":True})
        self.assertEqual(updated.status_code, 200, updated.text)
        cameras = {camera["id"]:camera for camera in self.client.get("/api/cameras").json()["cameras"]}
        self.assertTrue(cameras[first["id"]]["recording_enabled"])
        self.assertFalse(cameras[second["id"]]["recording_enabled"])

    def test_group_schedule_inheritance_camera_override_and_restoration(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)), \
             patch("app.main.app.state.recorders.sync_camera", new=AsyncMock()):
            camera = self.client.post("/api/cameras", json={
                "name":"Scheduled camera", "stream_url":"rtsp://10.1.2.40/stream", "recording_mode":"continuous"
            }).json()
            group = self.client.post("/api/camera-groups", json={"name":"Night shift"}).json()
            assigned = self.client.post("/api/cameras/bulk-update", json={"camera_ids":[camera["id"]],"group_name":group["name"]})
            self.assertEqual(assigned.status_code, 200, assigned.text)
            intervals = lambda mode: [
                {"days":[day], "start":"00:00", "end":"12:00", "mode":mode} for day in range(7)
            ] + [
                {"days":[day], "start":"12:00", "end":"00:00", "mode":mode} for day in range(7)
            ]
            schedule = {"timezone":"UTC", "intervals":intervals("continuous")}
            saved_group = self.client.put(f"/api/camera-groups/{group['id']}/schedule", json=schedule)
            self.assertEqual(saved_group.status_code, 200, saved_group.text)
            self.client.patch(f"/api/cameras/{camera['id']}", json={"recording_enabled":True})
            inherited = self.client.get(f"/api/cameras/{camera['id']}/schedule")
            self.assertTrue(inherited.json()["inherits_group"])
            inventory = self.client.get("/api/cameras").json()["cameras"][0]
            self.assertEqual(inventory["effective_recording_mode"], "continuous")
            self.assertEqual(inventory["recording_policy_source"], "group")

            override = self.client.put(f"/api/cameras/{camera['id']}/schedule", json={
                "timezone":"UTC", "intervals":intervals("motion")
            })
            self.assertEqual(override.status_code, 200, override.text)
            self.assertFalse(override.json()["inherits_group"])
            inventory = self.client.get("/api/cameras").json()["cameras"][0]
            self.assertEqual(inventory["effective_recording_mode"], "motion")
            self.assertEqual(inventory["recording_policy_source"], "camera")

            restored = self.client.put(f"/api/cameras/{camera['id']}/schedule", json={"inherit_group":True})
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json()["effective_recording_mode"], "continuous")
            self.assertTrue(restored.json()["inherits_group"])

    def test_schedule_overlap_validation_and_operator_write_denial(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        group = self.client.post("/api/camera-groups", json={"name":"Schedule ACL"}).json()
        invalid = self.client.put(f"/api/camera-groups/{group['id']}/schedule", json={
            "timezone":"UTC", "intervals":[
                {"days":[0],"start":"08:00","end":"10:00","mode":"motion"},
                {"days":[0],"start":"09:00","end":"11:00","mode":"continuous"},
            ]
        })
        self.assertEqual(invalid.status_code, 422)
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("operator-one",hash_password("OperatorPassphrase2026"),"operator"))
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/auth/login", json={"username":"operator-one","password":"OperatorPassphrase2026"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/camera-groups/{group['id']}/schedule").status_code, 200)
        denied = self.client.put(f"/api/camera-groups/{group['id']}/schedule", json={"timezone":"UTC","intervals":[]})
        self.assertEqual(denied.status_code, 403)

    def test_recording_manager_switches_modes_when_group_schedule_changes(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        async def hold_recording(*_args):
            await asyncio.Event().wait()
        manager = self.client_ctx.app.state.recorders
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)), \
             patch.object(manager, "_run", new=AsyncMock(side_effect=hold_recording)):
            camera = self.client.post("/api/cameras", json={
                "name":"Scheduled runtime camera", "stream_url":"rtsp://10.1.2.41/stream", "recording_mode":"continuous"
            }).json()
            group = self.client.post("/api/camera-groups", json={"name":"Runtime schedule"}).json()
            self.client.post("/api/cameras/bulk-update", json={"camera_ids":[camera["id"]],"group_name":group["name"]})
            all_week = lambda mode: [
                {"days":[day],"start":"00:00","end":"12:00","mode":mode} for day in range(7)
            ] + [
                {"days":[day],"start":"12:00","end":"00:00","mode":mode} for day in range(7)
            ]
            self.client.put(f"/api/camera-groups/{group['id']}/schedule", json={"timezone":"UTC","intervals":all_week("continuous")})
            self.client.patch(f"/api/cameras/{camera['id']}", json={"recording_enabled":True})
            self.assertEqual(manager.task_modes[camera["id"]], "continuous")
            switched = self.client.put(f"/api/camera-groups/{group['id']}/schedule", json={"timezone":"UTC","intervals":all_week("motion")})
            self.assertEqual(switched.status_code, 200, switched.text)
            self.assertEqual(manager.task_modes[camera["id"]], "motion")

    def test_server_motion_fallback_configuration_is_validated_and_exposed(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable":True,"video":{"codec":"h264","width":640,"height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            camera = self.client.post("/api/cameras", json={
                "name":"Fallback policy camera", "stream_url":"rtsp://127.0.0.1/stream"
            }).json()
        response = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={
            "fallback_enabled":True,
            "zones":[{"x":0.1,"y":0.2,"width":0.5,"height":0.6}],
            "sensitivity":72,
            "pre_roll_seconds":10,
            "post_roll_seconds":30,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["motion_fallback_enabled"])
        self.assertEqual(response.json()["motion_sensitivity"], 72)
        self.assertEqual(response.json()["server_motion_status"], "waiting")
        self.assertEqual(response.json()["motion_zones"], [{"x":0.1,"y":0.2,"width":0.5,"height":0.6}])
        partial = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={"sensitivity":80})
        self.assertEqual(partial.status_code, 200, partial.text)
        self.assertEqual(partial.json()["motion_pre_roll_seconds"], 10)
        self.assertEqual(partial.json()["motion_post_roll_seconds"], 30)
        custom = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={
            "fallback_enabled":True, "pre_roll_seconds":7, "post_roll_seconds":19
        })
        self.assertEqual(custom.status_code, 200, custom.text)
        partial = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={"sensitivity":60})
        self.assertEqual(partial.json()["motion_pre_roll_seconds"], 7)
        self.assertEqual(partial.json()["motion_post_roll_seconds"], 19)
        invalid_zone = self.client.post(f"/api/cameras/{camera['id']}/motion-source", json={
            "fallback_enabled":True, "zones":[{"x":0.9,"y":0,"width":0.2,"height":1}]
        })
        self.assertEqual(invalid_zone.status_code, 422)

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "FFprobe is required for the stream test")
    def test_real_probe_failure_is_reported_without_echoing_credentials(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.post("/api/cameras/test", json={
            "stream_url":"rtsp://probe-user:ProbeSecret2026@127.0.0.1:1/unavailable"
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["reachable"])
        self.assertNotIn("ProbeSecret2026", response.text)
        self.assertEqual(response.json()["reason"], "Camera stream could not be opened")

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for reconnect testing")
    def test_continuous_recorder_reconnects_after_stream_failure(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        capability = {"reachable": True, "video": {"codec":"h264", "width":640, "height":360}}
        with patch("app.main.probe_stream", new=AsyncMock(return_value=capability)):
            created = self.client.post("/api/cameras", json={
                "name":"Unavailable test feed",
                "stream_url":"rtsp://feed-user:FeedSecret2026@127.0.0.1:1/stream",
                "recording_mode":"continuous",
                "recording_enabled":True,
            })
        self.assertEqual(created.status_code, 201)
        camera_id = created.json()["id"]
        deadline = time.monotonic() + 8
        current = created.json()
        while time.monotonic() < deadline:
            current = self.client.get("/api/cameras").json()["cameras"][0]
            if current["recording_status"] == "reconnecting":
                break
            time.sleep(0.2)
        self.assertEqual(current["recording_status"], "reconnecting")
        self.assertNotIn("FeedSecret2026", self.client.get("/api/cameras").text)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(os.environ.get("NVR_RTSP_TEST_URL"), "A simulated RTSP publisher is required")
    def test_rtsp_stream_is_probed_recorded_indexed_and_playable(self):
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        ffmpeg_log = Path(self.tmp.name) / "ffmpeg-stderr.log"
        stderr_log = ffmpeg_log.open("wb")
        self.addCleanup(stderr_log.close)
        stderr_patch = patch("app.recording.asyncio.subprocess.DEVNULL", stderr_log)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)
        created = self.client.post("/api/cameras", json={
            "name":"Simulated RTSP camera",
            "stream_url":os.environ["NVR_RTSP_TEST_URL"],
            "recording_mode":"continuous",
            "recording_enabled":True,
        })
        self.assertEqual(created.status_code, 201, created.text)
        camera_id = created.json()["id"]
        self.assertTrue(created.json()["video"]["codec"])
        deadline = time.monotonic() + 35
        segments = []
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/cameras/{camera_id}/segments")
            self.assertEqual(response.status_code, 200)
            segments = response.json()["segments"]
            if segments:
                break
            time.sleep(1)
        log_text = ffmpeg_log.read_text(errors="replace")
        self.assertTrue(segments, f"FFmpeg did not index a completed segment from the RTSP stream. FFmpeg stderr:\n{log_text}")
        playback = self.client.get(segments[0]["media_url"])
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.headers["content-type"], "video/mp4")
        self.assertIn("inline", playback.headers["content-disposition"])
        self.assertGreater(len(playback.content), 128)
        stopped = self.client.patch(f"/api/cameras/{camera_id}", json={"recording_mode":"disabled"})
        self.assertEqual(stopped.status_code, 200)

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "FFmpeg is required for the media response test")
    def test_authenticated_browser_media_route_serves_indexed_video(self):
        media_root = Path(os.environ["NVR_RECORDINGS_PATH"])
        camera_id = str(uuid.uuid4())
        directory = media_root / camera_id / uuid.uuid4().hex
        directory.mkdir(parents=True)
        index_path = directory / "segments.csv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc=duration=2:size=64x64:rate=10", "-an", "-c:v", "libx264", "-f", "segment",
            "-segment_time", "60", "-reset_timestamps", "1", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+faststart", "-segment_list", str(index_path),
            "-segment_list_type", "csv", "-strftime", "1", str(directory / "segment-%Y%m%dT%H%M%SZ.mp4"),
        ], check=True, timeout=20)
        self.client.post("/api/setup/admin", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        from app.credentials import encrypt
        from app.security import hash_password
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            db.execute(
                "INSERT INTO cameras(id, name, stream_url_ciphertext, recording_mode) VALUES (?, ?, ?, 'disabled')",
                (camera_id, "Playback camera", encrypt("rtsp://viewer:secret@camera/stream")),
            )
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", ("viewer1", hash_password("ViewerPassword2026"), "operator"))
        from app.recording import RecordingManager
        self.assertEqual(RecordingManager()._index_csv(camera_id, index_path, directory), 1)
        with sqlite3.connect(os.environ["NVR_DB_PATH"]) as db:
            segment_id = db.execute("SELECT id FROM recording_segments").fetchone()[0]
            relative_path = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment_id,)).fetchone()[0]
        media = media_root / relative_path
        anonymous = self.client.post("/api/auth/logout")
        self.assertEqual(anonymous.status_code, 204)
        denied = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(denied.status_code, 401)
        self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        response = self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertGreater(len(response.content), 100)
        partial = self.client.get(
            f"/api/cameras/{camera_id}/segments/{segment_id}/media", headers={"Range":"bytes=0-99"}
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(len(partial.content), 100)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username":"viewer1", "password":"ViewerPassword2026"})
        self.assertEqual(self.client.get(f"/api/cameras/{camera_id}/segments").status_code, 200)
        self.assertEqual(self.client.get(f"/api/cameras/{camera_id}/segments/{segment_id}/media").status_code, 200)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        self.assertGreater(float(probe.stdout.strip()), 0)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username":"local-admin", "password":"StrongPassphrase2026"})
        self.assertEqual(self.client.delete(f"/api/cameras/{camera_id}").status_code, 204)
        self.assertFalse((media_root / camera_id).exists())


if __name__ == "__main__":
    unittest.main()
