from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
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


def _reset_site_appearance(db: sqlite3.Connection) -> None:
    db.execute("UPDATE site_branding SET app_name='Workplace NVR',primary_color='#17212b',accent_color='#69a1ff' WHERE singleton_id=1")
    db.execute("DELETE FROM site_branding_assets")
    db.execute("UPDATE site_display_settings SET time_zone='UTC',date_format='friendly',clock_format='12' WHERE singleton_id=1")


LONG_NAME_SUFFIX = " - North loading dock exterior entrance overlooking the delivery yard"
FOOTAGE_CLIP_SECONDS = 60


def _footage_clip(fps: int) -> Path:
    """One generated minute of video at `fps`, reused for every fixture interval at that rate."""
    recordings = Path(os.environ["NVR_RECORDINGS_PATH"])
    clip = recordings.parent / "fixture-footage" / f"clip-{FOOTAGE_CLIP_SECONDS}s-{fps}fps.mp4"
    if not clip.is_file():
        clip.parent.mkdir(parents=True, exist_ok=True)
        partial = clip.with_suffix(".partial.mp4")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", f"testsrc=s=320x180:r={fps}:d={FOOTAGE_CLIP_SECONDS}", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-g", str(fps), "-movflags", "+faststart", str(partial),
        ], check=True, timeout=120)
        partial.replace(clip)
    return clip


