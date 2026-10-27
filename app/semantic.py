"""Natural-language search (#127): a plain description in, ranked tracks out.

A query such as "white van at the gate after 6 pm" is split into what narrows the search and what describes the object:

* time and camera phrases ("yesterday", "last 2 hours", "after 6 pm", "between 6 and 8 pm", "in the evening", "at the gate", a camera or camera
  group by name) narrow the range, the time of day and the cameras;
* words for what the object is and looks like (person, van, "red jacket", "heading left") become the attribute filters of #125, applied as
  a label that must match and as attributes that raise the score;
* what is left is embedded by the administrator's CLIP-family model (``clip``) and compared with each track's image embedding.

Without a model, results are ranked by how many of the described attributes each track has, and the answer says so. Everything is
computed in this process, so no query, picture or embedding leaves the server. Every answer lists the cameras and time not analysed.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import clip
from .attributes import PALETTE
from .search import _TRACK_COLUMNS, _iso, _result

MAX_QUERY = 300
MAX_CANDIDATES = 5000
FACET_BONUS = 0.1
UPPER_WORDS = {"jacket", "shirt", "top", "coat", "hoodie", "sweater", "jumper", "tshirt", "t-shirt", "sweatshirt", "vest", "blouse", "dress"}
LOWER_WORDS = {"jeans", "trousers", "pants", "shorts", "skirt", "leggings"}
VEHICLE_WORDS = {"van": "van", "minivan": "van", "car": "car", "sedan": "car", "suv": "car", "hatchback": "car", "truck": "truck", "lorry": "truck", "pickup": "truck",
                 "bus": "bus", "coach": "bus"}
VEHICLE_ANY = {"vehicle", "vehicles", "automobile"}
PERSON_WORDS = {"person", "people", "man", "men", "woman", "women", "pedestrian", "pedestrians", "someone", "guy", "child", "kid", "walker", "driver"}
COLOUR_ALIASES = {"grey": "gray", "silver": "gray", "navy": "blue", "violet": "purple"}
GENERIC_CAMERA_WORDS = {"camera", "cam", "the", "a", "an", "of", "and"}
PARTS_OF_DAY = {"morning": (5 * 60, 12 * 60), "afternoon": (12 * 60, 17 * 60), "evening": (17 * 60, 22 * 60), "night": (22 * 60, 5 * 60)}
DAY = 24 * 60
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"


@dataclass
class Parsed:
    text: str = ""
    camera_ids: list[str] = field(default_factory=list)
    label: str | None = None
    facets: dict[str, str] = field(default_factory=dict)
    any_colours: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
    time_of_day: tuple[int, int] | None = None  # Local minutes from midnight; the end is before the start for a window that crosses midnight
    notes: list[str] = field(default_factory=list)

    @property
    def facet_count(self) -> int:
        return len(self.facets) + len(self.any_colours)


def _minutes(hour: str, minute: str | None, meridiem: str | None) -> int | None:
    value = int(hour)
    if meridiem:
        if not 1 <= value <= 12:
            return None
        value = value % 12 + (12 if meridiem.startswith("p") else 0)
    elif value > 24:
        return None
    return value * 60 + int(minute or 0) if value * 60 + int(minute or 0) <= DAY else None


def _clock(text: str) -> str:
    return f"{text // 60 % 24:02d}:{text % 60:02d}" if text < DAY else "24:00"


def describe_window(window: tuple[int, int]) -> dict[str, str]:
    return {"from": _clock(window[0]), "to": _clock(window[1])}


def _cut(text: str, span: tuple[int, int]) -> str:
    return text[:span[0]] + " " + text[span[1]:]


def _parse_time(text: str, parsed: Parsed, now: datetime, offset: timedelta) -> str:
    """Range and time-of-day phrases; returns the text with the phrases removed."""
    local = now + offset
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)

    def take(pattern: str) -> re.Match[str] | None:
        return re.search(pattern, text, re.IGNORECASE)

    if match := take(r"\byesterday\b"):
        parsed.start, parsed.end = midnight - timedelta(days=1) - offset, midnight - offset
        text = _cut(text, match.span())
    elif match := take(r"\blast night\b"):
        parsed.start, parsed.end = midnight - timedelta(hours=6) - offset, midnight + timedelta(hours=6) - offset
        text = _cut(text, match.span())
    elif match := take(r"\bthis (morning|afternoon|evening)\b"):
        begin, finish = PARTS_OF_DAY[match.group(1)]
        parsed.start, parsed.end = midnight + timedelta(minutes=begin) - offset, midnight + timedelta(minutes=finish) - offset
        text = _cut(text, match.span())
    elif match := take(r"\btoday\b"):
        parsed.start, parsed.end = midnight - offset, now
        text = _cut(text, match.span())
    elif match := take(r"\b(?:in |over |during )?the (?:last|past) (?:(\d{1,3}) )?(minute|hour|day|week)s?\b"):
        amount = int(match.group(1) or 1)
        unit = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}[match.group(2)]
        parsed.start, parsed.end = now - amount * unit, now
        text = _cut(text, match.span())
    elif match := take(r"\b(?:in |over |during )?(?:last|past) (?:(\d{1,3}) )?(minute|hour|day|week)s?\b"):
        amount = int(match.group(1) or 1)
        unit = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}[match.group(2)]
        parsed.start, parsed.end = now - amount * unit, now
        text = _cut(text, match.span())
    # An hour is read as 24-hour unless am or pm is given ("after 18:00", "before 9 am").
    if match := take(rf"\bbetween {_CLOCK} and {_CLOCK}"):
        second = _minutes(match.group(4), match.group(5), match.group(6))
        first = _minutes(match.group(1), match.group(2), match.group(3) or match.group(6))
        if first is not None and second is not None:
            parsed.time_of_day = (first, second)
            text = _cut(text, match.span())
    elif match := take(rf"\b(after|since|from) {_CLOCK}"):
        value = _minutes(match.group(2), match.group(3), match.group(4))
        if value is not None:
            parsed.time_of_day = (value, DAY)
            text = _cut(text, match.span())
    elif match := take(rf"\b(before|until|till|by) {_CLOCK}"):
        value = _minutes(match.group(2), match.group(3), match.group(4))
        if value is not None:
            parsed.time_of_day = (0, value)
            text = _cut(text, match.span())
    elif match := take(r"\b(?:in the|during the|at) (morning|afternoon|evening|night)\b"):
        parsed.time_of_day = PARTS_OF_DAY[match.group(1)]
        text = _cut(text, match.span())
    return text


def _words(name: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", name.lower()) if word not in GENERIC_CAMERA_WORDS]


def _parse_cameras(text: str, parsed: Parsed, cameras: dict[str, str], groups: dict[str, tuple[str, list[str]]]) -> str:
    """Camera and camera-group names in the query. The whole name wins; otherwise a word that tells cameras apart (not one every name has)."""
    lowered = text.lower()
    chosen: list[str] = []
    matched_span: tuple[int, int] | None = None
    for camera_id, name in cameras.items():
        if (found := re.search(rf"\b{re.escape(name.lower())}\b", lowered)):
            chosen.append(camera_id)
            matched_span = matched_span or found.span()
    if not chosen:
        for _, (name, members) in groups.items():
            if (found := re.search(rf"\b{re.escape(name.lower())}\b", lowered)):
                chosen.extend(members)
                matched_span = found.span()
                break
    if not chosen:
        shared = set.intersection(*(set(_words(name)) for name in cameras.values())) if cameras else set()
        for camera_id, name in cameras.items():
            distinct = [word for word in _words(name) if word not in shared and not word.isdigit()]
            for word in distinct:
                if (found := re.search(rf"\b{re.escape(word)}\b", lowered)):
                    chosen.append(camera_id)
                    matched_span = matched_span or found.span()
                    break
    if chosen and matched_span:
        parsed.camera_ids = list(dict.fromkeys(chosen))
        # Take the whole preposition phrase out ("at the gate"), leaving the description of the object.
        start = matched_span[0]
        lead = re.search(r"(?:\b(?:at|on|near|by|in|from|outside|inside|around)\s+(?:the\s+)?)$", lowered[:start])
        text = _cut(text, (lead.start() if lead else start, matched_span[1]))
    return text


def _parse_attributes(text: str, parsed: Parsed) -> None:
    words = re.findall(r"[a-z][a-z-]*", text.lower())
    for index, word in enumerate(words):
        if word in VEHICLE_WORDS:
            parsed.label = "vehicle"
            parsed.facets.setdefault("vehicle_type", VEHICLE_WORDS[word])
        elif word in VEHICLE_ANY:
            parsed.label = parsed.label or "vehicle"
        elif word in PERSON_WORDS and parsed.label != "vehicle":
            parsed.label = "person"
    for index, word in enumerate(words):
        colour = COLOUR_ALIASES.get(word, word)
        if colour not in PALETTE:
            continue
        following = [COLOUR_ALIASES.get(w, w) for w in words[index + 1:index + 3]]
        near = set(following) | {w for w in words[index + 1:index + 3]}
        if near & UPPER_WORDS:
            parsed.facets.setdefault("upper_colour", colour)
            parsed.label = parsed.label or "person"
        elif near & LOWER_WORDS:
            parsed.facets.setdefault("lower_colour", colour)
            parsed.label = parsed.label or "person"
        elif near & (set(VEHICLE_WORDS) | VEHICLE_ANY):
            parsed.facets.setdefault("vehicle_colour", colour)
            parsed.label = "vehicle"
        elif colour not in parsed.any_colours:
            parsed.any_colours.append(colour)
    lowered = text.lower()
    if re.search(r"\b(?:toward|towards|approaching|coming toward)\b", lowered):
        parsed.facets["direction"] = "toward"
    elif re.search(r"\b(?:away from|leaving|walking away|driving away)\b", lowered):
        parsed.facets["direction"] = "away"
    elif re.search(r"\b(?:stationary|parked|standing still|not moving|loitering|standing)\b", lowered):
        parsed.facets["direction"] = "stationary"
    elif (moving := re.search(r"\b(?:going|heading|moving|walking|driving|travelling|traveling|running)\s+(?:to the\s+|towards the\s+)?(left|right)\b", lowered)):
        parsed.facets["direction"] = moving.group(1)


def parse(text: str, cameras: dict[str, str], groups: dict[str, tuple[str, list[str]]], *, now: datetime, utc_offset_minutes: int = 0) -> Parsed:
    """What a description asks for. ``cameras`` and ``groups`` hold only those the person may see, so a camera they may not see is never recognised."""
    parsed = Parsed()
    remaining = _parse_time(" ".join(text.split())[:MAX_QUERY], parsed, now, timedelta(minutes=utc_offset_minutes))
    remaining = _parse_cameras(remaining, parsed, cameras, groups)
    _parse_attributes(remaining, parsed)
    remaining = re.sub(r"\b(?:at|on|in|from|near|by|around|during|the|a|an)\s*$", "", " ".join(remaining.split()), flags=re.IGNORECASE)
    remaining = re.sub(r"^\s*(?:show me|find|look for|search for|any|a|an|the)\b\s*", "", remaining, flags=re.IGNORECASE)
    parsed.text = " ".join(remaining.split())
    return parsed


def _matches(row: sqlite3.Row, parsed: Parsed) -> int:
    count = sum(1 for column, wanted in parsed.facets.items() if row[column] == wanted)
    return count + sum(1 for colour in parsed.any_colours if colour in (row["upper_colour"], row["lower_colour"], row["vehicle_colour"]))


def rank(db: sqlite3.Connection, parsed: Parsed, camera_ids: list[str], start: datetime, end: datetime, utc_offset_minutes: int, model: clip.ClipModel | None,
         limit: int) -> dict[str, Any]:
    """The best ``limit`` tracks for a parsed description among these cameras in range, with scores, how they were ranked, and how many were never indexed."""
    mode = "image-text" if model is not None and parsed.text else "attributes" if parsed.facet_count or parsed.label else "browse"
    if not camera_ids:
        return {"mode": mode, "results": [], "candidates": 0, "unindexed": 0, "candidates_truncated": False}
    clauses = [f"t.camera_id IN ({','.join('?' * len(camera_ids))})", "t.last_seen_at>=?", "t.first_seen_at<?"]
    values: list[Any] = [*camera_ids, _iso(start), _iso(end)]
    if parsed.label:
        clauses.append("t.label=?")
        values.append(parsed.label)
    if parsed.time_of_day:
        first, last = parsed.time_of_day
        local = (f"(CAST(strftime('%H',t.first_seen_at,'{utc_offset_minutes:+d} minutes') AS INTEGER)*60+"
                 f"CAST(strftime('%M',t.first_seen_at,'{utc_offset_minutes:+d} minutes') AS INTEGER))")
        clauses.append(f"({local}>=? AND {local}<?)" if first <= last else f"({local}>=? OR {local}<?)")
        values.extend([first, last])
    rows = db.execute(f"{_TRACK_COLUMNS} WHERE {' AND '.join(clauses)} ORDER BY t.first_seen_at DESC,t.id DESC LIMIT ?", (*values, MAX_CANDIDATES + 1)).fetchall()
    truncated = len(rows) > MAX_CANDIDATES
    rows = rows[:MAX_CANDIDATES]
    clip_scores: dict[str, float] = {}
    if mode == "image-text":
        query = model.embed_text(parsed.text) if model else None
        if query is not None:
            clip_scores = clip.scores(db, model, query, [row["id"] for row in rows])  # type: ignore[arg-type]
        else:
            mode = "attributes" if parsed.facet_count or parsed.label else "browse"
    scored: list[tuple[float, str, sqlite3.Row]] = []
    for row in rows:
        matched = _matches(row, parsed)
        if mode == "image-text":
            score = max(0.0, clip_scores.get(row["id"], 0.0)) + FACET_BONUS * matched
        elif mode == "attributes":
            if parsed.facet_count and matched == 0:
                continue
            score = matched / parsed.facet_count if parsed.facet_count else 1.0
        else:
            score = 0.0
        scored.append((score, row["first_seen_at"], row))
    scored.sort(key=lambda item: (-item[0], _negate(item[1])))
    results = []
    for position, (score, _, row) in enumerate(scored[:limit], start=1):
        results.append({**_result(row), "score": None if mode == "browse" else round(min(1.0, score), 3), "rank": position})
    unindexed = sum(1 for row in rows if mode == "image-text" and row["id"] not in clip_scores)
    return {"mode": mode, "results": results, "candidates": len(rows), "unindexed": unindexed, "candidates_truncated": truncated}


def _negate(moment: str) -> tuple[int, ...]:
    """A sort key that puts later times first when scores tie."""
    return tuple(-ord(char) for char in moment)


def interpretation(parsed: Parsed, cameras: dict[str, str]) -> dict[str, Any]:
    return {
        "text": parsed.text, "label": parsed.label, "attributes": {**parsed.facets, **({"any_colour": parsed.any_colours} if parsed.any_colours else {})},
        "cameras": [{"id": camera_id, "name": cameras[camera_id]} for camera_id in parsed.camera_ids if camera_id in cameras],
        "range": {"start": parsed.start.astimezone(timezone.utc).isoformat(), "end": parsed.end.astimezone(timezone.utc).isoformat()} if parsed.start and parsed.end else None,
        "time_of_day": describe_window(parsed.time_of_day) if parsed.time_of_day else None,
    }
