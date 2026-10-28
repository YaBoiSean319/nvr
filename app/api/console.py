"""The multi-site console (#141): administer the sites, see their health, and reach a site's cameras, alarms and pictures through this NVR.

Sites are federated through tokens limited to the read and media scopes, so the console can only look and watch; changing a site's alarms, cameras or settings is done
at the site. Hidden things answer like missing ones: a person without a grant for a site is told nothing of it.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.convertors import Convertor, register_url_convertor
from urllib.parse import quote

from app import console, network
from app.api import live_media
from app.audit import record
from app.credentials import encrypt
from app.principal import Administrator, CurrentUser, current_user, requires_scope
from app.store import connect

router = APIRouter()


class ConsoleIdConvertor(Convertor):
    """A path segment that names something at a site: ``<site>~<id there>``. This NVR's own identifiers never match."""

    regex = r"[a-z0-9]{8}~[^/]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return str(value)


register_url_convertor("consoleid", ConsoleIdConvertor())

PASS_REQUEST = ("range", "if-none-match", "if-modified-since", "content-type", "accept")
PASS_RESPONSE = ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified", "cache-control", "content-disposition", "x-content-type-options")


# --- administration ---------------------------------------------------------------------------------------------------------------------------

class SiteBody(BaseModel):
    """A site. ``token`` is write-only: send it to set or change it, leave it out to keep what is saved."""
    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, max_length=2048)
    token: str | None = Field(default=None, min_length=20, max_length=400)
    allow_private: bool | None = None
    enabled: bool | None = None


class ConsoleBody(BaseModel):
    enabled: bool


def _audit(db: sqlite3.Connection, actor, action: str, site_id: str, **details: Any) -> None:
    record(db, actor.id, action, "console_site", site_id, **details)


@router.get("/api/console/status")
async def console_status(user: CurrentUser) -> dict[str, object]:
    """Whether this NVR is a console, and the sites this person may see with how each is doing."""
    await console.registry.load()
    return {"enabled": console.registry.enabled, "sites": [console.site_status(link) for link in console.registry.visible(user.access)] if console.registry.enabled else []}


def _rows() -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute("SELECT * FROM console_sites ORDER BY name COLLATE NOCASE").fetchall()


@router.get("/api/console")
async def get_console(_: Administrator) -> dict[str, object]:
    await console.registry.load(max_age=0)
    rows = await run_in_threadpool(_rows)
    return {"enabled": console.registry.enabled, "sites": [console.admin_view(row) for row in rows], "needed_scopes": list(console.NEEDED_SCOPES)}


@router.put("/api/console")
async def put_console(body: ConsoleBody, user: Administrator) -> dict[str, object]:
    def save() -> None:
        with connect() as db:
            db.execute("UPDATE console_settings SET enabled=?,updated_at=? WHERE singleton_id=1", (int(body.enabled), console._now_iso()))
            record(db, user.id, "console_mode_changed", "console", "1", enabled=body.enabled)
    await run_in_threadpool(save)
    console.registry.invalidate()
    return await get_console(user)


