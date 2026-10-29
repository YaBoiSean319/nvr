"""Guided updates (#148): is a newer release out, what changed, and the host commands to upgrade or roll back.

This module only ever *tells* an administrator what to run on the host. It never pulls an image, restarts a container or touches Docker (the container has no Docker
socket, and there is no route that could ask for one). The release feed is the repository's published releases (GitHub's releases JSON by default, set by
``NVR_UPDATE_FEED_URL``; the image they point at is ``NVR_UPDATE_IMAGE``). A release's notes may carry a machine-readable line saying which schema version it needs:

    <!-- nvr-release: {"schema_version": 44} -->

The check is opt-out: ``NVR_UPDATE_CHECK=false`` in the environment, or the switch in Settings, and then no request is made at all.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app import backup
from app.audit import record
from app.migrations import latest_version
from app.store import connect, database_path
from app.version import APP_VERSION

DEFAULT_FEED = "https://api.github.com/repos/yaboisean319/nvr/releases?per_page=30"
DEFAULT_IMAGE = "ghcr.io/yaboisean319/nvr"
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_NOTES_CHARS = 20_000
CHECK_TIMEOUT = httpx.Timeout(10, connect=5)
STALE_AFTER_HOURS = 24
MARKER = re.compile(r"<!--\s*nvr-release:\s*(\{.*?\})\s*-->", re.DOTALL)
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdatesDisabled(RuntimeError):
    """The update check is turned off, so nothing was requested."""


class CheckFailed(RuntimeError):
    """The release feed could not be read."""


class PlanNotReady(RuntimeError):
    """The upgrade steps are not offered yet."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, int, int]
    notes: str
    url: str
    published_at: str
    schema_version: int | None

    @property
    def number(self) -> str:
        return ".".join(str(part) for part in self.version)


def image_repository() -> str:
    return os.environ.get("NVR_UPDATE_IMAGE", DEFAULT_IMAGE).strip() or DEFAULT_IMAGE


def feed_url() -> str:
    return os.environ.get("NVR_UPDATE_FEED_URL", DEFAULT_FEED).strip() or DEFAULT_FEED


def disabled_by_environment() -> bool:
    return os.environ.get("NVR_UPDATE_CHECK", "true").strip().lower() in {"0", "false", "no", "off"}


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = VERSION.match(text.strip())
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def current_version() -> tuple[int, int, int]:
    return parse_version(APP_VERSION) or (0, 0, 0)


def parse_release(item: object) -> Release | None:
    """One entry of the feed as a release, or None for a draft, a pre-release or anything that is not a plain x.y.z release."""
    if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
        return None
    tag = item.get("tag_name")
    version = parse_version(tag) if isinstance(tag, str) else None
    if version is None:
        return None
    body = item.get("body") if isinstance(item.get("body"), str) else ""
    schema = None
    marker = MARKER.search(body)
    if marker:
        try:
            value = json.loads(marker.group(1)).get("schema_version")
            schema = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        except (ValueError, AttributeError):
            schema = None
    url = item.get("html_url") if isinstance(item.get("html_url"), str) and item["html_url"].startswith("https://") else ""
    published = item.get("published_at") if isinstance(item.get("published_at"), str) else ""
    return Release(tag, version, MARKER.sub("", body).strip()[:MAX_NOTES_CHARS], url, published, schema)


def newest_release(items: object, current: tuple[int, int, int] | None = None) -> Release | None:
    """The newest release in the feed that is newer than this one."""
    current = current or current_version()
    releases = [release for release in (parse_release(item) for item in (items if isinstance(items, list) else [])) if release is not None]
    newer = [release for release in releases if release.version > current]
    return max(newer, key=lambda release: release.version, default=None)


def _feed_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    try:  # Plain http only to this machine, for a feed mirror or a test
        return parts.scheme == "http" and parts.hostname is not None and (parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback)
    except ValueError:
        return False


def fetch_feed(url: str | None = None) -> object:
    address = url or feed_url()
    if not _feed_allowed(address):
        raise CheckFailed("The release feed address must be https")
    try:
        with httpx.Client(timeout=CHECK_TIMEOUT, trust_env=False, follow_redirects=False, headers={"Accept": "application/vnd.github+json", "User-Agent": f"NVR/{APP_VERSION}"}) as client:
            with client.stream("GET", address) as response:
                if response.status_code != 200:
                    raise CheckFailed(f"The release feed answered {response.status_code}")
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_FEED_BYTES:
                        raise CheckFailed("The release feed is too large")
        return json.loads(body)
    except httpx.HTTPError as exc:
        raise CheckFailed("The release feed could not be reached") from exc
    except ValueError as exc:
        raise CheckFailed("The release feed is not valid JSON") from exc


# --- state ---------------------------------------------------------------------------------------------------------------------------

