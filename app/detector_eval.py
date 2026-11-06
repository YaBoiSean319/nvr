"""Measure how well the person detector finds people on frames from the site's own cameras (#255).

Why: people in wheelchairs (and anyone sitting, small, far away or half hidden) are the cases a pedestrian detector is likeliest to miss, and no published number says how
it does on this site's cameras. This takes frames and a person-by-person list of where the people are (made with ``tools/frame_labeler.html``) and says, for a range of
confidence thresholds, how many people were found, how many false alarms there were, and, for every person missed, how confident the model was about the place they
stand (a person the model gave 0.45 is found by lowering the threshold to 0.4; one it gave nothing is not found by any setting, and needs a better model or training).

    python -m app.detector_eval extract recording.mp4 frames --every 3 --limit 200
    python -m app.detector_eval score frames labels.json

Pure scoring is separate from the model, so it is tested without one, and the same scoring will compare other models later.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .detection import MODEL_FRAME_BYTES, MODEL_HEIGHT, MODEL_WIDTH, box_iou

LOW = 0.05  # The model is asked for everything it half-sees; the thresholds are applied here
THRESHOLDS = (0.3, 0.4, 0.5, 0.55, 0.6, 0.7)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class Truth:
    box: dict[str, float]
    tag: str  # "person" unless the labeller said more, for example "wheelchair"


def load_labels(path: Path) -> dict[str, list[Truth]]:
    """``{"frames": {"frame_00001.jpg": [{"box": [x, y, width, height], "tag": "wheelchair"}, ...]}}``, boxes in fractions of the picture."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, dict) or not frames:
        raise ValueError("The labels file needs a \"frames\" object with at least one frame")
    labelled: dict[str, list[Truth]] = {}
    for name, items in frames.items():
        truths = []
        for item in items:
            box = item.get("box") if isinstance(item, dict) else None
            if not isinstance(box, list) or len(box) != 4 or not all(isinstance(value, (int, float)) for value in box) or box[2] <= 0 or box[3] <= 0:
                raise ValueError(f"{name}: each person needs a box [x, y, width, height] in fractions of the picture")
            truths.append(Truth({"x": float(box[0]), "y": float(box[1]), "width": float(box[2]), "height": float(box[3])}, str(item.get("tag") or "person")))
        labelled[str(name)] = truths
    return labelled


Detections = list[dict[str, Any]]


def people(detections: Detections, threshold: float = 0.0) -> Detections:
    return sorted((item for item in detections if item["label"] == "person" and item["confidence"] >= threshold), key=lambda item: -item["confidence"])


def best_scores(detections: Detections, truths: list[Truth], iou_min: float) -> list[float | None]:
    """For each person, the highest confidence the model gave anything that overlaps them enough: None when it saw nothing there at all."""
    scores: list[float | None] = []
    for truth in truths:
        overlapping = [item["confidence"] for item in people(detections) if box_iou(item["box"], truth.box) >= iou_min]
        scores.append(max(overlapping) if overlapping else None)
    return scores


def match(detections: Detections, truths: list[Truth], threshold: float, iou_min: float) -> tuple[list[bool], int]:
    """Which people were found at this threshold (each detection counts for one person, the most confident first) and how many detections found nobody."""
    found = [False] * len(truths)
    false_alarms = 0
    for item in people(detections, threshold):
        best, best_overlap = None, iou_min
        for index, truth in enumerate(truths):
            overlap = box_iou(item["box"], truth.box)
            if not found[index] and overlap >= best_overlap:
                best, best_overlap = index, overlap
        if best is None:
            false_alarms += 1
        else:
            found[best] = True
    return found, false_alarms


@dataclass
class Row:
    threshold: float
    people: int
    found: int
    false_alarms: int
    frames: int
    by_tag: dict[str, tuple[int, int]]  # tag -> (found, total)

    @property
    def recall(self) -> float:
        return self.found / self.people if self.people else 0.0


def evaluate(frames: dict[str, tuple[list[Truth], Detections]], thresholds: tuple[float, ...], iou_min: float = 0.5) -> list[Row]:
    rows = []
    for threshold in thresholds:
        found_total = people_total = alarms = 0
        by_tag: dict[str, list[int]] = {}
        for truths, detections in frames.values():
            found, false_alarms = match(detections, truths, threshold, iou_min)
            alarms += false_alarms
            for truth, hit in zip(truths, found):
                people_total += 1
                found_total += int(hit)
                counts = by_tag.setdefault(truth.tag, [0, 0])
                counts[0] += int(hit)
                counts[1] += 1
        rows.append(Row(threshold, people_total, found_total, alarms, len(frames), {tag: (counts[0], counts[1]) for tag, counts in sorted(by_tag.items())}))
    return rows


def misses(frames: dict[str, tuple[list[Truth], Detections]], threshold: float, iou_min: float = 0.5) -> list[dict[str, Any]]:
    """Every person not found at this threshold, with the best confidence the model had for their place (None: nothing there)."""
    result = []
    for name, (truths, detections) in frames.items():
        found, _ = match(detections, truths, threshold, iou_min)
        scores = best_scores(detections, truths, iou_min)
        for truth, hit, score in zip(truths, found, scores):
            if not hit:
                result.append({"frame": name, "tag": truth.tag, "box": truth.box, "best_score": score})
    return result


