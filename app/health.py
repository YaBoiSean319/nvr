from __future__ import annotations

from datetime import datetime, timezone


def recording_health(
    *,
    enabled: bool,
    recording_enabled: bool,
    mode: str,
    effective_mode: str,
    recording_status: str,
    motion_source_status: str,
    last_video_at: str | None,
    last_footage_write_at: str | None,
    now: datetime | None = None,
    freshness_seconds: int = 45,
) -> dict[str, object]:
    """Describe coverage independently from connection and motion-source status."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    def age(value: str | None) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None

    video_age = age(last_video_at)
    write_age = age(last_footage_write_at)
    video_fresh = video_age is not None and video_age <= freshness_seconds
    write_fresh = write_age is not None and write_age <= freshness_seconds * 2

    if not enabled or not recording_enabled or effective_mode == "disabled":
        state, detail = "expected_idle", "Recording is disabled by camera or schedule policy"
    elif effective_mode == "motion" and recording_status in {"buffering", "recording"} and motion_source_status == "healthy" and video_fresh and not write_fresh:
        state, detail = "expected_idle", "Motion is armed; no event footage is currently expected"
    elif not video_fresh:
        state = "camera_unreachable" if recording_status in {"reconnecting", "unavailable", "failed"} else "uncertain"
        detail = "No fresh decodable video has been observed" if state == "uncertain" else "Camera stream is unavailable"
    elif effective_mode == "continuous" and not write_fresh:
        state, detail = "write_stalled", "Video is fresh, but no recent footage segment was committed"
    elif effective_mode == "motion" and motion_source_status not in {"healthy", "unsupported"}:
        state, detail = "motion_source_degraded", "Video is fresh, but motion-event coverage is uncertain"
    else:
        state, detail = "recording", "Fresh video and expected recording writes are present"

    return {
        "state": state,
        "detail": detail,
        "video_fresh": video_fresh,
        "last_video_at": last_video_at,
        "last_footage_write_at": last_footage_write_at,
        "video_age_seconds": round(video_age, 1) if video_age is not None else None,
        "footage_write_age_seconds": round(write_age, 1) if write_age is not None else None,
        "coverage_kind": "expected_idle" if state == "expected_idle" else ("failure" if state in {"camera_unreachable", "write_stalled"} else ("uncertain" if state in {"uncertain", "motion_source_degraded"} else "covered")),
    }
