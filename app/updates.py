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
import logging
import os
import re
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
STALE_AFTER = timedelta(hours=24)  # A check older than this is out of date: the page asks again when it is opened
IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._/:-]{0,200}$")
logger = logging.getLogger(__name__)
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
    """The image the commands name: the setting if it is a plain image name (it ends up in commands people paste into a shell), else the default."""
    name = os.environ.get("NVR_UPDATE_IMAGE", DEFAULT_IMAGE).strip()
    return name if IMAGE_NAME.match(name) else DEFAULT_IMAGE


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
                    raise CheckFailed(f"The release feed answered {response.status_code}" + (" (a redirect: use the final address)" if 300 <= response.status_code < 400 else ""))
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_FEED_BYTES:
                        raise CheckFailed("The release feed is too large")
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except httpx.HTTPError as exc:
        raise CheckFailed("The release feed could not be reached") from exc
    except (ValueError, RecursionError) as exc:
        raise CheckFailed("The release feed is not valid JSON") from exc


# --- state ---------------------------------------------------------------------------------------------------------------------------

def _settings(db: sqlite3.Connection) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO update_settings(singleton_id) VALUES(1)")
    return db.execute("SELECT * FROM update_settings WHERE singleton_id=1").fetchone()


def checking_allowed(db: sqlite3.Connection) -> bool:
    return not disabled_by_environment() and bool(_settings(db)["check_enabled"])


def _has_migrations(schema_version: int | None) -> bool | None:
    """Whether a release changes the database: None when the release does not say (the page says so, it does not guess)."""
    return None if schema_version is None else schema_version > latest_version()


def _release_view(release: Release) -> dict[str, object]:
    return {"version": release.number, "tag": release.tag, "notes": release.notes, "url": release.url, "published_at": release.published_at,
            "image": f"{image_repository()}:{release.number}", "schema_version": release.schema_version}


