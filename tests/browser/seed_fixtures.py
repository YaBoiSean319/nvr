from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
import uuid

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT))

from app.credentials import encrypt  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.store import connect, initialize  # noqa: E402

ADMIN_USERNAME = "camera-admin"
OPERATOR_USERNAME = "camera-operator"
TEST_PASSWORD = Path(os.environ["NVR_BROWSER_FIXTURE_PASSWORD_PATH"]).read_text(encoding="utf-8").strip()
NAMESPACE = uuid.UUID("9b9d4b48-2be7-4adf-a08b-949a7bc27f28")
GROUP_ID = str(uuid.uuid5(NAMESPACE, "group-operations"))
DISCOVERY_UNLINKED_ID = str(uuid.uuid5(NAMESPACE, "discovery-unlinked"))
DISCOVERY_LINKABLE_ID = str(uuid.uuid5(NAMESPACE, "discovery-linkable"))
DISCOVERY_LINKED_ID = str(uuid.uuid5(NAMESPACE, "discovery-linked"))


def _camera_id(index: int) -> str:
    return str(uuid.uuid5(NAMESPACE, f"camera-{index:03d}"))


def _playback_fixture(db: sqlite3.Connection, camera_ids: list[str], *, playback_jobs: bool) -> dict[str, str]:
    recordings = Path(os.environ["NVR_RECORDINGS_PATH"])
    recordings.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nvr-playback-media-") as temporary:
        media = Path(temporary) / "fixture.mp4"
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12", "-t", "12", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "12",
                "-movflags", "+faststart", str(media),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode or not media.is_file():
            raise RuntimeError(f"Could not generate deterministic playable browser fixture: {result.stderr[-500:]}")

        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=3)
        end = start + timedelta(seconds=12)
        for index, camera_id in enumerate(camera_ids):
            relative = f"{camera_id}/playback-fixture.mp4"
            target = recordings / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(media, target)
            cursor = db.execute(
                "INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                (camera_id, relative, start.isoformat(), end.isoformat(), target.stat().st_size),
            )
            if index == 0:
                event_at = start + timedelta(seconds=5)
                db.execute(
                    "INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,actual_start,actual_end,pre_roll_complete,status,segment_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,1,'complete',?)",
                    (
                        str(uuid.uuid5(NAMESPACE, "playback-motion-event")), camera_id, "browser-fixture", "synthetic-motion",
                        event_at.isoformat(), event_at.isoformat(), start.isoformat(), end.isoformat(),
                        start.isoformat(), end.isoformat(), cursor.lastrowid,
                    ),
                )

    if playback_jobs and camera_ids:
        admin_id = db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()[0]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        requested_start, requested_end = start.isoformat(), end.isoformat()
        camera_json = json.dumps([camera_ids[0]])
        display_json = json.dumps({"time_zone": "UTC", "date_format": "friendly", "clock_format": "12"})
        for index in range(12):
            job_id = str(uuid.uuid5(NAMESPACE, f"playback-export-job-{index}"))
            status = "running" if index == 0 else "failed" if index == 1 else "completed"
            created = (now - timedelta(seconds=12 - index)).isoformat()
            db.execute(
                "INSERT INTO exports(id,requested_by,camera_ids_json,output_modes_json,display_settings_json,tile_camera_ids_json,grid_resolution,requested_start,requested_end,status,error,progress,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, admin_id, camera_json, '["individual"]', display_json, camera_json, "1080p",
                    requested_start, requested_end, status,
                    "Synthetic fixture export failure" if status == "failed" else None,
                    48 if status == "running" else 0, created, created,
                ),
            )

    return {"start": start.isoformat(), "end": end.isoformat()}


