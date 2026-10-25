"""Coverage report and the self-test, for administrators in Settings."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from app import coverage, self_test
from app.principal import Administrator

router = APIRouter()

MAX_REPORT_WINDOW = timedelta(days=31)


@router.get("/api/coverage/report")
def coverage_report(_: Administrator, start: datetime = Query(), end: datetime = Query()) -> dict[str, object]:
    """Coverage per camera against its policy for [start, end), with the largest gaps and their reasons."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="Times must include a timezone")
    if not start < end or end - start > MAX_REPORT_WINDOW:
        raise HTTPException(status_code=422, detail="Report on up to 31 days at a time")
    return coverage.report(start, end)


@router.get("/api/coverage/reports")
def weekly_reports(_: Administrator) -> dict[str, object]:
    """The stored weekly reports, newest first."""
    return {"reports": coverage.latest_reports()}


@router.get("/api/self-test")
def self_test_status(_: Administrator) -> dict[str, object]:
    return {"latest": self_test.latest(), "schedule": {"local_time": self_test.SCHEDULE_TIME.strftime("%H:%M")}}


@router.post("/api/self-test/run")
def run_self_test(_: Administrator) -> dict[str, object]:
    """Run the self-test now, for example to confirm a fix; incidents recover when their check passes."""
    return {"latest": self_test.run("manual"), "schedule": {"local_time": self_test.SCHEDULE_TIME.strftime("%H:%M")}}