def _row(site_id: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT * FROM console_sites WHERE id=?", (site_id,)).fetchone()


@router.post("/api/console/sites", status_code=status.HTTP_201_CREATED)
async def add_site(body: SiteBody, user: Administrator) -> dict[str, object]:
    if not body.name or not body.url or not body.token:
        raise HTTPException(status_code=422, detail="A site needs a name, an address and a token")
    allow_private = bool(body.allow_private)
    try:
        url = console.check_url(body.url, allow_private)
        await console.probe(url, body.token)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    site_id = console.new_site_id()

    def save() -> sqlite3.Row:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT COUNT(*) FROM console_sites").fetchone()[0] >= console.MAX_SITES:
                    raise HTTPException(status_code=422, detail=f"At most {console.MAX_SITES} sites")
                db.execute("INSERT INTO console_sites(id,name,base_url,token_ciphertext,allow_private,enabled,created_at,created_by) VALUES(?,?,?,?,?,1,?,?)",
                           (site_id, body.name.strip(), url, encrypt(body.token), int(allow_private), console._now_iso(), user.id))
                _audit(db, user, "console_site_added", site_id, name=body.name.strip(), url=url)
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                raise HTTPException(status_code=409, detail="A site with that name already exists") from None
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return db.execute("SELECT * FROM console_sites WHERE id=?", (site_id,)).fetchone()
    row = await run_in_threadpool(save)
    console.registry.invalidate()
    await console.registry.load(max_age=0)
    return console.admin_view(row)


@router.put("/api/console/sites/{site_id}")
async def update_site(site_id: str, body: SiteBody, user: Administrator) -> dict[str, object]:
    row = await run_in_threadpool(_row, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Site not found")
    allow_private = bool(row["allow_private"]) if body.allow_private is None else body.allow_private
    try:
        url = console.check_url(body.url, allow_private) if body.url else row["base_url"]
        if body.token or body.url or body.allow_private is not None:
            await console.registry.load(max_age=0)
            link = console.registry.link(site_id)
            token = body.token or (link.token if link else None)
            if token is None:
                raise console.ConsoleError("The token of this site cannot be read: enter it again")
            await console.probe(url, token)
    except console.ConsoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    def save() -> sqlite3.Row:
        with connect() as db:
            try:
                db.execute("UPDATE console_sites SET name=?,base_url=?,token_ciphertext=?,allow_private=?,enabled=? WHERE id=?",
                           ((body.name or row["name"]).strip(), url, encrypt(body.token) if body.token else row["token_ciphertext"], int(allow_private),
                            int(row["enabled"] if body.enabled is None else body.enabled), site_id))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="A site with that name already exists") from None
            _audit(db, user, "console_site_updated", site_id, name=(body.name or row["name"]).strip(), token_changed=bool(body.token))
            return db.execute("SELECT * FROM console_sites WHERE id=?", (site_id,)).fetchone()
    saved = await run_in_threadpool(save)
    console.registry.invalidate()
    await console.registry.load(max_age=0)
    return console.admin_view(saved)


@router.delete("/api/console/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(site_id: str, user: Administrator) -> Response:
    def remove() -> bool:
        with connect() as db:
            row = db.execute("SELECT name FROM console_sites WHERE id=?", (site_id,)).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM console_sites WHERE id=?", (site_id,))
            _audit(db, user, "console_site_removed", site_id, name=row["name"])
            return True
    if not await run_in_threadpool(remove):
        raise HTTPException(status_code=404, detail="Site not found")
    console.registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/console/sites/{site_id}/check")
async def check_site(site_id: str, _: Administrator) -> dict[str, object]:
    """Try the site now, without waiting for the next health check."""
    await console.registry.load(max_age=0)
    link = console.registry.link(site_id)
    row = await run_in_threadpool(_row, site_id)
    if link is None or row is None:
        raise HTTPException(status_code=404, detail="Site not found")
    link.retry_at = 0.0
    try:
        await console.call(link, "GET", "/api/cameras")
    except console.SiteDown:
        pass
    return console.admin_view(row)


# --- reaching a site's things through this NVR ------------------------------------------------------------------------------------------------

def _link_for(identifier: str, user) -> tuple[console.SiteLink, str]:
    """The site an identifier names and the identifier there. A site this person has no grant for, or does not exist, is the same not-found."""
    parsed = console.split_id(identifier)
    link = console.registry.link(parsed[0]) if parsed else None
    if parsed is None or link is None or not link.enabled or not console.registry.enabled or not user.access.can_see_site(link.id):
        raise HTTPException(status_code=404, detail="Not found")
    return link, parsed[1]


def _query(raw: str) -> str:
    """The query as sent to the site, without ``unmasked``: whether someone may lift a privacy mask is decided here, never by the site's token."""
    return "&".join(part for part in raw.split("&") if part and not part.lower().startswith("unmasked="))


async def _forward(request: Request, link: console.SiteLink, path: str) -> Response:
    headers = {name: request.headers[name] for name in PASS_REQUEST if name in request.headers}
    headers["accept-encoding"] = "identity"
    content = await request.body() if request.method not in ("GET", "HEAD") else None
    try:
        upstream = await console.stream(link, request.method, path, query=_query(request.url.query), headers=headers, content=content)
    except console.SiteDown:
        raise HTTPException(status_code=502, detail=f"The site {link.name} cannot be reached") from None
    if upstream.headers.get("content-type", "").startswith("application/json"):
        raw = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(raw)
        except ValueError:
            return Response(raw, status_code=upstream.status_code, media_type="application/json")
        return JSONResponse(console.rewrite(link.id, data), status_code=upstream.status_code)
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers={name: upstream.headers[name] for name in PASS_RESPONSE if name in upstream.headers},
                             background=BackgroundTask(upstream.aclose))


@router.api_route("/api/cameras/{camera_id:consoleid}/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def camera_through_console(camera_id: str, rest: str, request: Request, user: CurrentUser) -> Response:
    """Live, snapshots, footage and the rest of what a camera offers, through the site that owns it (the site's own token decides what is allowed)."""
    await console.registry.load()
    link, local = _link_for(camera_id, user)
    return await _forward(request, link, f"/api/cameras/{quote(local)}/{rest}")


@router.get("/api/alarms/{alarm_id:consoleid}")
async def alarm_through_console(alarm_id: str, request: Request, user: CurrentUser) -> Response:
    await console.registry.load()
    link, local = _link_for(alarm_id, user)
    response = await _forward(request, link, f"/api/alarms/{quote(local)}")
    if isinstance(response, JSONResponse) and response.status_code == 200:
        data = json.loads(response.body)
        data["id"], data["site"] = alarm_id, {"id": link.id, "name": link.name}
        for key in ("track_id", "rule_id"):
            if data.get(key):
                data[key] = console.prefixed(link.id, data[key])
        return JSONResponse(data)
    return response


@router.api_route("/api/alarms/{alarm_id:consoleid}/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def alarm_action_at_the_site(alarm_id: str, rest: str, user: CurrentUser) -> Response:
    await console.registry.load()
    link, _ = _link_for(alarm_id, user)
    raise HTTPException(status_code=409, detail=f"This alarm belongs to the site {link.name}: acknowledge, assign and resolve it there")


@router.get("/api/ai/tracks/{track_id:consoleid}/thumbnail")
async def thumbnail_through_console(track_id: str, request: Request, user: CurrentUser) -> Response:
    await console.registry.load()
    link, local = _link_for(track_id, user)
    return await _forward(request, link, f"/api/ai/tracks/{quote(local)}/thumbnail")


async def _refuse(websocket: WebSocket, code: int, kind: str, detail: str) -> None:
    await live_media._refuse(websocket, code, {"type": kind, "detail": detail})


@router.websocket("/api/cameras/{camera_id:consoleid}/live/stream")
@requires_scope("media")
async def live_through_console(websocket: WebSocket, camera_id: str) -> None:
    """A site's live video, relayed as it comes (the protocol is the site's own: see ``live_stream``)."""
    if not live_media._same_origin(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        principal = current_user(websocket)
    except HTTPException as exc:
        if exc.status_code == 404:
            await _refuse(websocket, live_media.CLOSE_NOT_FOUND, "not_found", "Camera not found")
            return
        code = live_media.CLOSE_UNAUTHORIZED if exc.status_code == 401 else live_media.CLOSE_FORBIDDEN
        await _refuse(websocket, code, "unauthorized" if code == live_media.CLOSE_UNAUTHORIZED else "forbidden", str(exc.detail))
        return
    await console.registry.load()
    try:
        link, local = _link_for(camera_id, principal)
    except HTTPException:
        await _refuse(websocket, live_media.CLOSE_NOT_FOUND, "not_found", "Camera not found")
        return
    if link.down:
        await _refuse(websocket, live_media.CLOSE_STREAM_FAILED, "error", f"The site {link.name} cannot be reached")
        return
    scheme = "wss" if link.base_url.startswith("https") else "ws"
    query = _query(websocket.url.query)
    url = f"{scheme}://{link.base_url.split('://', 1)[1].rstrip('/')}/api/cameras/{quote(local)}/live/stream" + (f"?{query}" if query else "")
    try:
        upstream = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {link.token}"}, max_size=None, open_timeout=5, ping_interval=20, proxy=None)
    except (OSError, TimeoutError, websockets.WebSocketException):
        link.failed("No answer")
        await _refuse(websocket, live_media.CLOSE_STREAM_FAILED, "error", f"The site {link.name} cannot be reached")
        return

    async def to_site() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("text") if message.get("text") is not None else message.get("bytes")
            if data is not None:
                await upstream.send(data)

    async def to_viewer() -> None:
        async for data in upstream:
            if isinstance(data, str):
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)

    tasks = [asyncio.create_task(to_site()), asyncio.create_task(to_viewer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        code, reason = upstream.close_code or 1000, upstream.close_reason or ""
        await upstream.close()
        try:
            await websocket.close(code=code if 3000 <= code <= 4999 or code in (1000, 1001) else 1000, reason=reason[:120])
        except RuntimeError:
            pass
