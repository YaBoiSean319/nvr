# Evidence packages

Completed recording exports are delivered as ZIP evidence packages. `metadata.json` describes the requesting user ID, camera IDs and display names, requested interval, indexed actual and missing intervals, UTC-normalized timestamp provenance, and the audit reference. It does not include camera connection URLs or credentials. `integrity.json` contains a SHA-256 digest and byte count for each other package member; the manifest excludes itself because a file cannot contain its own stable checksum.

To verify a downloaded package, send its exact ZIP bytes to the authenticated endpoint:

```http
POST /api/exports/{export_id}/verify
Content-Type: application/zip

<ZIP bytes>
```

The verifier checks the SHA-256 and byte count of every listed member against the delivered bytes. It does not extract files or determine whether the MP4 files are playable. Playability remains a separate media validation step. Each verification records the actor and valid/invalid outcome in the audit log.

SHA-256 provides change detection only. Since the manifest is inside the same unsigned package, a party able to replace both media and manifest can create a package that verifies. Verification does not prove who created the evidence, when it was created, the accuracy of the camera clock, or chain of custody. Timestamp intervals are derived from the NVR recording segment index and serialized as ISO 8601 UTC values.
