"""Who may see which camera.

Administrators see every camera. An operator sees the cameras their grants give them: a
grant links a user, or everyone holding an Entra role, to every camera or to one camera
group, optionally until an expiry. A grant that has expired gives nothing, from that moment.
An operator with no live grant sees no cameras.

``resolve`` answers once per request (the principal carries the result). Code that hands
camera data to a person outside a request, such as push notifications, asks ``may_view_camera``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class Access:
    """The cameras and camera groups a person may see. ``None`` means every one."""

    cameras: frozenset[str] | None = None
    groups: frozenset[str] | None = None

    @property
    def unrestricted(self) -> bool:
        return self.cameras is None

    def can_see(self, camera_id: str) -> bool:
        return self.cameras is None or camera_id in self.cameras

    def can_see_group(self, group_id: str) -> bool:
        return self.groups is None or group_id in self.groups

    def only(self, camera_ids: Iterable[str]) -> list[str]:
        """The permitted ones among ``camera_ids``, in order."""
        return [camera_id for camera_id in camera_ids if self.can_see(camera_id)]


EVERYTHING = Access()
NOTHING = Access(frozenset(), frozenset())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _roles(encoded: str | None) -> list[str]:
    return [role for role in json.loads(encoded or "[]") if isinstance(role, str)]


def live_grants(db: sqlite3.Connection, user_id: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """The grants in force for a user: their own, and those of every Entra role they hold."""
    row = db.execute("SELECT provider_roles_json FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return []
    roles = _roles(row["provider_roles_json"])
    clause = "(subject_type='user' AND subject=?)"
    if roles:
        clause += " OR (subject_type='role' AND subject IN (%s))" % ",".join("?" * len(roles))
    return db.execute(f"SELECT * FROM access_grants WHERE (expires_at IS NULL OR expires_at>?) AND ({clause})",
                      ((now or _now()).isoformat(), str(user_id), *roles)).fetchall()


def effective_grants(db: sqlite3.Connection, user_id: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """The grants that count: the automatic "all cameras" default gives way once a role the person holds has grants of its own."""
    grants = live_grants(db, user_id, now)
    if any(grant["subject_type"] == "role" for grant in grants):
        grants = [grant for grant in grants if not grant["default_grant"]]
    return grants


def resolve(db: sqlite3.Connection, user_id: int, role: str, now: datetime | None = None) -> Access:
    """What this person may see right now."""
    if role == "administrator":
        return EVERYTHING
    grants = effective_grants(db, user_id, now)
    if any(grant["scope_type"] == "all" for grant in grants):
        return EVERYTHING
    groups = frozenset(grant["group_id"] for grant in grants if grant["scope_type"] == "group")
    if not groups:
        return NOTHING
    marks = ",".join("?" * len(groups))
    cameras = frozenset(row[0] for row in db.execute(
        f"SELECT id FROM cameras WHERE group_id IN ({marks}) UNION SELECT camera_id FROM camera_group_memberships WHERE group_id IN ({marks})",
        (*groups, *groups)))
    return Access(cameras, groups)


def may_view_camera(db: sqlite3.Connection, user_id: int, camera_id: str) -> bool:
    user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
        return False
    return resolve(db, user_id, user["role"]).can_see(camera_id)


def ensure_default_grant(db: sqlite3.Connection, user_id: int) -> None:
    """Give an operator who has no grant at all (a user or role grant) access to every camera, as before grants existed.

    For an account that has just become an operator after being something else (an administrator demoted by Entra).
    It is never a repair for an operator who was left with none: that is an administrator's decision and stands."""
    user = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if user is None or user["role"] != "operator":
        return
    row = db.execute("SELECT provider_roles_json FROM users WHERE id=?", (user_id,)).fetchone()
    roles = _roles(row["provider_roles_json"])
    clause = "(subject_type='user' AND subject=?)"
    if roles:
        clause += " OR (subject_type='role' AND subject IN (%s))" % ",".join("?" * len(roles))
    if not db.execute(f"SELECT 1 FROM access_grants WHERE {clause}", (str(user_id), *roles)).fetchone():
        db.execute("INSERT INTO access_grants(subject_type,subject,scope_type,default_grant) VALUES ('user',?,'all',1)", (str(user_id),))
