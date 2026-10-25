# Viewing audit

The NVR records who watched which camera, from when to when, and every export, download and snapshot. An administrator reads it under **Settings › Viewing audit**, as a report per person or per camera for a date range, and can download it as CSV.

## What is recorded

| Entry | When it is written | Length |
|---|---|---|
| **Live** | Video or audio starts being delivered (WebSocket video, the motion-JPEG fallback, or listening to a camera's audio on its own, which has quality `audio`) and ends when delivery stops. Quality (`low`/`high`) is kept. A tile that is refused or never delivers anything (no access, camera archived, no slot, ffmpeg missing) is not a viewing. | First delivery to closing |
| **Playback** | A recording is served. Requests by one person for one camera belong to one session until two minutes pass without one. A recording that could not be served is not recorded. | First request to last request |
| **Export** | An export is requested, once per camera it covers. | – |
| **Download** | An evidence package is downloaded, once per camera it covers. A resumed or ranged download within ten minutes is the same download. | – |
| **Snapshot** | A snapshot is extracted from playback. | – |

Playback is counted from the requests for recordings, so a segment someone opened and left paused counts for as long as the session lasts, and a session of one segment has no length. Live sessions are kept alive by a heartbeat every 30 seconds; a session a crash left open ends at its last heartbeat when the recorder restarts.

A failure to write a live, playback or download entry is logged and never interrupts the viewing. Export and snapshot entries are written in the same transaction as the security-log entry for the same action, so if one cannot be written the action fails rather than going unrecorded.

Each entry keeps the person's and the camera's name as they were, so the report still reads correctly after an account is removed or a camera is renamed. It never holds a camera address or credential.

## The report

- **By person** and **by camera** group the same entries, so their totals always agree for the same range and filters. Time is clipped to the range: a session that straddles midnight counts only the time inside.
- Each row can be opened to its entries, newest first.
- **CSV** downloads the current grouping, or every entry. Cells that begin with `=`, `+`, `-` or `@` are prefixed with `'` so a spreadsheet does not run a name as a formula. A range with more entries than one file holds (100,000) is refused rather than cut short: choose a shorter range. Downloading a CSV is itself written to the security log.

## API

All of these need an administrator, or an API token with the `admin` scope:

- `GET /api/admin/viewing-audit/report?by=person|camera&from=&to=&user_id=&camera_id=`
- `GET /api/admin/viewing-audit/entries?from=&to=&user_id=&camera_id=&kind=&limit=&offset=`
- `GET /api/admin/viewing-audit/export.csv?view=person|camera|entries&from=&to=`
- `GET`/`PUT /api/admin/viewing-audit/settings` — `{"retention_days": 7..3650}`

`from` and `to` need a time zone; they default to the last seven days.

## Retention

Viewing entries are kept for 365 days unless changed (7 to 3650). The recorder removes older entries every five minutes, and saving a shorter period removes them at once. This is separate from footage retention, and does not touch the security log of changes to settings and accounts.

## Not covered

- Playback is inferred from requests; the NVR cannot see how long a browser actually kept a recording on screen.
- Video-wall sessions and API-token viewing are recorded as the administrator they act for, with nothing marking them as a wall or a token.
- Playback records when recordings were requested, not which footage was on screen, and a session of a single recording has no length.
- A row shows the name of a person or camera from its newest entry; two removed accounts that shared a username are grouped together.
