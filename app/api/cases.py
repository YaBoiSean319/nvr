"""Case files: a case's items, members and holds, and its evidence packages."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import cases, footage_chain
from app.exports import MAX_VERIFY_PACKAGE_BYTES
from app.principal import CurrentUser, requires_scope
from app.retention import acquire_playback, release_playback
from app.store import connect

router = APIRouter()


def _open(db, case_id: str, user) -> Any:
    """The case if this person is in it; anything else is a case that does not exist (and the attempt is audited)."""
    row = cases.find(db, case_id, user)
    if row is None:
        if db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None:
            if db.in_transaction:
                db.execute("COMMIT")  # Nothing has been changed yet: keep the record of the attempt when the request ends in a 404
            cases.record_denied(db, case_id, user)
        raise HTTPException(status_code=404, detail="Case not found")
    return row


def _manage(db, row, user) -> None:
    if not cases.is_manager(db, row, user):
        raise HTTPException(status_code=403, detail="Only the case's owner or an administrator can do this")


def _guard(action):
    try:
        return action()
    except cases.CaseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=cases.MAX_TITLE)
    description: str = Field(default="", max_length=cases.MAX_TEXT)


class CaseUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=cases.MAX_TITLE)
    description: str | None = Field(default=None, max_length=cases.MAX_TEXT)


class MemberAdd(BaseModel):
    username: str = Field(min_length=1, max_length=120)


class ItemAdd(BaseModel):
    kind: Literal["clip", "mark", "note", "alarm", "search", "export"]
    camera_id: str | None = Field(default=None, max_length=64)
    start: datetime | None = None
    end: datetime | None = None
    at: datetime | None = None
    text: str = Field(default="", max_length=cases.MAX_TEXT)
    alarm_id: str | None = Field(default=None, max_length=64)
    export_id: str | None = Field(default=None, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    track_ids: list[str] = Field(default_factory=list, max_length=cases.MAX_SEARCH_TRACKS)


class CloseRequest(BaseModel):
    confirm: bool = False


@router.get("/api/cases")
@requires_scope("export")
def list_cases(user: CurrentUser, status_filter: Annotated[str | None, Query(alias="status", pattern="^(open|closed)$")] = None) -> dict[str, object]:
    with connect() as db:
        return {"cases": cases.list_cases(db, user, status_filter)}


@router.post("/api/cases", status_code=status.HTTP_201_CREATED)
@requires_scope("export")
def create_case(payload: CaseCreate, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        case_id = _guard(lambda: cases.create_case(db, int(user["id"]), payload.title, payload.description))
        return cases.detail(db, cases.find(db, case_id, user), user)


@router.get("/api/cases/{case_id}")
@requires_scope("export")
def get_case(case_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        row = _open(db, case_id, user)
        cases.record_opened(db, case_id, user)
        return cases.detail(db, row, user)


@router.patch("/api/cases/{case_id}")
@requires_scope("export")
def update_case(case_id: str, payload: CaseUpdate, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _manage(db, row, user)
        _guard(lambda: cases.update_case(db, row, int(user["id"]), payload.title, payload.description))
        return cases.detail(db, cases.find(db, case_id, user), user)


@router.post("/api/cases/{case_id}/members", status_code=status.HTTP_201_CREATED)
@requires_scope("export")
def add_member(case_id: str, payload: MemberAdd, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _manage(db, row, user)
        found = db.execute("SELECT id FROM users WHERE username=?", (payload.username.strip(),)).fetchone()
        if found is None:
            raise HTTPException(status_code=404, detail="No such user")
        _guard(lambda: cases.add_member(db, row, int(user["id"]), int(found["id"])))
        return cases.detail(db, row, user)


@router.delete("/api/cases/{case_id}/members/{user_id}")
@requires_scope("export")
def remove_member(case_id: str, user_id: int, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _manage(db, row, user)
        _guard(lambda: cases.remove_member(db, row, int(user["id"]), user_id))
        return cases.detail(db, row, user)


@router.post("/api/cases/{case_id}/items", status_code=status.HTTP_201_CREATED)
@requires_scope("export")
def add_item(case_id: str, payload: ItemAdd, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)

        def add() -> str:
            if payload.kind == "clip":
                if not payload.camera_id or payload.start is None or payload.end is None:
                    raise cases.CaseError("A clip needs a camera, a start and an end")
                return cases.add_clip(db, row, user, payload.camera_id, payload.start, payload.end, payload.text)
            if payload.kind == "mark":
                if not payload.camera_id or payload.at is None:
                    raise cases.CaseError("A mark needs a camera and a time")
                return cases.add_mark(db, row, user, payload.camera_id, payload.at, payload.text)
            if payload.kind == "note":
                return cases.add_note(db, row, user, payload.text)
            if payload.kind == "alarm":
                if not payload.alarm_id:
                    raise cases.CaseError("Name the alarm to add")
                return cases.add_alarm(db, row, user, payload.alarm_id)
            if payload.kind == "search":
                return cases.add_search(db, row, user, payload.text, payload.params, payload.track_ids)
            if not payload.export_id:
                raise cases.CaseError("Name the export to add")
            return cases.add_export(db, row, user, payload.export_id)

        item_id = _guard(add)
        detail = cases.detail(db, row, user)
        return {"item_id": item_id, "case": detail}


@router.delete("/api/cases/{case_id}/items/{item_id}")
@requires_scope("export")
def remove_item(case_id: str, item_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _guard(lambda: cases.remove_item(db, row, user, item_id))
        return cases.detail(db, row, user)


@router.post("/api/cases/{case_id}/items/{item_id}/viewed", status_code=status.HTTP_204_NO_CONTENT)
@requires_scope("export")
def item_viewed(case_id: str, item_id: str, user: CurrentUser) -> Response:
    """Record that this person opened an item of the case (the interface calls this when a clip is opened in Playback from the case)."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _guard(lambda: cases.record_item_viewed(db, row, user, item_id))
    return Response(status_code=204)


