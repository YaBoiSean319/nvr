"""Measure fall detection on recorded clips (#230): how many labelled falls it finds and how many alerts it raises that are not falls.

Run the real AI worker loop (the person detector, tracker, pose model and fall decision, exactly as a camera is analysed) over each clip, and compare the falls it raises with a
file of labels. It reports detections, misses and false alarms, and false alarms per camera-day, which is the number to look at before relying on it anywhere.

    python -m app.fall_eval labels.json [--sensitivity high] [--stay-down 3] [--fps 1] [--tolerance 20] [--json report.json]

``labels.json`` names the clips (in the same folder, or ``--clips-dir``) and, for each, the seconds into the clip at which a person went down (the moment they hit the floor)::

    {"clips": [{"file": "corridor-fall.mp4", "falls": [41.5], "note": "staged fall"},
               {"file": "corridor-sitting.mp4", "falls": [], "note": "sat down, bent, lay on the sofa on purpose"}]}

A clip with no falls is how false alarms are measured, so record plenty of ordinary movement. A raised fall counts as the labelled one if it comes from two seconds before it to
``--tolerance`` seconds after it (the person must stay down before it is raised); each labelled fall matches at most one raised fall, and every other raised fall is a false alarm.
Nothing here needs a licence; the models are the ones the NVR already uses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

EARLY_SECONDS = 2.0
DEFAULT_TOLERANCE = 20.0
SETTLE_SECONDS = 1.0  # After the last frame, for the worker to store what it found


@dataclass
class Clip:
    file: Path
    falls: list[float] = field(default_factory=list)
    note: str = ""


@dataclass
class Scored:
    detected: list[tuple[float, float]]  # (labelled time, how many seconds after it the fall was raised)
    missed: list[float]
    false_alarms: list[float]  # Times raised that match no labelled fall


def load_labels(path: Path, clips_dir: Path | None = None) -> list[Clip]:
    document = json.loads(path.read_text(encoding="utf-8"))
    folder = clips_dir or path.parent
    clips = []
    for item in document.get("clips", []):
        falls = sorted(float(value) for value in item.get("falls", []))
        if any(value < 0 for value in falls):
            raise ValueError(f"{item.get('file')}: a fall cannot be before the start of the clip")
        clips.append(Clip(folder / str(item["file"]), falls, str(item.get("note", ""))))
    if not clips:
        raise ValueError("The labels name no clips")
    return clips


def score(raised: list[float], labelled: list[float], tolerance: float = DEFAULT_TOLERANCE) -> Scored:
    """Match raised falls (seconds into the clip) to labelled ones: each label takes the earliest unused raised fall in its window; the rest are false alarms."""
    unused = sorted(raised)
    detected: list[tuple[float, float]] = []
    missed: list[float] = []
    for label in sorted(labelled):
        match = next((item for item in unused if label - EARLY_SECONDS <= item <= label + tolerance), None)
        if match is None:
            missed.append(label)
        else:
            unused.remove(match)
            detected.append((label, round(match - label, 2)))
    return Scored(detected, missed, unused)


@contextmanager
def isolated() -> Iterator[Path]:
    """A throwaway database, recordings folder and credential key for the run; the real ones are never touched, and the environment is put back."""
    keys = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")
    previous = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="nvr-fall-eval-") as directory:
        root = Path(directory)
        (root / "recordings").mkdir()
        os.environ.update({"NVR_DB_PATH": str(root / "eval.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")})
        try:
            yield root
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


async def _frames(path: Path, fps: float, counter: dict[str, int]):
    """The clip's frames as the AI worker receives them (448x256 BGR at the analysis rate)."""
    from .detection import MODEL_FRAME_BYTES, MODEL_HEIGHT, MODEL_WIDTH

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to read the clips")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-vf", f"fps={fps},scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert process.stdout
    try:
        while True:
            try:
                frame = await process.stdout.readexactly(MODEL_FRAME_BYTES)
            except asyncio.IncompleteReadError:
                return
            counter["index"] += 1
            yield frame
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def clip_seconds(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("FFprobe is required to read the clips")
    result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(result.stdout.strip() or 0)


def run_clip(path: Path, *, sensitivity: str, stay_down: float, fps: float, detector: Any = None, reader: Any = None, frames: Any = None) -> list[float]:
    """Analyse one clip as a camera with fall detection on and return when (seconds into the clip) each fall was raised. ``detector``, ``reader`` and ``frames``
    stand in for the person detector, the pose model and the decoder (the tests use them); left out, the real ones are used."""
    with isolated():
        from . import detection
        from .credentials import encrypt
        from .store import connect, initialize

        initialize()
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera_id, "Evaluation", encrypt("rtsp://camera.invalid/evaluation")))
            db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,use_substream,analysis_fps) VALUES(?,1,1,0,?)", (camera_id, fps))
            db.execute("INSERT INTO fall_settings(camera_id,enabled,sensitivity,stay_down_seconds) VALUES(?,1,?,?)", (camera_id, sensitivity, stay_down))
        start = datetime.now(timezone.utc)
        counter = {"index": 0}

        def clock() -> datetime:
            return start + timedelta(seconds=counter["index"] / fps)

        async def analyse() -> None:
            finished = asyncio.Event()
            source = frames or (lambda _source, _fps: _frames(path, fps, counter))

            async def feed(_source: str, _fps: float):
                async for frame in source(_source, _fps):
                    if frames is not None:
                        counter["index"] += 1
                    yield frame
                finished.set()
                await asyncio.Event().wait()  # The camera stays connected

            manager = detection.DetectionManager(clock=clock)
            if detector is not None:
                manager.detector = detector
            if reader is not None:
                manager._pose_readers["CPU"] = reader
            original = detection._raw_frames
            detection._raw_frames = feed
            try:
                await manager.start()
                await finished.wait()
                await asyncio.sleep(SETTLE_SECONDS)
                await manager.stop()
            finally:
                detection._raw_frames = original

        asyncio.run(analyse())
        with connect() as db:
            rows = db.execute("SELECT occurred_at FROM fall_events ORDER BY occurred_at").fetchall()
        return [(datetime.fromisoformat(row["occurred_at"]) - start).total_seconds() for row in rows]