def _settings(db: sqlite3.Connection) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO update_settings(singleton_id) VALUES(1)")
    return db.execute("SELECT * FROM update_settings WHERE singleton_id=1").fetchone()


def checking_allowed(db: sqlite3.Connection) -> bool:
    return not disabled_by_environment() and bool(_settings(db)["check_enabled"])


def _release_view(release: Release, schema_now: int) -> dict[str, object]:
    return {
        "version": release.number, "tag": release.tag, "notes": release.notes, "url": release.url, "published_at": release.published_at,
        "image": f"{image_repository()}:{release.number}", "schema_version": release.schema_version,
        # None when the release does not say: the page says "not stated" rather than guessing
        "has_migrations": None if release.schema_version is None else release.schema_version > schema_now,
    }


def set_check_enabled(db: sqlite3.Connection, actor_id: int, enabled: bool) -> None:
    _settings(db)
    db.execute("UPDATE update_settings SET check_enabled=? WHERE singleton_id=1", (int(enabled),))
    record(db, actor_id, "update_check_changed", "settings", "updates", enabled=enabled)


def check(actor_id: int | None = None) -> dict[str, object]:
    """Read the release feed now. Raises UpdatesDisabled, without any request, when the check is off."""
    with connect() as db:
        if not checking_allowed(db):
            raise UpdatesDisabled("The update check is turned off")
    error: str | None = None
    found: Release | None = None
    try:
        found = newest_release(fetch_feed())
    except CheckFailed as exc:
        error = str(exc)
    now = _now()
    with connect() as db:
        _settings(db)
        if error is None:
            db.execute("UPDATE update_settings SET last_checked_at=?,last_error=NULL,latest_json=? WHERE singleton_id=1",
                       (now, json.dumps(_release_view(found, latest_version())) if found else None))
        else:
            db.execute("UPDATE update_settings SET last_checked_at=?,last_error=? WHERE singleton_id=1", (now, error))
        record(db, actor_id, "update_checked", "settings", "updates", found=found.number if found else None, error=error)
    return state()


def state() -> dict[str, object]:
    with connect() as db:
        row = _settings(db)
        history = [{"at": item["at"], "from_version": item["from_version"], "to_version": item["to_version"], "from_schema": item["from_schema"],
                    "to_schema": item["to_schema"], "migrations": json.loads(item["migrations_json"])}
                   for item in db.execute("SELECT * FROM update_history ORDER BY id DESC LIMIT 10")]
        applied = [{"version": item["version"], "name": item["name"], "applied_at": item["applied_at"]}
                   for item in db.execute("SELECT version,name,applied_at FROM schema_migrations ORDER BY version DESC LIMIT 10")]
        allowed = checking_allowed(db)
    latest = json.loads(row["latest_json"]) if row["latest_json"] else None
    if latest is not None:  # Recomputed against today's schema: it may have changed since the check
        latest["has_migrations"] = None if latest["schema_version"] is None else latest["schema_version"] > latest_version()
    return {
        "version": APP_VERSION, "schema_version": latest_version(), "image": f"{image_repository()}:{APP_VERSION}",
        "check_enabled": bool(row["check_enabled"]), "disabled_by_environment": disabled_by_environment(), "checking": allowed,
        "last_checked_at": row["last_checked_at"], "last_error": row["last_error"], "available": latest,
        "prepared": prepared_view(row), "history": history, "applied_migrations": applied,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- what changed at the last start ---------------------------------------------------------------------------------------------------

def note_start() -> None:
    """At startup: when the version or schema is not what the last start saw, write what happened to the history (so Settings can say what the upgrade applied)."""
    schema = latest_version()
    with connect() as db:
        row = _settings(db)
        before_version, before_schema = row["seen_version"], row["seen_schema"]
        if before_version is not None and (before_version != APP_VERSION or before_schema != schema):
            applied = [{"version": item["version"], "name": item["name"]} for item in
                       db.execute("SELECT version,name FROM schema_migrations WHERE version>? ORDER BY version", (before_schema or 0,))]
            db.execute("INSERT INTO update_history(at,from_version,to_version,from_schema,to_schema,migrations_json) VALUES(?,?,?,?,?,?)",
                       (_now(), before_version, APP_VERSION, before_schema, schema, json.dumps(applied)))
            record(db, None, "upgrade_detected", "settings", "updates", from_version=before_version, to_version=APP_VERSION, migrations=[item["version"] for item in applied])
        db.execute("UPDATE update_settings SET seen_version=?,seen_schema=? WHERE singleton_id=1", (APP_VERSION, schema))


# --- the guided steps ----------------------------------------------------------------------------------------------------------------

def prepared_view(row: sqlite3.Row) -> dict[str, object] | None:
    if row["prepared_at"] is None:
        return None
    return {"at": row["prepared_at"], "run_id": row["prepared_run_id"], "without_backup": bool(row["prepared_without_backup"])}


def prepare(actor_id: int, *, without_backup: bool = False) -> dict[str, object]:
    """Start the pre-update backup (or, when backups are not set up and the administrator says so, go on without one)."""
    with connect() as db:
        _settings(db)
    reason = backup.disabled_reason()
    run: dict[str, object] | None = None
    if reason:
        if not without_backup:
            raise backup.BackupError(f"{reason}. Set the backup up first, or choose to go on without a backup.")
    else:
        run = backup.queue_backup(actor_id)
    with connect() as db:
        db.execute("UPDATE update_settings SET prepared_at=?,prepared_run_id=?,prepared_without_backup=? WHERE singleton_id=1",
                   (_now(), run["id"] if run else None, int(run is None)))
        record(db, actor_id, "update_prepared", "settings", "updates", backup_run=run["id"] if run else None, without_backup=run is None)
    return {"run": run}


def pre_migration_copies() -> list[dict[str, object]]:
    """The database copies the NVR made just before applying migrations, newest first."""
    path = database_path()
    copies = []
    for item in path.parent.glob(f"{path.name}.pre-migration-*"):
        match = re.search(r"pre-migration-v(\d+)-to-v(\d+)-(\d{8}T\d{6})", item.name)
        try:
            info = item.stat()
        except OSError:
            continue
        copies.append({"file": item.name, "path": str(item), "from_schema": int(match.group(1)) if match else None, "to_schema": int(match.group(2)) if match else None,
                       "size_bytes": info.st_size, "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")})
    return sorted(copies, key=lambda item: item["modified_at"], reverse=True)


