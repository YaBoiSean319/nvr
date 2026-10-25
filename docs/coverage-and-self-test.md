# Gap reasons, the coverage report and the self-test

## Why a gap happened

Every stretch of a camera's timeline without footage records why. The recorder classifies what it sees every few seconds:

| Reason code | Meaning |
| --- | --- |
| `camera_offline` | The stream failed and nothing answers at the camera's stream address. |
| `stream_error` | The camera answers, but its stream could not be recorded (wrong path or credentials, unreadable video). |
| `disk_unavailable` | The camera's recording volume is missing, read-only or full, or indexed footage was lost from it (for example after a restore). |
| `outside_schedule` | The camera's recording schedule has it off. |
| `recording_disabled` | The camera or its recording is turned off. |
| `worker_restart` | The recorder was not running, for example during an upgrade or a restart. |
| `no_motion` | A motion camera with nothing to record. |
| `unknown` | Nothing recorded why, for example before this version or while a camera first connects. |

To tell an offline camera from a broken stream, the recorder tries a TCP connection to the camera's stream port (3 seconds) each time the stream fails.

When the recorder starts, any coverage it last confirmed more than 15 seconds earlier is closed at that point, and the time since is recorded as a `worker_restart` gap. Where no recording was expected (idle, disabled or off schedule), the interval simply carries on across the restart. A restart quicker than 15 seconds is not recorded as a gap. The upgrade that introduced reasons treats intervals then open as confirmed at the upgrade, so it does not invent an outage.

Playback's timeline shows the reason when you hover over a gap, and a tile at the playhead shows it in its "No recording" label. The APIs add `reason_code` next to the existing `reason` description, without changing any existing field:

- `POST /api/playback/timeline` adds it to `gaps` and `coverage`;
- `GET /api/cameras/{id}/coverage` adds it to `intervals`.

Intervals recorded before this version get a code from their description. A stream fault stays while the recorder retries and is dropped once video is fresh again, so a later gap is never blamed on an old fault.

## Coverage report

**Settings › Coverage & self-test** shows each camera's coverage against its policy for the last 7 days, or for any stored week. Each camera shows:

- its policy;
- coverage, as the share of time its policy expects recording that has footage. Gaps for `outside_schedule`, `recording_disabled` and `no_motion` are left out;
- missed time by reason;
- its five largest gaps with their reasons.

`GET /api/coverage/report?start=…&end=…` (administrators, up to 31 days) returns the same report. Only time that has already happened counts, and only since each camera was added. The playback timeline answers the same question for a window of up to 24 hours (and up to 500 segments per camera), so compare them over a window within those limits. It computes gaps exactly as the Playback timeline does, so the two agree for the same range.

Every Monday the background worker stores the report for the week before (Monday to Monday, site time). `GET /api/coverage/reports` lists stored reports. Each one is also sent once to webhooks subscribed to `coverage.weekly_report`, with the report as `report` in the payload. Route that webhook to an email service to have it emailed.

## Nightly self-test

At 03:41 site time, after the nightly backup, the self-test checks:

- that every camera expected to record at some point in the last day (recording enabled, and not only expected idle) wrote footage in that day;
- that every recording volume is writable;
- that the latest configuration backup is fresh (under 26 hours old). When backups are not configured, this check is reported and skipped.

Each failed check raises its own incident (`self_test`), which is sent to `system.failure` webhooks. The incident recovers when a later run passes. Choose **Run self-test now** to confirm a fix. The latest result appears in **Settings › General** under System status, and in `GET /api/system/status` as `self_test`.

- `GET /api/self-test`: the latest run and the schedule.
- `POST /api/self-test/run`: run it now (administrators).
