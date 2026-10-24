"""Versioned, forward-only schema migrations.

Each migration module exposes ``NAME`` and ``upgrade(db)``. Migrations run inside a
transaction together with their version record unless the module sets
``TRANSACTIONAL = False`` (the baseline manages its own transactions). A module may
also expose ``verify(db)`` to refuse a result whose shape is not what it expects.

Migrations never run backwards. A database recorded at a version newer than this
release knows about is refused, because an older image cannot safely use a schema
it did not create.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable

from app.migrations import m0001_baseline, m0002_api_tokens, m0003_worker_status, m0004_backups, m0005_user_preferences, m0006_export_retries

logger = logging.getLogger("nvr.migrations")

MIGRATIONS: list[tuple[int, ModuleType]] = [
    (1, m0001_baseline),
    (2, m0002_api_tokens),
    (3, m0003_worker_status),
    (4, m0004_backups),
    (5, m0005_user_preferences),
    (6, m0006_export_retries),
]

PRE_MIGRATION_COPIES_KEPT = 3

# Called with the database path before pending migrations run on a database that
# holds data. Configuration backups register here; a failing hook is logged and the
# local pre-migration copy remains the guarantee.
pre_migration_hooks: list[Callable[[Path], None]] = []


class MigrationError(RuntimeError):
    pass


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def migrate(path: Path, connect: Callable[[], sqlite3.Connection]) -> list[int]:
    """Apply pending migrations and return the versions applied."""
    with connect() as db:
        tables = _user_tables(db)
        applied = _applied_versions(db) if "schema_migrations" in tables else set()
        known = {version for version, _ in MIGRATIONS}
        unknown = sorted(applied - known)
        if unknown:
            raise MigrationError(
                f"The database at {path} was migrated by a newer release (schema version {unknown[-1]}); "
                f"this release supports up to version {latest_version()}. Restore the pre-upgrade "
                "configuration backup or run the newer release."
            )
        pending = [(version, module) for version, module in MIGRATIONS if version not in applied]
        if not pending:
            db.execute("PRAGMA journal_mode = WAL")
            return []

        holds_data = bool(tables - {"schema_migrations"})
        if holds_data and "schema_migrations" not in tables and "users" not in tables:
            raise MigrationError(
                f"The database at {path} is not a recognised NVR database; refusing to modify it."
            )
        copy_path = None
        if holds_data:
            copy_path = _pre_migration_copy(db, path, max(applied, default=0), pending[-1][0])
            for hook in pre_migration_hooks:
                try:
                    hook(path)
                except Exception:  # noqa: BLE001 - the local copy still protects the upgrade
                    logger.exception("Pre-migration hook failed; continuing with the local copy at %s", copy_path)

        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        done: list[int] = []
        for version, module in pending:
            try:
                _apply(db, version, module)
            except Exception as exc:
                where = f"; the pre-migration copy is {copy_path}" if copy_path else ""
                raise MigrationError(
                    f"Schema migration {version} ({module.NAME}) failed: {exc}{where}"
                ) from exc
            logger.info("Applied schema migration %s (%s)", version, module.NAME)
            done.append(version)
        return done


def _apply(db: sqlite3.Connection, version: int, module: ModuleType) -> None:
    record = "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)"
    values = (version, module.NAME, _now())
    if getattr(module, "TRANSACTIONAL", True):
        db.execute("BEGIN IMMEDIATE")
        try:
            module.upgrade(db)
            if hasattr(module, "verify"):
                module.verify(db)
            db.execute(record, values)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return
    module.upgrade(db)
    if hasattr(module, "verify"):
        module.verify(db)
    db.execute(record, values)


def _user_tables(db: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


def _applied_versions(db: sqlite3.Connection) -> set[int]:
    return {row[0] for row in db.execute("SELECT version FROM schema_migrations")}


def _pre_migration_copy(db: sqlite3.Connection, path: Path, from_version: int, to_version: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.pre-migration-v{from_version}-to-v{to_version}-{stamp}")
    copy = sqlite3.connect(target)
    try:
        db.backup(copy)
    finally:
        copy.close()
    os.chmod(target, 0o600)
    copies = sorted(path.parent.glob(f"{path.name}.pre-migration-*"), key=lambda item: item.stat().st_mtime)
    for stale in copies[:-PRE_MIGRATION_COPIES_KEPT]:
        stale.unlink(missing_ok=True)
    logger.info("Saved pre-migration database copy to %s", target)
    return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
