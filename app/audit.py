"""One place that writes the audit log."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def record(db: sqlite3.Connection, actor_id: int | None, action: str, resource_type: str, resource_id: str | int, **details: Any) -> None:
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(?,?,?,?,?)",
               (actor_id, action, resource_type, str(resource_id), json.dumps(details, sort_keys=True)))
