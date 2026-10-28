# Edge backfill from the camera's own recording

When the NVR misses footage (the network dropped, the camera's stream failed, or the NVR itself was down), a camera with an SD card or other storage usually still recorded it. **Backfill** fetches that span from the camera over ONVIF Profile G and adds it to the timeline, marked as backfilled.

Turn it on per camera: **Settings > Cameras > a camera > Recording > Backfill from the camera's own recording**, with an optional bandwidth limit. It is off by default. Administrators change it; everyone who may see the camera sees what it did.

## What it fills

- Outages the coverage record explains by the camera, its stream or the NVR (*Camera offline*, *Stream error*, *Recorder restarting*, *Unknown*) in the last 24 hours, at least 30 seconds long and not still going on (an outage ending within 90 seconds of now is left until it has clearly ended). Time before the camera was added is not an outage.
- Gaps the camera's schedule or policy expects (*Outside schedule*, *Recording disabled*, *No motion*) are never filled, and *Disk full or unavailable* is not tried: there is nowhere to write.

## How

1. The camera's **Search** service lists its recordings and the time each covers (`FindRecordings`, `GetRecordingSearchResults`), and its **Replay** service gives the address to play one from (`GetReplayUri`). The address is used on the host entered for the camera, and never logged.
2. The span is copied in chunks (the first 10 seconds, then 60 at a time) by FFmpeg with `-c copy`: **no re-encoding**. Each chunk becomes a segment in the index (`source: backfill`, file `backfill-<time>.mp4`) that plays, exports, is held and is deleted by retention like any other.
3. Live footage that was indexed in the gap meanwhile is never fetched over; a chunk that comes out shorter than asked is followed by one that starts where it ended; the camera's replay address, which carries the login, reaches FFmpeg over stdin and is not in its arguments.
4. The camera's own recording may not cover the whole gap (its card was removed, or recorded over). What it has is filled and the job is *partly filled*; a gap it has nothing for is *failed* and not retried.

Chunk boundaries follow the nominal times of the request; copying without re-encoding starts at the camera's key frame at or before the time asked, so a backfilled segment can be off by up to one key-frame interval (typically 1-2 seconds).

## Bandwidth, resuming, failures

- One chunk is fetched at a time across all cameras. FFmpeg reads at the rate that holds the stream at or under the camera's limit (default 8192 kbit/s, 64 to 1,000,000), worked out from the bitrate of the first chunk (that short first chunk is fetched at real-time speed). Each job shows the rate it observed.
- A job records the point it has filled up to. After an interruption, a restart, or backfill being switched off and on, it carries on from there; chunks already stored are not fetched again.
- A transient failure (camera unreachable, replay refused) is tried again after one minute, doubling up to 30 minutes, and given up on after six tries ("Gave up after 6 tries"). The chunks already fetched stay.

## Cameras without Profile G

A camera that offers no Search or Replay service, or has no ONVIF address, is found out as soon as backfill is switched on for it (or at its first outage, or by **Check the camera**) and recorded once as **"Backfill not supported"** (the message is on the camera's backfill settings and on its gaps) and is **not asked again**: no retries. **Check the camera** asks now; switching backfill off and on again does too, and also gives jobs that gave up another go. A camera that cannot be reached at the time is not called unsupported: it is retried as above.

## Where it shows

- If retention later deletes backfilled footage the gap comes back, marked *removed*, and is not fetched again.
- The timeline marks backfilled footage with a hatched green bar (legend: *Backfilled from the camera*); its tooltip says it came from the camera's own recording. A gap's tooltip says what backfill is doing about it (*waiting*, *running*, *partly filled*, *failed*, *unsupported*, or *off*).
- `POST /api/playback/timeline` segments carry `source` (`live` or `backfill`) and gaps carry `backfill` (`{state, message}` or null). `GET /api/cameras/{id}/coverage` lists `backfilled` segments; the coverage report has `backfilled_seconds` per camera (the time counted as covered that came from the camera).
- `GET /api/cameras/{id}/backfill` shows the setting, support and recent jobs; `PUT` (administrator) sets `enabled` and `limit_kbps`; `POST .../backfill/check` checks support. Changes are in the audit log (`backfill_settings_changed`).

## Limits

Tested with an ONVIF fixture that serves a recording over HTTP. A real camera's replay is RTSP; the NVR seeks in it with FFmpeg (`-ss` sends an `npt` range), which depends on the camera honouring it. Validate with your camera model (see the real-camera checklist) before relying on backfill.