@router.post("/api/cases/{case_id}/close")
@requires_scope("export")
def close_case(case_id: str, payload: CloseRequest, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        _manage(db, row, user)
        _guard(lambda: cases.close_case(db, row, int(user["id"]), payload.confirm))
        return cases.detail(db, cases.find(db, case_id, user), user)


@router.get("/api/cases/{case_id}/audit")
@requires_scope("export")
def get_audit(case_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        _open(db, case_id, user)
        return {"case_id": case_id, "events": cases.audit_extract(db, case_id, user.access)}


@router.post("/api/cases/{case_id}/packages", status_code=status.HTTP_202_ACCEPTED)
@requires_scope("export")
def request_package(case_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        package_id = _guard(lambda: cases.request_package(db, row, user))
        return cases.package_view(db.execute("SELECT * FROM case_packages WHERE id=?", (package_id,)).fetchone())


def _package(db, case_id: str, package_id: str, user):
    _open(db, case_id, user)
    package = db.execute("SELECT * FROM case_packages WHERE id=? AND case_id=?", (package_id, case_id)).fetchone()
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found")
    return package


@router.get("/api/cases/{case_id}/packages/{package_id}")
@requires_scope("export")
def get_package(case_id: str, package_id: str, user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        return cases.package_view(_package(db, case_id, package_id, user))


@router.delete("/api/cases/{case_id}/packages/{package_id}")
@requires_scope("export")
def delete_package(case_id: str, package_id: str, user: CurrentUser) -> dict[str, object]:
    """Remove a package's file and record (to make room for another); the person who asked for it, the owner and administrators may."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _open(db, case_id, user)
        package = _package(db, case_id, package_id, user)
        if package["requested_by"] != int(user["id"]) and not cases.is_manager(db, row, user):
            raise HTTPException(status_code=403, detail="Only the person who asked for the package, the case's owner or an administrator can delete it")
        if package["status"] == "building":
            raise HTTPException(status_code=409, detail="The package is still being built")
        if package["relative_path"]:
            try:
                cases.package_path(package).unlink(missing_ok=True)
            except (FileNotFoundError, ValueError, OSError):
                pass
        db.execute("DELETE FROM case_packages WHERE id=?", (package_id,))
        cases.audit(db, case_id, int(user["id"]), "package_deleted", package=package_id)
        return cases.detail(db, row, user)


@router.get("/api/cases/{case_id}/packages/{package_id}/download")
@requires_scope("export")
def download_package(case_id: str, package_id: str, user: CurrentUser) -> FileResponse:
    with connect() as db:
        package = _package(db, case_id, package_id, user)
        if package["requested_by"] != int(user["id"]) and user["role"] != "administrator":
            raise HTTPException(status_code=404, detail="Package not found")  # Built with the access of the person who asked for it
        try:
            path = cases.package_path(package)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=410, detail="The package file is unavailable") from exc
        if not path.is_file():
            raise HTTPException(status_code=410, detail="The package file is unavailable")
        cases.record_download(db, user, package_id, case_id)
    acquire_playback(path)
    return FileResponse(path, media_type="application/zip", filename=f"nvr-case-{case_id}-{package_id}.zip", background=BackgroundTask(release_playback, path))


@router.post("/api/cases/{case_id}/packages/{package_id}/verify")
@requires_scope("export")
async def verify_package(case_id: str, package_id: str, request: Request, user: CurrentUser) -> dict[str, object]:
    """Check an uploaded case package: every file against its manifest, and each clip's own evidence package."""
    with connect() as db:
        _package(db, case_id, package_id, user)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_VERIFY_PACKAGE_BYTES:
        raise HTTPException(status_code=413, detail="The package exceeds the verification size limit")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_VERIFY_PACKAGE_BYTES:
            raise HTTPException(status_code=413, detail="The package exceeds the verification size limit")
    data = bytes(body)
    del body
    result = await run_in_threadpool(cases.verify_package_bytes, data, case_id, package_id, footage_chain.public_key())
    with connect() as db:
        package = db.execute("SELECT * FROM case_packages WHERE id=? AND case_id=?", (package_id, case_id)).fetchone()
        recorded = {row["export_id"]: row for row in db.execute(
            "SELECT p.export_id,e.metadata_json FROM case_package_clips p LEFT JOIN exports e ON e.id=p.export_id WHERE p.package_id=?", (package_id,))}
    # What the package's own manifest says is only as good as the package: it is checked against what this NVR built.
    result["matches_issued_package"] = bool(package and package["sha256"] and hashlib.sha256(data).hexdigest() == package["sha256"])
    result["clips_match_request"] = {clip["export_id"] for clip in result["clips"]} == set(recorded)
    for clip in result["clips"]:
        row = recorded.get(clip["export_id"])
        expected = (json.loads(row["metadata_json"]) if row and row["metadata_json"] else {}).get("signed_footage", {})
        wanted = sorted(camera for camera, state in expected.items() if state == "signed")
        if clip["valid"] and wanted and clip.get("chain") != "valid":
            clip.update(valid=False, error=f"cameras {wanted} were signed when exported, but the clip's signed chain is missing or not valid")
    result["clips_valid"] = bool(result.get("clips_valid", False)) and all(clip["valid"] for clip in result["clips"])
    result["complete"] = bool(result["complete"]) and result["clips_valid"] and result["clips_match_request"]
    valid = bool(result["checksums_valid"] and result["clips_valid"] and result["clips_match_request"] and result["matches_issued_package"])
    result["verified"] = valid
    with connect() as db:
        cases.audit(db, case_id, int(user["id"]), "package_verified" if valid else "package_verification_failed", package=package_id,
                    clips=len(result["clips"]), complete=bool(result["complete"]), matches_issued_package=result["matches_issued_package"])
    return {**result, "authenticity": "A package verifies when it is byte-for-byte the one this NVR built for this request, and every clip's own package and signed chain verify. "
                                      "Checksums inside a file are unsigned: on their own they detect changes only if the manifest is untouched."}
