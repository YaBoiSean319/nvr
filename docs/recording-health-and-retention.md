# Recording health and retention

Camera status distinguishes the stream connection from persisted footage. The camera inventory returns video freshness, last indexed footage write, and a coverage state. Coverage history records transitions as covered, expected idle, known failure, or uncertain. Motion-only idle is expected when the ONVIF event source and video stream remain healthy; a stale continuous write while video is fresh is reported as a recording failure. Each interval also records why footage is missing; see [coverage and self-test](coverage-and-self-test.md).

Retention defaults to 30 days. An administrator may override the target for a camera group or an individual camera. A camera override wins over its group; removing it restores inheritance. The authenticated camera retention endpoint reports the effective target, achieved time span, indexed bytes, free filesystem capacity, and a projection based on observed indexed bitrate. The projection is an estimate and does not promise a fixed retention duration.

Cleanup runs periodically and removes catalog entries only after their files have been removed or were already missing. It retains an active playback lease until the response completes, so cleanup cannot delete footage being served. Failed filesystem deletions leave the catalog record for retry.

The retention API is:

- `GET/PUT/DELETE /api/camera-groups/{id}/retention`
- `GET/PUT/DELETE /api/cameras/{id}/retention`
- `GET /api/cameras/{id}/coverage`

Retention targets are integers from 1 through 3650 days. Operators can read effective policy and achieved coverage; only administrators can change retention targets.

## Multiple recording volumes

The default Compose file provides a separate `archive` volume mounted at `/recordings/archive`. Set `NVR_STORAGE_VOLUMES` to a JSON object that maps volume names to directories below `NVR_RECORDINGS_PATH`, for example `{"default":"","archive":"archive"}`. Every non-default directory must be a mounted filesystem; the recorder reports it unavailable when the mount is missing. Add additional Compose bind or named volumes at matching paths to expose more local disks.

Administrators can inspect `/api/storage` and assign a group through `GET/PUT /api/camera-groups/{id}/storage-volume`. Storage status includes mount, write-probe latency, capacity, free space, and affected cameras. Low free space and unavailable or slow volumes report affected cameras and retention risk.

## Storage headroom

Storage space comes before the retention policy. A recorder fills its disks by design, so each storage volume keeps a **headroom** of free space. When free space falls below it, the **oldest footage on that volume is deleted, across all its cameras and even if it is still inside its retention policy**, until the headroom is free again. This keeps recording running and leaves room for exports to be written, instead of a full disk stopping recording and failing exports with "No space left on device".

- The headroom is set per volume under **Settings > Recording & storage > Storage headroom**, in GB (`PUT /api/storage/{volume_id}/headroom`, administrators only, audited as `storage_headroom_changed`; `null` restores the default). The default is 5% of the volume, and at least 5 GB.
- It is checked every minute. Footage that is held, being exported (queued or running), kept for an open or recently resolved alarm, or being played is never deleted; if those alone fill the volume the setting shows how far short of the headroom it is. Footage already moved to a cold tier is not on the volume, so it is not counted.
- Beside each setting the page shows the headroom and the free space now, whether footage is being deleted to keep it, how much was deleted in the last 24 hours, the age of the oldest footage kept against the longest retention policy on the volume, and any shortfall.
- Deletions are counted in the day's retention deletion record like any other, so the coverage and playback show the footage as removed.
- Footage removed this way can be younger than its retention policy: the retention forecast shows which cameras cannot be held for their policy at their write rate.