def evaluate(clips: list[Clip], *, sensitivity: str = "high", stay_down: float = 3.0, fps: float = 1.0, tolerance: float = DEFAULT_TOLERANCE, **injected: Any) -> dict[str, Any]:
    report: dict[str, Any] = {"settings": {"sensitivity": sensitivity, "stay_down_seconds": stay_down, "analysis_fps": fps, "tolerance_seconds": tolerance}, "clips": []}
    totals = {"seconds": 0.0, "labelled": 0, "detected": 0, "missed": 0, "false_alarms": 0}
    known_seconds = injected.pop("seconds", None)  # The tests give a clip's length instead of having it probed
    for clip in clips:
        raised = run_clip(clip.file, sensitivity=sensitivity, stay_down=stay_down, fps=fps, **injected)
        scored = score(raised, clip.falls, tolerance)
        seconds = known_seconds or clip_seconds(clip.file)
        report["clips"].append({"file": clip.file.name, "note": clip.note, "seconds": round(seconds, 1), "labelled": clip.falls, "raised": [round(item, 1) for item in raised],
                                "detected": [{"at": at, "raised_after_seconds": after} for at, after in scored.detected], "missed": scored.missed,
                                "false_alarms": [round(item, 1) for item in scored.false_alarms]})
        totals["seconds"] += seconds
        totals["labelled"] += len(clip.falls)
        totals["detected"] += len(scored.detected)
        totals["missed"] += len(scored.missed)
        totals["false_alarms"] += len(scored.false_alarms)
    days = totals["seconds"] / 86400
    report["totals"] = {
        "clips": len(clips), "camera_hours": round(totals["seconds"] / 3600, 2), "labelled_falls": totals["labelled"], "detected": totals["detected"], "missed": totals["missed"],
        "false_alarms": totals["false_alarms"],
        "detection_rate": round(totals["detected"] / totals["labelled"], 3) if totals["labelled"] else None,
        "false_alarms_per_camera_day": round(totals["false_alarms"] / days, 2) if days else None,
    }
    return report


def render(report: dict[str, Any]) -> str:
    lines = ["Fall detection on recorded clips", ""]
    settings = report["settings"]
    lines.append(f"Sensitivity {settings['sensitivity']}, stay-down {settings['stay_down_seconds']:g} s, analysed at {settings['analysis_fps']:g} frames a second, "
                 f"a raised fall counts if it is up to {settings['tolerance_seconds']:g} s after the labelled one.")
    lines.append("")
    for clip in report["clips"]:
        verdict = "ok" if not clip["missed"] and not clip["false_alarms"] else "CHECK"
        lines.append(f"[{verdict}] {clip['file']} ({clip['seconds']:g} s){' - ' + clip['note'] if clip['note'] else ''}")
        for item in clip["detected"]:
            lines.append(f"      found the fall at {item['at']:g} s, raised {item['raised_after_seconds']:g} s later")
        for item in clip["missed"]:
            lines.append(f"      MISSED the fall at {item:g} s")
        for item in clip["false_alarms"]:
            lines.append(f"      FALSE ALARM at {item:g} s")
    totals = report["totals"]
    lines += ["", f"{totals['clips']} clips, {totals['camera_hours']:g} camera-hours",
              f"Labelled falls found: {totals['detected']} of {totals['labelled_falls']}" + (f" ({totals['detection_rate']:.0%})" if totals["detection_rate"] is not None else ""),
              f"False alarms: {totals['false_alarms']}" + (f" ({totals['false_alarms_per_camera_day']:g} per camera-day)" if totals["false_alarms_per_camera_day"] is not None else ""),
              "", "A small sample says little: the more ordinary footage (and the more staged falls) the better these numbers can be trusted. This is an aid, not a safety system."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure fall detection on recorded clips")
    parser.add_argument("labels", type=Path, help="labels.json naming the clips and when each fall happens")
    parser.add_argument("--clips-dir", type=Path, help="where the clips are (default: next to the labels)")
    parser.add_argument("--sensitivity", choices=("low", "normal", "high"), default="high")
    parser.add_argument("--stay-down", type=float, default=3.0, help="seconds a person must stay down")
    parser.add_argument("--fps", type=float, default=1.0, help="analysis rate, frames a second (the camera's AI analysis rate)")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--json", type=Path, help="also write the report here")
    options = parser.parse_args(argv)
    try:
        clips = load_labels(options.labels, options.clips_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not read the labels: {exc}", file=sys.stderr)
        return 2
    missing = [clip.file for clip in clips if not clip.file.is_file()]
    if missing:
        print("Clips not found: " + ", ".join(str(item) for item in missing), file=sys.stderr)
        return 2
    from . import pose_models

    if not pose_models.installed():
        print("The pose model is not installed (see docs/fall-detection.md), so no falls can be found.", file=sys.stderr)
        return 2
    report = evaluate(clips, sensitivity=options.sensitivity, stay_down=options.stay_down, fps=options.fps, tolerance=options.tolerance)
    print(render(report))
    if options.json:
        options.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