def plan() -> dict[str, object]:
    """The backup that was just taken and the exact host commands, once the backup is done. Raises PlanNotReady until then."""
    with connect() as db:
        row = _settings(db)
        latest = json.loads(row["latest_json"]) if row["latest_json"] else None
        run = db.execute("SELECT * FROM backup_runs WHERE id=?", (row["prepared_run_id"],)).fetchone() if row["prepared_run_id"] else None
    if row["prepared_at"] is None:
        raise PlanNotReady("Start the update first: the backup comes before the steps")
    if latest is None:
        raise PlanNotReady("There is no newer release to update to")
    if run is None and not row["prepared_without_backup"]:
        raise PlanNotReady("The pre-update backup is missing; start again")
    if run is not None and run["status"] != "succeeded":
        raise PlanNotReady(f"The pre-update backup failed: {run['error'] or 'see Backups'}" if run["status"] == "failed" else "The pre-update backup is still running")
    copies = pre_migration_copies()
    return {
        "target": latest,
        "backup": backup._run_view(run) if run is not None else None,
        "backup_destination": str(backup.destination()) if run is not None else None,
        "without_backup": run is None,
        "upgrade": upgrade_commands(str(latest["version"])),
        "rollback": rollback_steps(APP_VERSION, copies),
        "pre_migration_copies": copies,
        "will_migrate": latest["has_migrations"],
        "note": "These commands are for you to run on the machine that runs the NVR. The NVR cannot run them and has no access to Docker.",
    }


def upgrade_commands(version: str) -> list[str]:
    repository = image_repository()
    return [
        "# Run these in the folder that holds the NVR's compose.yaml, on the host.",
        "docker compose images nvr                       # note the image you are replacing",
        f"# In compose.yaml set:   image: {repository}:{version}",
        "docker compose pull nvr",
        "docker compose up -d nvr",
        "docker compose logs --tail 50 nvr               # the migrations applied are logged here and then listed in Settings, Updates",
    ]


def rollback_steps(previous_version: str, copies: list[dict[str, object]]) -> dict[str, object]:
    """How to go back: the previous image, and the database as it was before the migrations."""
    repository = image_repository()
    newest = copies[0] if copies else None
    copy_name = newest["file"] if newest else "<the pre-migration copy listed in Settings, Updates after the upgrade>"
    database = database_path()
    steps = [
        "# Run these in the same folder, on the host.",
        "docker compose stop nvr",
    ]
    if newest:
        steps += [
            f"# Put back the database copy made just before the migrations ({newest['file']}, schema v{newest['from_schema']} to v{newest['to_schema']}):",
            f"docker compose run --rm --no-deps --entrypoint sh nvr -c 'rm -f {database}-wal {database}-shm && cp {newest['path']} {database}'",
        ]
    else:
        steps += [
            "# No pre-migration copy exists yet: one is made automatically when the new version first starts and applies migrations, and then named here.",
            "# Until then, the pre-update backup above is the way back (see the restore steps in the backup documentation).",
        ]
    steps += [
        f"# In compose.yaml set the previous image back:   image: {repository}:{previous_version}",
        "docker compose up -d nvr",
    ]
    return {"steps": steps, "copy": copy_name, "previous_image": f"{repository}:{previous_version}", "has_copy": newest is not None}
