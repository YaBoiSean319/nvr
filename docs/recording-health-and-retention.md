# Recording health and retention

Camera status distinguishes the stream connection from persisted footage. The camera inventory returns video freshness, last indexed footage write, and a coverage state. Coverage history records transitions as covered, expected idle, known failure, or uncertain. Motion-only idle is expected when the ONVIF event source and video stream remain healthy; a stale continuous write while video is fresh is reported as a recording failure.

Retention defaults to 30 days. An administrator may override the target for a camera group or an individual camera. A camera override wins over its group; removing it restores inheritance. The authenticated camera retention endpoint reports the effective target, achieved time span, indexed bytes, free filesystem capacity, and a projection based on observed indexed bitrate. The projection is an estimate and does not promise a fixed retention duration.

Cleanup runs periodically and removes catalog entries only after their files have been removed or were already missing. It retains an active playback lease until the response completes, so cleanup cannot delete footage being served. Failed filesystem deletions leave the catalog record for retry.

The retention API is:

- `GET/PUT/DELETE /api/camera-groups/{id}/retention`
- `GET/PUT/DELETE /api/cameras/{id}/retention`
- `GET /api/cameras/{id}/coverage`

Retention targets are integers from 1 through 3650 days. Operators can read effective policy and achieved coverage; only administrators can change retention targets.
