# Signed footage

Every recording segment is hashed into a signed, per-camera chain. That makes it possible to show that footage has not been altered, removed or reordered since the NVR wrote it.

## The chain

When a segment is indexed, the background worker reads the file, normally within 5 seconds. It records its SHA-256, time range and size as the next link in that camera's chain. Each link includes the hash of the link before it, so changing, removing or reordering any link breaks every link after it. Hashing happens in the background worker, never on the recorder's write path.

Once a minute, the head of each camera's chain is signed with the installation's Ed25519 signing key. The key is generated on first use and stored in the NVR database, encrypted with the credential key. Configuration backups contain both, so a restored installation keeps its signing key and its old signatures stay valid. The public key is at `GET /api/footage/signing-key`.

Deletions the NVR makes are recorded as links too, so they are expected rather than reported as tampering. This covers retention, administrator footage deletion, and removal of a camera's footage.

Footage recorded before signing was turned on (before this version was installed) is reported as **not signed**, not as tampered.

## Verifying a camera's footage

In **Cameras**, open a camera's **Health & History** tab, choose a range under **Verify footage**, and choose **Verify footage**. The API equivalent is:

```http
GET /api/cameras/{camera_id}/footage/verify?start=2026-10-20T00:00:00Z&end=2026-10-21T00:00:00Z
```

Ranges are limited to 31 days. Each segment in the range gets one of these statuses:

| Status | Meaning |
| --- | --- |
| `ok` | The file matches its hash and is covered by a valid signature. |
| `ok_unsigned_yet` | The file matches its hash; the link is newer than the last signature (signed within a minute). |
| `not_signed` | Recorded before signing began. |
| `pending` | Not hashed yet. A segment whose file cannot be read stays pending and is retried every 10 minutes. |
| `unavailable` | Marked unavailable by the NVR, for example after a restore without its recordings. |
| `out_of_order` | Its time range in the index differs from the chain: the footage was moved in time. |
| `missing` | The chain has it but the file or its index entry is gone, without a recorded deletion. |
| `altered` | The file no longer matches its hash. |

The result is `valid` only when no segment is out of order, missing or altered, and the chain itself checks out. If the chain has been rewritten, or its signature does not match this installation's key, `chain_error` says so and nothing in it can be trusted.

## Evidence packages

Each export includes `chain.json` (listed in `integrity.json` like every other member). For each camera, it holds:

- the SHA-256 of every source segment used, taken at export time;
- the chain links from the first of those segments to the latest signature;
- the signature and the public key.

`metadata.json` marks each camera `signed` or `not_signed` under `signed_footage`.

The package verifies offline, without the NVR's database. The same check runs on `POST /api/exports/{export_id}/verify`, which reports `chain.valid` and, for each camera, whether it was signed by this installation (`signed_by_this_installation`).

What the chain proves, and what it does not:

- It proves the **source recordings** used for the export matched, when exported, what this NVR hashed when it wrote them. That holds only if the package's public key is the installation's public key; compare it with `GET /api/footage/signing-key`. A package can carry any key, so a matching key is what ties it to this NVR.
- The exported MP4s are rendered from those sources (concatenated, time-compressed, masked or overlaid), so they are not byte-identical to them. They are covered only by the unsigned `integrity.json` checksums.
- It does not prove the camera's clock was right.
