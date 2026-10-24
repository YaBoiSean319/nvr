from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.principal import current_user
from app.store import connect, initialize


class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.previous=os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"]=str(Path(self.temporary.name)/"state.sqlite3")
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        self.admin={"id":1,"username":"admin","role":"administrator"}
        app.dependency_overrides[current_user]=lambda:self.admin
        self.context=TestClient(app)
        self.client=self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None,None,None)
        app.dependency_overrides.pop(current_user,None)
        if self.previous is None:os.environ.pop("NVR_DB_PATH",None)
        else:os.environ["NVR_DB_PATH"]=self.previous
        self.temporary.cleanup()

    def _png(self)->bytes:
        ffmpeg=shutil.which("ffmpeg")
        if not ffmpeg:self.skipTest("ffmpeg is required to generate a valid image fixture")
        result=subprocess.run([ffmpeg,"-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=red:s=16x16","-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"],capture_output=True,check=True,timeout=10)
        return result.stdout

    def test_public_branding_is_minimal_and_admin_save_is_audited(self):
        public=self.client.get("/api/public/branding")
        self.assertEqual(public.status_code,200)
        self.assertEqual(public.json()["app_name"],"Workplace NVR")
        self.assertEqual(set(public.json()),{"app_name","primary_color","accent_color","assets","updated_at"})
        saved=self.client.put("/api/settings/branding",json={"app_name":"  North Hall  ","primary_color":"#101820","accent_color":"#f0a020"})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()["app_name"],"North Hall")
        with connect() as db:
            audit=db.execute("SELECT action,actor_id,details_json FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(audit["action"],"branding_saved")
        self.assertEqual(audit["actor_id"],1)
        self.assertNotIn("North Hall",audit["details_json"])

    def test_asset_upload_is_validated_served_versioned_and_resettable(self):
        png=self._png()
        response=self.client.put("/api/settings/branding",json={
            "app_name":"North Hall","primary_color":"#101820","accent_color":"#f0a020",
            "assets":{"primary":{"media_type":"image/png","data_base64":base64.b64encode(png).decode("ascii")}},
        })
        self.assertEqual(response.status_code,200,response.text)
        url=response.json()["assets"]["primary"]["url"]
        asset=self.client.get(url)
        self.assertEqual(asset.status_code,200)
        self.assertEqual(asset.headers["content-type"],"image/png")
        self.assertEqual(asset.headers["cache-control"],"public, max-age=31536000, immutable")
        self.assertEqual(asset.content,png)
        rejected=self.client.put("/api/settings/branding",json={
            "app_name":"Must not publish","primary_color":"#101820","accent_color":"#f0a020",
            "assets":{"icon":{"media_type":"image/png","data_base64":base64.b64encode(b"<svg></svg>").decode("ascii")}},
        })
        self.assertIn(rejected.status_code,{415,422})
        unchanged=self.client.get("/api/public/branding").json()
        self.assertEqual(unchanged["app_name"],"North Hall")
        self.assertIn("primary",unchanged["assets"])
        reset=self.client.put("/api/settings/branding",json={"app_name":"ignored","primary_color":"#000000","accent_color":"#ffffff","reset_defaults":True})
        self.assertEqual(reset.status_code,200,reset.text)
        self.assertEqual(reset.json()["app_name"],"Workplace NVR")
        self.assertEqual(reset.json()["assets"],{})

    def test_rejected_asset_errors_name_the_asset(self):
        rejected=self.client.put("/api/settings/branding",json={
            "app_name":"North Hall","primary_color":"#101820","accent_color":"#f0a020",
            "assets":{"dark":{"media_type":"image/png","data_base64":base64.b64encode(b"<svg></svg>").decode("ascii")}},
        })
        self.assertIn(rejected.status_code,{415,422})
        self.assertTrue(rejected.json()["detail"].startswith("Dark-theme logo: "),rejected.json())
        oversized=self.client.put("/api/settings/branding",json={
            "app_name":"North Hall","primary_color":"#101820","accent_color":"#f0a020",
            "assets":{"icon":{"media_type":"image/png","data_base64":base64.b64encode(b"x"*(2*1024*1024+1)).decode("ascii")}},
        })
        self.assertEqual(oversized.status_code,413)
        self.assertEqual(oversized.json()["detail"],"Icon: Each branding image must be no larger than 2 MiB")

    def test_operator_and_anonymous_cannot_change_branding(self):
        payload={"app_name":"Attempt","primary_color":"#000000","accent_color":"#ffffff"}
        app.dependency_overrides[current_user]=lambda:{"id":2,"username":"operator","role":"operator"}
        denied=self.client.put("/api/settings/branding",json=payload)
        self.assertEqual(denied.status_code,403)
        app.dependency_overrides.pop(current_user,None)
        try:anonymous=self.client.put("/api/settings/branding",json=payload)
        finally:app.dependency_overrides[current_user]=lambda:self.admin
        self.assertEqual(anonymous.status_code,401)


if __name__=="__main__":
    unittest.main()
