# Evidence packages

Completed recording exports are delivered as ZIP evidence packages. `metadata.json` describes the requesting user ID, camera IDs and display names, requested interval, indexed actual and missing intervals, UTC-normalized timestamp provenance, and the audit reference. It does not include camera connection URLs or credentials. `integrity.json` contains a SHA-256 digest and byte count for each other package member; the manifest excludes itself because a file cannot contain its own stable checksum.

To verify a downloaded package, send its exact ZIP bytes to the authenticated endpoint:

```http
POST /api/exports/{export_id}/verify
Content-Type: application/zip

<ZIP bytes>
```

The verifier checks the SHA-256 and byte count of every listed member against the delivered bytes. It does not extract files or determine whether the MP4 files are playable. Playability remains a separate media validation step. Each verification records the actor and valid/invalid outcome in the audit log.

Exports default to both individual camera videos and the synchronized grid. The ZIP manifest records each source interval and its output-clock mapping, selected outputs, display settings, audio policy, and package expiry. Optional audio is off by default and is available only when an administrator has enabled a camera's supported recorded audio. Individual videos use each camera's own audio. A grid uses one selected camera's audio, aligned to the grid output clock; missing source spans and globally compressed time remain silent.

Individual outputs concatenate that camera's covered spans and compress each of its own gaps, so individual MP4s can have different durations. A grid output follows the union of selected-camera coverage and compresses only intervals where every selected camera lacks footage; retained camera tiles use a shared source clock and show gaps within each tile. These time-compression rules are distinct and are recorded in the package mapping and metadata.

Export jobs persist queue status, progress, cancellation state, and errors. A running job interrupted by a process restart is marked failed with an explicit restart reason; queued work resumes when the worker starts. A failed or cancelled job can be retried with `POST /api/exports/{export_id}/retry`. A retry creates a new job from the original request, which itself stays unchanged. The retry revalidates the cameras and footage as they are now and is audited as `export_retried` with a reference to the original job (`retry_of`). Completed ZIPs remain available for seven days. Expiry removes the ZIP while preserving job status and manifest metadata. Output is bounded to 2 GiB and the worker preserves at least 256 MiB of free storage before rendering or packaging. Source recordings remain reserved only while the job is using them.

SHA-256 provides change detection only. Since the manifest is inside the same unsigned package, a party able to replace both media and manifest can create a package that verifies. Verification does not prove who created the evidence, when it was created, the accuracy of the camera clock, or chain of custody. Timestamp intervals are derived from the NVR recording segment index and serialized as ISO 8601 UTC values.