def _seed_footage(db: sqlite3.Connection, cameras: list[str]) -> dict[str, object]:
    """Recorded intervals relative to a recent anchor, so the default past-hour search finds them.

    Offsets in seconds from the anchor:
    Camera 1 (15 fps): 0-60 and 150-210, a failure gap between them; motion at 40 and 160.
    Camera 2 (10 fps): 30-90; motion at 40 (simultaneous with camera 1) and 75; a person
    detection at 50 and an unanalyzed AI interval 60-90.
    90-150 is empty on both cameras. Every other camera has no footage.
    """
    clips = {0: _footage_clip(15), 1: _footage_clip(10)}
    anchor = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(second=0, microsecond=0)
    at = lambda seconds: (anchor + timedelta(seconds=seconds)).isoformat()
    plan = {0: [0, 150], 1: [30]}
    recordings = Path(os.environ["NVR_RECORDINGS_PATH"])
    segments: list[dict[str, object]] = []
    for index, offsets in plan.items():
        if index >= len(cameras):
            continue
        camera_id = cameras[index]
        for offset in offsets:
            relative = f"{camera_id}/fixture-{offset:04d}.mp4"
            target = recordings / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(clips[index], target)
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                       (camera_id, relative, at(offset), at(offset + FOOTAGE_CLIP_SECONDS), target.stat().st_size))
            segments.append({"camera_id": camera_id, "starts_at": at(offset), "ends_at": at(offset + FOOTAGE_CLIP_SECONDS)})
    first = cameras[0]
    db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at,ends_at) VALUES(?,?,?,?,?)",
               (first, "failure", "Camera offline", at(60), at(150)))

    def motion(camera_id: str, seconds: int) -> None:
        db.execute("INSERT INTO motion_events(id,camera_id,source,source_topic,source_event_at,received_at,requested_start,requested_end,status) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (str(uuid.uuid5(NAMESPACE, f"motion-{camera_id}-{seconds}")), camera_id, "onvif", "motion", at(seconds), at(seconds),
                    at(seconds - 5), at(seconds + 10), "complete"))

    motion(first, 40)
    motion(first, 160)
    if len(cameras) > 1:
        second = cameras[1]
        motion(second, 40)
        motion(second, 75)
        db.execute("INSERT INTO ai_observations(id,camera_id,label,confidence,box_json,first_seen_at,last_seen_at,frame_width,frame_height,model_id,model_version) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (str(uuid.uuid5(NAMESPACE, "person-1")), second, "person", 0.9, "[0.2,0.2,0.4,0.6]", at(50), at(52), 320, 180, "fixture", "1"))
        db.execute("INSERT INTO ai_coverage(camera_id,state,starts_at,ends_at,detail) VALUES(?,?,?,?,?)",
                   (second, "failed", at(60), at(90), "Detector unavailable"))
    return {"anchor": anchor.isoformat(), "segments": segments}


def _seed_detections(db: sqlite3.Connection, camera_id: str) -> dict[str, object]:
    """Store the detection fixture's scene on a camera, as analysed an hour ago."""
    from app.tracks import Tracker, record_detections
    from tests.fixtures import detection_scene as scene

    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    detector, tracker = scene.SceneDetector(), Tracker()
    for index, at in enumerate(scene.frame_times(start)):
        frame = scene.frame(index)
        record_detections(db, camera_id, detector.infer(frame, 0.5), at, tracker, frame=frame,
                          model_id="fixture-scene", model_version="1", provider="CPU")
    return {"camera_id": camera_id, "scene_start": start.isoformat()}


def _seed_alarm(db: sqlite3.Connection, camera_id: str, anchor: str) -> dict[str, object]:
    """A new alarm from a gate rule on camera 1, 30 seconds into its first recorded interval."""
    from app.alarms import create_alarm

    rule_id = str(uuid.uuid5(NAMESPACE, "rule-gate"))
    occurred = (datetime.fromisoformat(anchor) + timedelta(seconds=30)).isoformat()
    db.execute(
        "INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
        (rule_id, "Gate entry", json.dumps([camera_id]), "zone_entry", json.dumps({"zone": {"x": 0, "y": 0.6, "width": 0.3, "height": 0.4}}), '["vehicle"]', anchor),
    )
    event_id = str(uuid.uuid5(NAMESPACE, "rule-event-gate"))
    db.execute(
        "INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, rule_id, "Gate entry", camera_id, "fixture-track", "vehicle", "zone_entry", occurred, '{"x":0.1,"y":0.7,"width":0.2,"height":0.15}'),
    )
    return {"alarm_id": create_alarm(db, event_id), "rule_id": rule_id, "occurred_at": occurred}


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


def seed_fixtures(count: int, *, bulk_trigger: bool = False, fail_name_save: bool = False, extra_group: bool = False,
                  streams: dict[int, str] | None = None, audio_cameras: set[int] | None = None,
                  footage: bool = False, long_names: bool = False, detections: bool = False, alarm: bool = False) -> dict[str, object]:
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
        # Settings state from earlier journeys.
        for table in ("api_tokens", "webhook_deliveries", "webhook_endpoints", "heartbeat_configuration", "backup_runs", "diagnostic_settings"):
            db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE ai_runtime_settings SET max_active_cameras=4 WHERE singleton_id=1")
        db.execute("DELETE FROM export_sources")
        db.execute("DELETE FROM exports")
        db.execute("DELETE FROM rules")
        for table in ("push_deliveries", "push_subscriptions", "push_preferences"):
            db.execute(f"DELETE FROM {table}")
        db.execute("UPDATE alarm_settings SET resolved_footage_days=30 WHERE singleton_id=1")
        db.execute("DELETE FROM live_workspace_states")
        db.execute("DELETE FROM viewer_layouts")
        db.execute("DELETE FROM shared_viewer_layouts")
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
                    f"Camera {index:03d}{LONG_NAME_SUFFIX if long_names else ''}",
                    GROUP_ID if index % 2 else None,
                    ip,
                    "axis" if metadata_known else "auto",
                    encrypt((streams or {}).get(index, "rtsp://127.0.0.1:8554/nvr-test")),
                    "h264" if metadata_known else None,
                    320 if metadata_known else None,
                    240 if metadata_known else None,
                ),
            )

            if index in (audio_cameras or set()):
                db.execute("UPDATE cameras SET audio_supported=1,audio_enabled=1 WHERE id=?", (camera_id,))

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
            "footage": _seed_footage(db, cameras) if (footage or alarm) and cameras else None,
            "detections": _seed_detections(db, cameras[0]) if detections and cameras else None,
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
        if alarm and cameras:
            manifest["alarm"] = _seed_alarm(db, cameras[0], manifest["footage"]["anchor"])
    return _write_manifest(manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--bulk-trigger", action="store_true")
    parser.add_argument("--fail-name-save", action="store_true")
    parser.add_argument("--extra-group", action="store_true")
    parser.add_argument("--fresh-install", action="store_true", help="remove every account so first-run setup is required")
    parser.add_argument("--stream", action="append", default=[], metavar="INDEX=URL", help="give camera INDEX this RTSP URL")
    parser.add_argument("--audio", action="append", type=int, default=[], metavar="INDEX", help="enable audio on camera INDEX")
    parser.add_argument("--footage", action="store_true", help="give cameras 1 and 2 generated recorded intervals in the past hour")
    parser.add_argument("--long-names", action="store_true", help="give every camera a long name")
    parser.add_argument("--detections", action="store_true", help="store the detection fixture's scene on camera 1, an hour ago")
    parser.add_argument("--alarm", action="store_true", help="give camera 1 footage and a new alarm 30 seconds into it")
    arguments = parser.parse_args()
    if arguments.fresh_install:
        seed_fresh_install()
        return
    if not 0 <= arguments.count <= 200:
        parser.error("camera fixture count must be between 0 and 200")
    streams = {int(index): url for index, url in (item.split("=", 1) for item in arguments.stream)}
    seed_fixtures(arguments.count, bulk_trigger=arguments.bulk_trigger, fail_name_save=arguments.fail_name_save, extra_group=arguments.extra_group,
                  streams=streams, audio_cameras=set(arguments.audio), footage=arguments.footage, long_names=arguments.long_names,
                  detections=arguments.detections, alarm=arguments.alarm)


if __name__ == "__main__":
    main()
