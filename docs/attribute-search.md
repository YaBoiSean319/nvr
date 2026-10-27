# Attribute search across cameras

Playback > **Find by attributes…** finds people and vehicles across cameras by what they looked like and how they moved, and opens Playback at the moment of any result.

## Filters

- **Kind**: person or vehicle.
- **Top colour** and **lower colour** (people), **vehicle colour**, from a palette of eleven names: black, white, gray, red, orange, yellow, green, blue, purple, pink, brown.
- **Vehicle type**: car, bus, truck or van. This needs the vehicle attribute model (see below); without it the filter is switched off and says why.
- **Direction**: left, right, toward the camera (down the picture), away (up the picture) or not moving.
- **Passed through**: anywhere, or the left, right, top or bottom half or the centre of the picture.
- **Cameras**: every camera you can see, or the cameras in the current Playback view. **When**: the last hour, day, week or 30 days, or the current Playback search range.

Results are the tracks that matched, with the picture the object was first seen in (the box drawn on it), the camera, the time (site time) and what was found, newest first. Choosing one opens Playback at that moment with the camera selected. A search shows up to 60 results and says when more matched.

## What was not analysed

Above the results, the panel says how much of the searched time was analysed and lists, for each camera, the time that was not: AI analysis failed, was delayed, interrupted or skipped, or **was not running at all** (it was switched off, or never on). A search that finds nothing in time that was not analysed has not looked: the panel says so rather than leaving "no match" to be read as "nothing there". The percentage counts every searched camera equally over the range; time still to come is not counted.

## How the attributes are worked out

The AI worker describes every detection from the frame it was found in and keeps, on the track, what most of its detections agreed on (the earlier value wins a tie):

- **Colours** come from the pixels. A person's top is the torso region of the box and the lower colour the legs; a vehicle's colour is the middle of its body. The dominant palette colour of the region is taken, so a flat red jacket is red whatever its shade. Light, shadow, night footage and similar colours (pink and red, brown and orange) make this approximate.
- **Direction** is where the track went across the picture, from where it first appeared to where it was last seen; moving less than 8% of the picture is "not moving".
- **Passed through** uses a 6×6 grid of the places the track's centre went, then checks the exact path: a track matches a zone when the centre of one of its detections was inside it.
- **Vehicle type** (and a second opinion on the vehicle's colour) come from the OpenVINO model `vehicle-attributes-recognition-barrier-0039` from the Open Model Zoo, when its `.xml` and `.bin` files are installed at `/opt/nvr/models` (or the path in `NVR_AI_ATTRIBUTE_MODEL_PATH`). It is **not bundled**: its files are not downloaded by the image build because no reviewed checksum is pinned for them yet (see `docs/acceptance/roadmap.md`). Install them beside the detection model and restart the AI worker.

Tracks recorded before this feature have no attributes and are never matched by an attribute filter. Attributes describe appearance at a glance; nothing here identifies a person, and results are search aids to check against the footage.

## Who sees what

- A search covers only the cameras the person may see (access grants). Asking for a camera they cannot see finds nothing and does not reveal that it exists. Thumbnails of a camera they cannot see do not exist for them.
- The API is a footage read: a signed-in user, or an API token with the `read` scope.
- Thumbnails are the stored frame with the camera's privacy masks drawn over it; if a mask cannot be drawn there is no picture.

## API

```http
GET /api/ai/search?start=<iso>&end=<iso>[&camera_ids=<id>&camera_ids=<id>][&label=person|vehicle][&upper_colour=red][&lower_colour=blue]
    [&vehicle_colour=white][&vehicle_type=car][&direction=left][&zone=left|right|top|bottom|centre|x,y,width,height][&order=desc|asc][&limit=60]
GET /api/ai/search/options
GET /api/ai/tracks/<track-id>/thumbnail
```

`start` and `end` carry a time zone and span at most 92 days. The search response holds `results`, `truncated`, and `coverage` (`analysed_percent`, `fully_analysed` and, for each camera with unanalysed time, `unanalysed_seconds` and its `intervals`, each with `state`, `starts_at`, `ends_at` and `detail`). `GET /api/ai/tracks` and `/api/ai/tracks/<id>` now include each track's `attributes`.

## Speed

On a CI runner with 30 days of fixture tracks for 50 cameras (150,000 tracks, 300,000 detections) each search below, including its coverage, answered in well under two seconds: unfiltered 88-131 ms; filtered by kind, colour and direction about 90 ms; with a zone and newest first about 0.5 s. These are measurements from a generated fixture on a CI runner, not a claim about any deployment. The test (`tests/test_attribute_search.py`, `ScaleTests`) fails if a search exceeds two seconds and prints the timings.
