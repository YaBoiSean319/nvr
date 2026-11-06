"""Motion clip labels (#254): what the AI found in the time a clip covers."""
from __future__ import annotations

import unittest

from app import clip_labels


def event(start: str, end: str, **extra):
    return {"id": "m", "source_event_at": start, "actual_start": start, "actual_end": end, "requested_start": None, "requested_end": None, **extra}


def track(label: str, first: str, last: str):
    return {"label": label, "first_seen_at": first, "last_seen_at": last}


class ClipLabelTests(unittest.TestCase):
    WATCHED = [{"starts_at": "2026-11-06T00:00:00+00:00", "ends_at": None}]  # analysing since midnight, still running

    def label(self, events, tracks=(), analysed=WATCHED):
        clip_labels.label_events(events, list(tracks), list(analysed))
        return [item["ai"] for item in events]

    def test_a_clip_with_a_person_and_a_vehicle_lists_both_in_a_fixed_order(self):
        result = self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")],
                            [track("vehicle", "2026-11-06T12:00:10+00:00", "2026-11-06T12:00:20+00:00"), track("person", "2026-11-06T12:00:05+00:00", "2026-11-06T12:00:25+00:00")])
        self.assertEqual(result, [{"classes": ["person", "vehicle"], "checked": True}])

    def test_a_clip_the_ai_watched_throughout_that_found_no_one_is_labelled_nothing(self):
        self.assertEqual(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], [track("person", "2026-11-06T11:00:00+00:00", "2026-11-06T11:00:10+00:00")]),
                         [{"classes": [], "checked": True}])

    def test_a_clip_the_ai_was_not_watching_for_part_of_its_time_is_not_labelled_nothing(self):
        stopped = [{"starts_at": "2026-11-06T00:00:00+00:00", "ends_at": "2026-11-06T12:00:20+00:00"}]
        self.assertEqual(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], analysed=stopped), [{"classes": [], "checked": False}])

    def test_a_camera_the_ai_never_watched_is_not_labelled_nothing(self):
        self.assertEqual(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], analysed=[]), [{"classes": [], "checked": False}])

    def test_touching_analysed_intervals_count_as_one(self):
        touching = [{"starts_at": "2026-11-06T11:00:00+00:00", "ends_at": "2026-11-06T12:00:10+00:00"},
                    {"starts_at": "2026-11-06T12:00:10+00:00", "ends_at": "2026-11-06T13:00:00+00:00"}]
        self.assertTrue(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], analysed=touching)[0]["checked"])

    def test_a_gap_between_analysed_intervals_inside_the_clip_makes_it_unchecked(self):
        split = [{"starts_at": "2026-11-06T11:00:00+00:00", "ends_at": "2026-11-06T12:00:10+00:00"},
                 {"starts_at": "2026-11-06T12:00:20+00:00", "ends_at": "2026-11-06T13:00:00+00:00"}]
        self.assertFalse(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], analysed=split)[0]["checked"])

    def test_a_track_just_outside_the_clip_still_counts(self):
        near = track("person", "2026-11-06T12:00:32+00:00", "2026-11-06T12:00:40+00:00")
        far = track("vehicle", "2026-11-06T12:01:00+00:00", "2026-11-06T12:01:10+00:00")
        self.assertEqual(self.label([event("2026-11-06T12:00:00+00:00", "2026-11-06T12:00:30+00:00")], [near, far])[0]["classes"], ["person"])

    def test_an_event_without_a_recorded_span_uses_what_was_asked_for_then_the_moment(self):
        asked = {"id": "m", "source_event_at": "2026-11-06T12:00:10+00:00", "actual_start": None, "actual_end": None,
                 "requested_start": "2026-11-06T12:00:00+00:00", "requested_end": "2026-11-06T12:00:30+00:00"}
        bare = {"id": "n", "source_event_at": "2026-11-06T12:00:10+00:00", "actual_start": None, "actual_end": None, "requested_start": None, "requested_end": None}
        person = track("person", "2026-11-06T12:00:25+00:00", "2026-11-06T12:00:28+00:00")
        self.assertEqual([item["classes"] for item in self.label([asked, bare], [person])], [["person"], []])


if __name__ == "__main__":
    unittest.main()
