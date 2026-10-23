from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
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


def _reset_site_appearance(db: sqlite3.Connection) -> None:
    db.execute("UPDATE site_branding SET app_name='Workplace NVR',primary_color='#17212b',accent_color='#69a1ff' WHERE singleton_id=1")
    db.execute("DELETE FROM site_branding_assets")


def seed_fresh_install() -> dict[str, object]:
    """An installation before first-run setup: no accounts, default appearance."""
    initialize()
    with connect() as db:
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM api_tokens")
        db.execute("DELETE FROM users")
        _reset_site_appearance(db)
    return _write_manifest({"camera_count": 0, "camera_ids": [], "fresh_install": True})


def _write_manifest(manifest: dict[str, object]) -> dict[str, object]:
    manifest_path = Path(os.environ.get(
        "NVR_BROWSER_FIXTURE_MANIFEST",
        str(Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3")).with_name("fixtures.json")),
    ))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def seed_fixtures(count: int, *, bulk_trigger: bool = False, fail_name_save: bool = False, extra_group: bool = False) -> dict[str, object]:
    initialize()
    with connect() as db:
        _reset_site_appearance(db)
        db.execute("UPDATE users SET theme_preference='system'")
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
                    f"Camera {index:03d}",
                    GROUP_ID if index % 2 else None,
                    ip,
                    "axis" if metadata_known else "auto",
                    encrypt("rtsp://127.0.0.1:8554/nvr-test"),
                    "h264" if metadata_known else None,
                    320 if metadata_known else None,
                    240 if metadata_known else None,
                ),
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
            "primary_camera_id": cameras[0] if cameras else None,
            "bulk_rejected_camera_id": rejected_id,
            "name_save_rejected_camera_id": cameras[0] if fail_name_save and cameras else None,
            "group_id": GROUP_ID,
            "extra_group_id": extra_group_id,
            "discovery_unlinked_id": DISCOVERY_UNLINKED_ID,
            "discovery_linkable_id": DISCOVERY_LINKABLE_ID,
            "discovery_linked_id": DISCOVERY_LINKED_ID if linked_id else None,
        }
    return _write_manifest(manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--bulk-trigger", action="store_true")
    parser.add_argument("--fail-name-save", action="store_true")
    parser.add_argument("--extra-group", action="store_true")
    parser.add_argument("--fresh-install", action="store_true", help="remove every account so first-run setup is required")
    arguments = parser.parse_args()
    if arguments.fresh_install:
        seed_fresh_install()
        return
    if not 0 <= arguments.count <= 200:
        parser.error("camera fixture count must be between 0 and 200")
    seed_fixtures(arguments.count, bulk_trigger=arguments.bulk_trigger, fail_name_save=arguments.fail_name_save, extra_group=arguments.extra_group)


if __name__ == "__main__":
    main()
