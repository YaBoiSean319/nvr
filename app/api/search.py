"""Attribute search across cameras: filters, results with thumbnails, and the unanalysed coverage of the range searched."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response

from app import privacy, search
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
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
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
    return Response(picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
