"""Bulk camera adding runs as a job on the server (#173).

A job is a list of cameras to check and add, each with its own status. The browser used to drive the list one camera at a time, so closing the page
ended the run and lost its progress; the job lives in the database and is worked through by the web process, so the page can be closed, and
reopened, while it runs. A camera's password is kept encrypted only until its item is finished, and is cleared when the item ends (or the job
is cancelled).
"""
from __future__ import annotations

import sqlite3

NAME = "camera_add_jobs"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS camera_add_jobs ("
        "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, created_by INTEGER, state TEXT NOT NULL DEFAULT 'running' "
        "CHECK (state IN ('running','done','cancelled')), add_anyway INTEGER NOT NULL DEFAULT 1, recording INTEGER NOT NULL DEFAULT 0, finished_at TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS camera_add_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES camera_add_jobs(id) ON DELETE CASCADE, row_number INTEGER NOT NULL, "
        "host TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', username TEXT, password_ciphertext BLOB, "
        "status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','checking','adding','added','attention','failed','skipped')), "
        "detail TEXT NOT NULL DEFAULT '', camera_id TEXT, updated_at TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS camera_add_items_job ON camera_add_items(job_id, row_number)")
    db.execute("CREATE INDEX IF NOT EXISTS camera_add_items_queue ON camera_add_items(status) WHERE status IN ('queued','checking','adding')")