def seed_fixtures(count: int, *, bulk_trigger: bool = False, fail_name_save: bool = False, extra_group: bool = False,
                  playback: bool = False, playback_jobs: bool = False, archived_playback: bool = False,
                  long_camera_name: bool = False, saved_playback_view: bool = False) -> dict[str, object]:
    initialize()
    with connect() as db:
        users = {str(row["username"]).casefold() for row in db.execute("SELECT username FROM users")}
        if ADMIN_USERNAME.casefold() not in users:
            db.execute(
                "INSERT INTO users(username,password_hash,role) VALUES(?,?, 'administrator')",
                (ADMIN_USERNAME, hash_password(TEST_PASSWORD)),
            )
        if OPERATOR_USERNAME.casefold() not in users:
            db.execute(
                "INSERT INTO users(username,password_hash,role) VALUES(?,?, 'operator')",
                (OPERATOR_USERNAME, hash_password(TEST_PASSWORD)),
            )

        db.execute("DROP TRIGGER IF EXISTS camera_browser_fixture_reject_bulk_update")
        db.execute("DROP TRIGGER IF EXISTS camera_browser_fixture_reject_name_save")
        db.execute("DELETE FROM discovery_candidates")
        db.execute("DELETE FROM exports")
        db.execute("DELETE FROM cameras")
        db.execute("DELETE FROM camera_groups")
        db.execute("DELETE FROM recording_policy_overrides")
        db.execute("DELETE FROM retention_policy_overrides")
        db.execute("INSERT INTO camera_groups(id,name) VALUES(?, 'Operations')", (GROUP_ID,))
        extra_group_id = str(uuid.uuid5(NAMESPACE, "group-weekend")) if extra_group else None
        if extra_group_id:
            db.execute("INSERT INTO camera_groups(id,name) VALUES(?, 'Weekend')", (extra_group_id,))

        cameras: list[str] = []
        for index in range(1, count + 1):
            camera_id = _camera_id(index)
            cameras.append(camera_id)
            ip = None if index == 1 else f"192.0.2.{((index - 1) % 254) + 1}"
            metadata_known = index % 17 != 0
            db.execute(
                "INSERT INTO cameras(id,name,group_id,camera_ip,camera_brand,stream_url_ciphertext,"
                "audio_supported,recording_mode,recording_enabled,recording_status,video_codec,video_width,video_height) "
                "VALUES(?,?,?,?,?,?,NULL,'continuous',0,'stopped',?,?,?)",
                (
                    camera_id,
                    ("Fixture Camera 16 · North Loading Dock Perimeter Camera for Overnight Receiving" if long_camera_name and index == 16 else f"Camera {index:03d}"),
                    GROUP_ID if index % 2 else None,
                    ip,
                    "axis" if metadata_known else "auto",
                    encrypt("rtsp://127.0.0.1:8554/nvr-test"),
                    "h264" if metadata_known else None,
                    320 if metadata_known else None,
                    240 if metadata_known else None,
                ),
            )

        archived_camera_id = None
        if archived_playback:
            archived_camera_id = _camera_id(count + 100)
            db.execute(
                "INSERT INTO cameras(id,name,camera_ip,camera_brand,stream_url_ciphertext,recording_mode,recording_status,archived_at) "
                "VALUES(?,?,NULL,'auto',?,'disabled','archived',?)",
                (archived_camera_id, "Archived Fixture Camera", encrypt("rtsp://127.0.0.1:8554/nvr-test"), "2026-01-01T00:00:00+00:00"),
            )

        playback_manifest = _playback_fixture(db, cameras, playback_jobs=playback_jobs) if playback and cameras else None
        if playback:
            db.execute("UPDATE site_display_settings SET time_zone='UTC',date_format='friendly',clock_format='12' WHERE singleton_id=1")
        if saved_playback_view and len(cameras) >= 2:
            admin_id = db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()[0]
            view_camera_ids = cameras[:2]
            db.execute(
                "INSERT INTO viewer_layouts(user_id,name,camera_ids_json,grid_size,tile_camera_ids_json,layout_mode) "
                "VALUES(?,?,?,4,?,'manual') ON CONFLICT(user_id,name) DO UPDATE SET "
                "camera_ids_json=excluded.camera_ids_json,grid_size=excluded.grid_size,"
                "tile_camera_ids_json=excluded.tile_camera_ids_json,layout_mode=excluded.layout_mode",
                (admin_id, "Playback Fixture View", json.dumps(view_camera_ids),
                 json.dumps([*view_camera_ids, None, None])),
            )

        now = "2026-01-01T00:00:00+00:00"
        linked_id = cameras[0] if cameras else None
        for candidate_id, fingerprint, label in (
            (DISCOVERY_UNLINKED_ID, "ipscan:fixture-unlinked-7ecb", "Unadded Fixture Camera"),
            (DISCOVERY_LINKABLE_ID, "ipscan:fixture-linkable-4d20", "Linkable Fixture Camera"),
        ):
            db.execute(
                "INSERT INTO discovery_candidates(id,device_uuid,service_urls,scopes,capabilities,linked_camera_id,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    fingerprint,
                    json.dumps(["http://127.0.0.1:8766/onvif/device_service"]),
                    json.dumps([f"onvif://www.onvif.org/name/{label}"]),
                    json.dumps({"onvif_discovery": "supported", "video_stream": "untested", "audio": "unknown"}),
                    None,
                    now,
                    now,
                ),
            )
        if linked_id:
            db.execute(
                "INSERT INTO discovery_candidates(id,device_uuid,service_urls,scopes,capabilities,linked_camera_id,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    DISCOVERY_LINKED_ID,
                    "ipscan:fixture-linked-1b42",
                    json.dumps(["http://192.0.2.241/onvif/device_service"]),
                    json.dumps(["onvif://www.onvif.org/name/Already Added Fixture"]),
                    json.dumps({"onvif_discovery": "supported", "video_stream": "supported", "audio": "unknown"}),
                    linked_id,
                    now,
                    now,
                ),
            )

        if bulk_trigger and len(cameras) >= 2:
            rejected_id = cameras[1]
            db.execute(
                "CREATE TRIGGER camera_browser_fixture_reject_bulk_update BEFORE UPDATE ON cameras "
                f"WHEN OLD.id='{rejected_id}' AND NEW.recording_enabled != OLD.recording_enabled "
                "BEGIN SELECT RAISE(ABORT, 'deterministic browser fixture rejects this update'); END"
            )
        else:
            rejected_id = None

        if fail_name_save and cameras:
            db.execute(
                "CREATE TRIGGER camera_browser_fixture_reject_name_save BEFORE UPDATE OF name ON cameras "
                f"WHEN OLD.id='{cameras[0]}' AND NEW.name != OLD.name "
                "BEGIN SELECT RAISE(ABORT, 'deterministic browser fixture rejects this save'); END"
            )

        manifest = {
            "camera_count": count,
            "camera_ids": cameras,
            "archived_camera_id": archived_camera_id,
            "playback": playback_manifest,
            "primary_camera_id": cameras[0] if cameras else None,
            "bulk_rejected_camera_id": rejected_id,
            "name_save_rejected_camera_id": cameras[0] if fail_name_save and cameras else None,
            "group_id": GROUP_ID,
            "extra_group_id": extra_group_id,
            "discovery_unlinked_id": DISCOVERY_UNLINKED_ID,
            "discovery_linkable_id": DISCOVERY_LINKABLE_ID,
            "discovery_linked_id": DISCOVERY_LINKED_ID if linked_id else None,
        }
    manifest_path = Path(os.environ.get(
        "NVR_BROWSER_FIXTURE_MANIFEST",
        str(Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3")).with_name("fixtures.json")),
    ))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--bulk-trigger", action="store_true")
    parser.add_argument("--fail-name-save", action="store_true")
    parser.add_argument("--extra-group", action="store_true")
    parser.add_argument("--playback", action="store_true")
    parser.add_argument("--playback-jobs", action="store_true")
    parser.add_argument("--archived-playback", action="store_true")
    parser.add_argument("--long-camera-name", action="store_true")
    parser.add_argument("--saved-playback-view", action="store_true")
    arguments = parser.parse_args()
    if not 0 <= arguments.count <= 200:
        parser.error("camera fixture count must be between 0 and 200")
    seed_fixtures(
        arguments.count, bulk_trigger=arguments.bulk_trigger, fail_name_save=arguments.fail_name_save,
        extra_group=arguments.extra_group, playback=arguments.playback,
        playback_jobs=arguments.playback_jobs, archived_playback=arguments.archived_playback,
        long_camera_name=arguments.long_camera_name, saved_playback_view=arguments.saved_playback_view,
    )


if __name__ == "__main__":
    main()