def report(rows: list[Row], missed: list[dict[str, Any]], threshold: float, milliseconds: float | None) -> str:
    tags = sorted({tag for row in rows for tag in row.by_tag})
    lines = [f"{rows[0].people} people on {rows[0].frames} frames" + (f", {milliseconds:.0f} ms a frame on this machine" if milliseconds is not None else ""), "",
             "threshold  found           false alarms a frame" + "".join(f"  {tag:<12}" for tag in tags if tag != "person")]
    for row in rows:
        extra = "".join(f"  {row.by_tag.get(tag, (0, 0))[0]}/{row.by_tag.get(tag, (0, 0))[1]:<{max(1, 11 - len(str(row.by_tag.get(tag, (0, 0))[0])))}}" for tag in tags if tag != "person")
        lines.append(f"{row.threshold:<9.2f}  {row.found:>3}/{row.people:<3} {row.recall * 100:>5.1f}%   {row.false_alarms / row.frames if row.frames else 0:>10.2f}{extra}")
    lines += ["", f"Missed at {threshold:.2f} ({len(missed)}): the model's best confidence for each place, or none when it saw nothing there"]
    for item in missed[:60]:
        score = "nothing seen" if item["best_score"] is None else f"best {item['best_score']:.2f}"
        box = item["box"]
        lines.append(f"  {item['frame']}  {item['tag']:<10} at x {box['x']:.2f}, y {box['y']:.2f}, {box['width']:.2f} wide  ({score})")
    if len(missed) > 60:
        lines.append(f"  ... and {len(missed) - 60} more")
    seen = [item for item in missed if item["best_score"] is not None]
    if missed:
        lines += ["", f"{len(seen)} of {len(missed)} misses were scored below the threshold (a lower threshold finds them); {len(missed) - len(seen)} were not seen at all (only a better model finds those)."]
    return "\n".join(lines)


def decode_frame(path: Path) -> bytes:
    """The picture as the analysis worker sees it: scaled to the model's size the same way."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is required")
    result = subprocess.run([executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", f"scale={MODEL_WIDTH}:{MODEL_HEIGHT}:flags=bilinear",
                             "-frames:v", "1", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"], capture_output=True, timeout=60)
    if result.returncode != 0 or len(result.stdout) != MODEL_FRAME_BYTES:
        raise RuntimeError(f"{path.name} could not be read as a picture")
    return result.stdout


def detect_all(folder: Path, names: list[str], infer: Callable[[bytes, float], Detections], decode: Callable[[Path], bytes] = decode_frame) -> tuple[dict[str, Detections], float]:
    detections: dict[str, Detections] = {}
    spent = 0.0
    for name in names:
        data = decode(folder / name)
        started = time.monotonic()
        detections[name] = infer(data, LOW)
        spent += time.monotonic() - started
    return detections, (spent / len(names) * 1000 if names else 0.0)


def extract(video: Path, folder: Path, every: float, limit: int) -> int:
    """Pull a still picture every ``every`` seconds from a recording, at its full size, for labelling."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is required")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf", f"fps=1/{every}", "-frames:v", str(limit), "-q:v", "3",
                    str(folder / "frame_%05d.jpg")], check=True, timeout=1800)
    return len(list(folder.glob("frame_*.jpg")))


def check_inputs(folder: Path, labels: Path) -> dict[str, list[Truth]]:
    """The labels, once every picture they name is known to be there (before anything slow starts)."""
    labelled = load_labels(labels)
    missing = [name for name in labelled if not (folder / name).is_file()]
    if missing:
        raise ValueError(f"{len(missing)} labelled pictures are not in {folder}: {', '.join(missing[:5])}")
    return labelled


def run_score(folder: Path, labels: Path, infer: Callable[[bytes, float], Detections], thresholds: tuple[float, ...], iou_min: float, decode: Callable[[Path], bytes] = decode_frame) -> str:
    labelled = check_inputs(folder, labels)
    detections, milliseconds = detect_all(folder, list(labelled), infer, decode)
    frames = {name: (truths, detections[name]) for name, truths in labelled.items()}
    rows = evaluate(frames, thresholds, iou_min)
    return report(rows, misses(frames, 0.55 if 0.55 in thresholds else thresholds[len(thresholds) // 2], iou_min), 0.55 if 0.55 in thresholds else thresholds[len(thresholds) // 2], milliseconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.detector_eval", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("extract", help="Pull still pictures out of a recording, for labelling")
    pull.add_argument("video", type=Path)
    pull.add_argument("folder", type=Path)
    pull.add_argument("--every", type=float, default=3.0, help="seconds between pictures (default 3)")
    pull.add_argument("--limit", type=int, default=200, help="most pictures to take (default 200)")
    score = commands.add_parser("score", help="Score the person detector against a labels file")
    score.add_argument("folder", type=Path)
    score.add_argument("labels", type=Path)
    score.add_argument("--provider", default="CPU", choices=["CPU", "GPU"])
    score.add_argument("--thresholds", default=",".join(str(value) for value in THRESHOLDS))
    score.add_argument("--iou", type=float, default=0.5, help="how much a box must overlap a person to count as finding them (default 0.5)")
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            print(f"{extract(args.video, args.folder, args.every, args.limit)} pictures in {args.folder}")
            return 0
        check_inputs(args.folder, args.labels)
        from .detection import MODEL_PATH, OpenVINODetector

        detector = OpenVINODetector(provider=args.provider, model_path=MODEL_PATH)
        thresholds = tuple(float(value) for value in args.thresholds.split(","))
        print(run_score(args.folder, args.labels, detector.infer, thresholds, args.iou))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
