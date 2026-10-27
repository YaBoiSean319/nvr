"""Case files: a case's items, members and holds, and its evidence packages."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
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
        return {"case_id": case_id, "events": cases.audit_extract(db, case_id)}


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
    result = cases.verify_package_bytes(bytes(body), case_id, package_id, footage_chain.public_key())
    valid = result["checksums_valid"] and bool(result.get("clips_valid", False))
    with connect() as db:
        cases.audit(db, case_id, int(user["id"]), "package_verified" if valid else "package_verification_failed", package=package_id,
                    clips=len(result["clips"]), complete=bool(result["complete"]))
    return {**result, "authenticity": "Checksums are unsigned: a matching manifest detects changes to the files but not who made the package. "
                                      "Each clip's signed chain shows its source recordings match what this NVR hashed when they were written."}