def _latest(row: sqlite3.Row) -> dict[str, object] | None:
    """The release the last check found, if it is still newer than this one (an upgrade makes it old news) and well formed."""
    try:
        found = json.loads(row["latest_json"]) if row["latest_json"] else None
        version = parse_version(found["version"]) if found else None
        if found is None or version is None or version <= current_version():
            return None
        schema = found.get("schema_version")
        found["schema_version"] = schema if isinstance(schema, int) and not isinstance(schema, bool) else None
        found["has_migrations"] = _has_migrations(found["schema_version"])  # Against today's schema: it may have changed since the check
        return found
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


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
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not checking_allowed(db):  # Switched off while the feed was being read: nothing is kept
                raise UpdatesDisabled("The update check is turned off")
            if error is None:
                db.execute("UPDATE update_settings SET last_checked_at=?,last_error=NULL,latest_json=? WHERE singleton_id=1", (_now(), json.dumps(_release_view(found)) if found else None))
            else:
                db.execute("UPDATE update_settings SET last_checked_at=?,last_error=? WHERE singleton_id=1", (_now(), error))
            record(db, actor_id, "update_checked", "settings", "updates", found=found.number if found else None, error=error)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
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
    checked = _parse_time(row["last_checked_at"])
    return {
        "version": APP_VERSION, "schema_version": latest_version(), "image": f"{image_repository()}:{APP_VERSION}",
        "check_enabled": bool(row["check_enabled"]), "disabled_by_environment": disabled_by_environment(), "checking": allowed,
        "last_checked_at": row["last_checked_at"], "last_error": row["last_error"], "stale": checked is None or datetime.now(timezone.utc) - checked > STALE_AFTER,
        "available": _latest(row), "prepared": prepared_view(row), "history": history, "applied_migrations": applied,
        "rollback": last_upgrade_rollback(history),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


# --- what changed at the last start ---------------------------------------------------------------------------------------------------

def note_start() -> None:
    """At startup: when the version or schema is not what the last start saw, write what changed to the history, so Settings can say what the upgrade applied.
    Never stops the application from starting."""
    try:
        _note_start()
    except sqlite3.Error:
        logger.exception("Could not record the version this start is running")


def _note_start() -> None:
    schema = latest_version()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = _settings(db)
            before_version, before_schema = row["seen_version"], row["seen_schema"]
            if before_version is not None and (before_version != APP_VERSION or before_schema != schema):
                applied = [{"version": item["version"], "name": item["name"]} for item in
                           db.execute("SELECT version,name FROM schema_migrations WHERE version>? ORDER BY version", (before_schema or 0,))]
                db.execute("INSERT INTO update_history(at,from_version,to_version,from_schema,to_schema,migrations_json) VALUES(?,?,?,?,?,?)",
                           (_now(), before_version, APP_VERSION, before_schema, schema, json.dumps(applied)))
                record(db, None, "version_changed", "settings", "updates", from_version=before_version, to_version=APP_VERSION, migrations=[item["version"] for item in applied])
                db.execute("UPDATE update_settings SET prepared_at=NULL,prepared_run_id=NULL,prepared_version=NULL,prepared_without_backup=0 WHERE singleton_id=1")  # That update is done
            db.execute("UPDATE update_settings SET seen_version=?,seen_schema=? WHERE singleton_id=1", (APP_VERSION, schema))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


# --- the guided steps ----------------------------------------------------------------------------------------------------------------

def prepared_view(row: sqlite3.Row) -> dict[str, object] | None:
    if row["prepared_at"] is None:
        return None
    return {"at": row["prepared_at"], "run_id": row["prepared_run_id"], "without_backup": bool(row["prepared_without_backup"]), "version": row["prepared_version"]}


def prepare(actor_id: int, *, without_backup: bool = False) -> dict[str, object]:
    """Start the pre-update backup (or, when backups are not set up and the administrator says so, go on without one) for the release found by the last check."""
    with connect() as db:
        target = _latest(_settings(db))
    if target is None:
        raise PlanNotReady("There is no newer release to update to")
    reason = backup.disabled_reason()
    run: dict[str, object] | None = None
    if reason:
        if not without_backup:
            raise backup.BackupError(f"{reason}. Set the backup up first, or choose to go on without a backup.")
    else:
        run = backup.queue_backup(actor_id)
    with connect() as db:
        db.execute("UPDATE update_settings SET prepared_at=?,prepared_run_id=?,prepared_version=?,prepared_without_backup=? WHERE singleton_id=1",
                   (_now(), run["id"] if run else None, target["version"], int(run is None)))
        record(db, actor_id, "update_prepared", "settings", "updates", version=target["version"], backup_run=run["id"] if run else None, without_backup=run is None)
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
        target = _latest(row)
        run = db.execute("SELECT * FROM backup_runs WHERE id=?", (row["prepared_run_id"],)).fetchone() if row["prepared_run_id"] else None
    if row["prepared_at"] is None:
        raise PlanNotReady("Start the update first: the backup comes before the steps")
    if target is None:
        raise PlanNotReady("There is no newer release to update to")
    if target["version"] != row["prepared_version"]:
        raise PlanNotReady(f"Version {target['version']} was found after the backup was started; start again so the backup is taken for it")
    if run is None and not row["prepared_without_backup"]:
        raise PlanNotReady("The pre-update backup is missing; start again")
    if run is not None:
        if run["status"] == "failed":
            raise PlanNotReady(f"The pre-update backup failed: {run['error'] or 'see Backups'}")
        if run["status"] != "succeeded":
            raise PlanNotReady("The pre-update backup is still running")
        if not (backup.destination() / str(run["file_name"])).is_file():
            raise PlanNotReady("The pre-update backup file is not where it was written; start again")
    return {
        "target": target,
        "backup": backup.run_view(run) if run is not None else None,
        "backup_destination": str(backup.destination()) if run is not None else None,
        "without_backup": run is None,
        "upgrade": upgrade_commands(str(target["version"])),
        "rollback": rollback_steps(APP_VERSION, None, target["has_migrations"] is not False),
        "pre_migration_copies": pre_migration_copies(),
        "will_migrate": target["has_migrations"],
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


def last_upgrade_rollback(history: list[dict[str, object]]) -> dict[str, object] | None:
    """How to undo the upgrade this installation last went through: the previous image and the database copy made just before that upgrade's migrations."""
    if not history:
        return None
    last = history[0]
    wanted = [copy for copy in pre_migration_copies() if copy["to_schema"] == last["to_schema"] and (last["from_schema"] is None or copy["from_schema"] == last["from_schema"])]
    return rollback_steps(str(last["from_version"]), wanted[0] if wanted else None, bool(last["migrations"]))


def rollback_steps(previous_version: str, copy: dict[str, object] | None, migrates: bool) -> dict[str, object]:
    """How to go back: the previous image, and when the upgrade changed the database, the database as it was before (``copy``, the one made for that upgrade)."""
    repository = image_repository()
    database = database_path()
    quote = shlex.quote
    steps = ["# Run these in the same folder, on the host.", "docker compose stop nvr"]
    if copy:
        script = f"rm -f {quote(f'{database}-wal')} {quote(f'{database}-shm')} && cp {quote(str(copy['path']))} {quote(str(database))}"
        steps += [f"# Put back the database copy made just before that upgrade's migrations ({copy['file']}, schema v{copy['from_schema']} to v{copy['to_schema']}):",
                  f"docker compose run --rm --no-deps --entrypoint sh nvr -c {quote(script)}"]
    elif migrates:
        steps += ["# This upgrade changes the database. The copy of the database made just before its migrations does not exist yet: the new version makes it when it first starts,",
                  "# and Settings, Updates then names it here. Until then the pre-update backup above is the way back."]
    steps += [f"# In compose.yaml set the previous image back:   image: {repository}:{previous_version}", "docker compose up -d nvr"]
    return {"steps": steps, "copy": copy["file"] if copy else None, "has_copy": copy is not None, "previous_image": f"{repository}:{previous_version}", "will_migrate": migrates}
