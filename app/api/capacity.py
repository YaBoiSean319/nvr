"""Capacity planner (#149): estimates for a set of cameras, and this installation's own cameras as a starting point. An estimate, never a certification."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app import capacity
from app.principal import Administrator
from app.store import connect

router = APIRouter()

MAX_ROWS = 200


class PlanRow(BaseModel):
    name: str = Field(default="Cameras", max_length=80)
    count: int = Field(default=1, ge=1, le=5000)
    width: int | None = Field(default=None, ge=160, le=16384)
    height: int | None = Field(default=None, ge=120, le=16384)
    codec: Literal["h264", "h265"] | None = None
    fps: float | None = Field(default=None, ge=1, le=120)
    bitrate_kbps: float | None = Field(default=None, ge=16, le=200_000)
    bitrate_source: Literal["entered", "measured", "assumed"] = "entered"
    recording: Literal["continuous", "motion", "disabled"] = "continuous"
    recorded_share: float | None = Field(default=None, ge=0, le=1)
    retention_days: int = Field(default=30, ge=1, le=3650)
    ai: bool = False
    analysis_fps: float | None = Field(default=None, ge=0.1, le=5)

    @field_validator("name")
    @classmethod
    def trimmed(cls, value: str) -> str:
        return value.strip() or "Cameras"


class PlanRequest(BaseModel):
    rows: list[PlanRow] = Field(min_length=1, max_length=MAX_ROWS)
    viewers: int = Field(default=0, ge=0, le=1000)
    tiles_per_viewer: int = Field(default=4, ge=1, le=64)
    assumptions: dict[str, float] = Field(default_factory=dict, max_length=len(capacity.ASSUMPTIONS))

    @field_validator("assumptions")
    @classmethod
    def known_and_sane(cls, value: dict[str, float]) -> dict[str, float]:
        for key, number in value.items():
            item = capacity.ASSUMPTION_BY_KEY.get(key)
            if item is None:
                raise ValueError(f"Unknown assumption {key}")
            if not item.low <= number <= item.high:
                raise ValueError(f"{item.label} must be between {item.low:g} and {item.high:g}")
        return value


@router.get("/api/admin/capacity/inventory")
def get_inventory(_: Administrator) -> dict[str, object]:
    """This installation's cameras as plan rows, with measured bitrates where there is enough footage; the rest is left for the estimate to label as assumed."""
    with connect() as db:
        rows = capacity.inventory(db)
    return {"rows": rows, "disclaimer": capacity.DISCLAIMER}


@router.get("/api/admin/capacity/assumptions")
def get_assumptions(_: Administrator) -> dict[str, object]:
    return {"assumptions": [{"key": item.key, "label": item.label, "value": item.value, "unit": item.unit, "low": item.low, "high": item.high} for item in capacity.ASSUMPTIONS],
            "disclaimer": capacity.DISCLAIMER}


@router.post("/api/admin/capacity/estimate")
def post_estimate(payload: PlanRequest, _: Administrator) -> dict[str, object]:
    rows = [capacity.Row(**row.model_dump()) for row in payload.rows]
    return capacity.estimate(rows, payload.viewers, payload.tiles_per_viewer, payload.assumptions)
