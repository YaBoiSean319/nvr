# Evidence packages

Completed recording exports are delivered as ZIP evidence packages. `metadata.json` describes the requesting user ID, camera IDs and display names, requested interval, indexed actual and missing intervals, UTC-normalized timestamp provenance, and the audit reference. It does not include camera connection URLs or credentials. `integrity.json` contains a SHA-256 digest and byte count for each other package member; the manifest excludes itself because a file cannot contain its own stable checksum.

To verify a downloaded package, send its exact ZIP bytes to the authenticated endpoint:

```http
POST /api/exports/{export_id}/verify
Content-Type: application/zip

<ZIP bytes>
```

The verifier checks the SHA-256 and byte count of every listed member against the delivered bytes. It does not extract files or determine whether the MP4 files are playable. Playability remains a separate media validation step. Each verification records the actor and valid/invalid outcome in the audit log.

An uploaded package is written to a temporary file beside the exports as it arrives, checked from there a piece at a time, and removed afterwards whether the check passes, fails or is refused. Checking a package therefore takes about the same memory whatever its size, and does not hold up the rest of the server while it runs. A case package's clips are checked the same way.

Exports default to both individual camera videos and the synchronized grid. The ZIP manifest records each source interval and its output-clock mapping, selected outputs, display settings, audio policy, and package expiry. Optional audio is off by default and is available only when an administrator has enabled a camera's supported recorded audio. Individual videos use each camera's own audio. A grid uses one selected camera's audio, aligned to the grid output clock; missing source spans and globally compressed time remain silent.

Individual outputs concatenate that camera's covered spans and compress each of its own gaps, so individual MP4s can have different durations. A grid output follows the union of selected-camera coverage and compresses only intervals where every selected camera lacks footage; retained camera tiles use a shared source clock and show gaps within each tile. These time-compression rules are distinct and are recorded in the package mapping and metadata.

Export jobs persist queue status, progress, cancellation state, and errors. A running job interrupted by a process restart is marked failed with an explicit restart reason; queued work resumes when the worker starts. A failed or cancelled job can be retried with `POST /api/exports/{export_id}/retry`. A retry creates a new job from the original request, which itself stays unchanged. The retry revalidates the cameras and footage as they are now and is audited as `export_retried` with a reference to the original job (`retry_of`). Completed ZIPs remain available for seven days by default; an administrator can choose 1 to 30 days in **Settings → Recording & storage → Retention → Exports and copies** (`GET`/`PUT /api/export-storage`, audited as `export_retention_changed`). A new setting applies to exports finished from then on. Expiry removes the ZIP while preserving job status and manifest metadata. Output is bounded to 2 GiB, and the worker leaves the recordings volume's headroom free (at least 256 MiB) before rendering or packaging, so an export never makes the recorder delete footage.

**One disk budget.** Exports, case packages and playback copies (masked, or converted to H.264 for a browser) together may use **10% of the recordings volume**. At the limit, playback copies are removed first, oldest first, since they can be made again; if that is not enough, a new export is refused with `507` and a message saying why, and a case package fails with the same reason. Nobody's export is removed to make room. Settings shows how much each of the three uses. Source recordings remain reserved only while the job is using them.

**Is it the package this NVR issued?** When an export completes, the NVR keeps the SHA-256 of the ZIP it hands out. Verification compares the uploaded bytes with it and answers in `matches_issued_package`: `true` (byte for byte the file that was issued), `false` (it is not, whatever its own list of checksums says; the result is recorded as a failed verification) or `null` with `issued_hash_recorded: false` for an export made before this was kept, which is checked against its own list only, as before. The audit entry records `issued_package` as `matches`, `differs` or `not_recorded`. This closes the case described next for anyone who verifies against the NVR that made the export; it needs that NVR and its database, and the export's record, which is removed when the export expires.

On its own, away from the NVR, SHA-256 provides change detection only. Since the manifest is inside the same unsigned package, a party able to replace both media and manifest can create a package that verifies. Verification does not prove who created the evidence, when it was created, the accuracy of the camera clock, or chain of custody. Timestamp intervals are derived from the NVR recording segment index and serialized as ISO 8601 UTC values.

Each camera's entry in `chain.json` is also checked to be that camera's own chain: a valid chain of one camera placed under another camera's name is reported as invalid.

Packages also carry `chain.json`, the signed hash chain covering their source recordings, and the public key. Verification checks it too: see [signed footage](signed-footage.md) for what it proves.
