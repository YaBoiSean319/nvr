"""Attribute search across cameras: filters, results with thumbnails, and the unanalysed coverage of the range searched."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Response

from app import privacy, search, similar
from app.attributes import DIRECTIONS, PALETTE, VEHICLE_TYPES
from app.principal import CurrentUser
from app.store import connect

router = APIRouter()


def _choice(name: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value in (None, ""):
        return None
    if value not in allowed:
        raise HTTPException(status_code=422, detail=f"{name} must be one of {', '.join(allowed)}")
    return value


@router.get("/api/ai/search/options")
def get_options(_: CurrentUser) -> dict[str, object]:
    return search.options()


@router.get("/api/ai/search")
def search_tracks(
    user: CurrentUser,
    start: str = Query(max_length=64),
    end: str = Query(max_length=64),
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    label: str | None = Query(default=None, max_length=32),
    upper_colour: str | None = Query(default=None, max_length=16),
    lower_colour: str | None = Query(default=None, max_length=16),
    vehicle_colour: str | None = Query(default=None, max_length=16),
    vehicle_type: str | None = Query(default=None, max_length=16),
    direction: str | None = Query(default=None, max_length=16),
    zone: str | None = Query(default=None, max_length=64),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=60, ge=1, le=search.MAX_LIMIT),
) -> dict[str, object]:
    try:
        start_time, end_time = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be timestamps with a time zone") from None
    if (end_time - start_time).total_seconds() <= 0 or (end_time - start_time) > search.MAX_RANGE:
        raise HTTPException(status_code=422, detail=f"The range must be positive and at most {search.MAX_RANGE.days} days")
    try:
        zone_box = search.parse_zone(zone) if zone else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    filters = {
        "label": _choice("label", label, search.LABELS), "upper_colour": _choice("upper_colour", upper_colour, PALETTE), "lower_colour": _choice("lower_colour", lower_colour, PALETTE),
        "vehicle_colour": _choice("vehicle_colour", vehicle_colour, PALETTE), "vehicle_type": _choice("vehicle_type", vehicle_type, VEHICLE_TYPES),
        "direction": _choice("direction", direction, DIRECTIONS),
    }
    searched_until = min(end_time, datetime.now(timezone.utc))
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
        # Cameras this person may not see are not searched, and are not distinguishable from cameras that do not exist.
        wanted = [camera_id for camera_id in (camera_ids or names) if camera_id in names]
        searched = {camera_id: names[camera_id] for camera_id in user.access.only(wanted)}
        results, truncated = search.find_tracks(db, list(searched), start_time, end_time, zone=zone_box, order=order, limit=limit, **filters)
        coverage = search.coverage(db, searched, start_time, searched_until)
    return {"start": start_time.astimezone(timezone.utc).isoformat(), "end": end_time.astimezone(timezone.utc).isoformat(), "results": results, "truncated": truncated,
            "coverage": coverage, "coverage_note": "No match in time that was not analysed means nothing was looked at; unanalysed time is listed with the results."}


@router.get("/api/ai/tracks/{track_id}/similar")
def similar_tracks(
    track_id: str,
    user: CurrentUser,
    start: str | None = Query(default=None, max_length=64),
    end: str | None = Query(default=None, max_length=64),
    camera_ids: list[str] | None = Query(default=None, max_length=500),
    limit: int = Query(default=30, ge=1, le=similar.MAX_RESULTS),
    min_similarity: float = Query(default=0.6, ge=0.0, le=1.0),
    sort: str = Query(default="time", pattern="^(time|similarity)$"),
) -> dict[str, object]:
    """The tracks that look most like this one on every camera the person may see, with how alike each is (never the embeddings themselves).

    The closest ``limit`` are chosen by similarity (``rank`` 1 is the closest) and listed in time order, so a person can be followed through the site,
    or by similarity with ``sort=similarity``. The range defaults to the last seven days."""
    try:
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else end_time - timedelta(days=7)
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be timestamps with a time zone") from None
    if (end_time - start_time).total_seconds() <= 0 or (end_time - start_time) > search.MAX_RANGE:
        raise HTTPException(status_code=422, detail=f"The range must be positive and at most {search.MAX_RANGE.days} days")
    with connect() as db:
        source = db.execute("SELECT camera_id FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
        if source is None or not user.access.can_see(source["camera_id"]):
            raise HTTPException(status_code=404, detail="Object not found")
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
        wanted = [camera_id for camera_id in (camera_ids or names) if camera_id in names]
        searched = {camera_id: names[camera_id] for camera_id in user.access.only(wanted)}
        try:
            matches = similar.find_similar(db, track_id, list(searched), start_time, end_time, limit=limit, min_similarity=min_similarity)
        except LookupError:
            raise HTTPException(status_code=409, detail="This object has no appearance recorded, so nothing can be compared with it") from None
        views = {view["track_id"]: view for view in search.tracks_by_ids(db, [match[0] for match in matches])}
        results = []
        for rank, (found_id, score) in enumerate(matches, start=1):
            if found_id in views:
                results.append({**views[found_id], "similarity": round(max(0.0, min(1.0, score)), 3), "rank": rank})
        coverage = search.coverage(db, searched, start_time, min(end_time, datetime.now(timezone.utc)))
    if sort == "time":
        results.sort(key=lambda item: (item["first_seen_at"], item["rank"]))
    return {"track_id": track_id, "start": start_time.astimezone(timezone.utc).isoformat(), "end": end_time.astimezone(timezone.utc).isoformat(), "results": results,
            "coverage": coverage, "sorted_by": sort}


@router.get("/api/ai/tracks/{track_id}/thumbnail")
async def track_thumbnail(track_id: str, user: CurrentUser) -> Response:
    """The frame an object was first analysed in, with the camera's privacy masks drawn over it; if a mask cannot be drawn, no picture."""
    with connect() as db:
        row = db.execute("SELECT t.camera_id,k.jpeg,k.captured_at FROM ai_tracks t JOIN track_keyframes k ON k.track_id=t.id WHERE t.id=?", (track_id,)).fetchone()
        if row is None or not user.access.can_see(row["camera_id"]):
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        zones = privacy.server_zones(db, row["camera_id"], datetime.fromisoformat(row["captured_at"]))
    try:
        picture = await privacy.mask_jpeg(bytes(row["jpeg"]), zones)
    except Exception:  # noqa: BLE001 - never the unmasked picture
        raise HTTPException(status_code=503, detail="The privacy mask could not be applied to this picture") from None
    return Response(picture, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
