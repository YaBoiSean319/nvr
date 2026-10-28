# Storage tiering to a cold tier or S3-compatible storage

Footage older than an age you choose moves from the recorder's disks to a **cold tier**: a folder (another disk, a network share mounted into the container) or an S3-compatible bucket (AWS S3, MinIO, Ceph, Backblaze B2, Wasabi...). It stays in the timeline and plays back through this NVR; holds, exports, signed-footage verification and retention work across tiers.

## Setting it up

Settings, **Storage tiering**:

1. **Add a cold tier.** A *folder* is an absolute path outside the recordings folder, writable by the NVR. A *bucket* is the store's address, the bucket, a region (default `us-east-1`), an optional prefix inside the bucket, and an access key and secret key. The NVR writes a small object, reads it and removes it before it keeps the tier, so a tier that does not work is refused with the reason. Keys are stored encrypted like camera credentials and are never shown again; a public store must use HTTPS, and one on a private address needs the private-network box ticked.
2. **Set a policy for a storage volume** (the default volume, or any volume in `NVR_STORAGE_VOLUMES`): the tier, the age in days after which footage moves, and a rate limit (megabytes a second). One policy per volume; it applies to every camera whose group uses that volume.

The age is separate from retention: retention still deletes footage when its policy says (from whichever tier it is on, with its copy). Set the move age below the retention days or nothing will ever reach the tier.

## How a move goes

1. Pick the oldest eligible segments (older than the age, not being recorded, not already moved), a few at a time, one at a time, rate limited.
2. **Skip** a segment that is **held** or is in a **queued or running export**; both stay hot until released. A segment whose footage no longer matches its signed chain is **never moved** (it is shown in the tier's last error and left alone).
3. Hash the hot file, **copy** it to the tier (to a partial file or a signed upload), **read the copy back** and check its SHA-256 and size against the original. A bucket also refuses an upload whose body is not the file the request was signed for.
4. Check again that the segment is not held or exported, then switch its row to the tier. It keeps its id and path, which is what the signed chain, holds, exports and cases refer to.
5. Delete the hot file, unless someone is playing it; a later pass removes it once it is released.

Every step can be repeated: the copy overwrites, the switch only happens once, and a pass first finishes anything interrupted. Whatever the interruption (the copy dies half way, the process stops after the switch before the hot file goes), the segment stays readable once, and no duplicate or partial copy is left behind. A tier that fails a few times in a row raises a storage incident, which recovers with its first success.

## Playing and verifying

- A segment on a **folder** tier is read in place. A segment in a **bucket** is fetched into a read-through cache (`NVR_TIER_CACHE_PATH`, default beside the database; `NVR_TIER_CACHE_BYTES`, default 2 GiB, least recently used out, never footage being played) and checked against the digest it moved with before it is used; if it came back different it is refused. The first seconds of footage from a bucket are slower.
- Playback, exports, snapshots, case clips and the footage chain's verification all read through the same path, so they work unchanged. Verification of a long range on a bucket downloads every segment in it.
- The timeline marks footage on a tier in its tooltip.

## Deleting

Retention, deleting a segment and deleting a camera's footage remove the cold copy and the cached copy too. A bucket that cannot be reached for the delete does not stop retention: the object is remembered and removed on a later pass.

## Limits

- A tier's location cannot be changed, or the tier removed, while footage is on it.
- Moving needs the recorder's hot copy to be readable; footage already unavailable is not moved.
- Cold storage is not a backup: it holds the footage that left the disks, one copy, with whatever durability the store gives.
- Tested here with a local folder and with a stand-in S3 server that checks AWS Signature Version 4 and upload hashes, and with the signing code checked against examples AWS publishes; the CI workflow also runs the object-store tests against MinIO when it can start one. Try your own store (see the acceptance checklist) before relying on it.

## API

Administrators: `GET /api/admin/tiering`; `POST /api/admin/tiering/tiers`, `PUT`/`DELETE …/tiers/<id>`, `POST …/tiers/<id>/check`; `PUT`/`DELETE /api/admin/tiering/policies/<volume>`; `POST /api/admin/tiering/run` (moves up to five of each policy's oldest segments now). Changes are in the audit log (`cold_tier_created`, `cold_tier_updated`, `cold_tier_deleted`, `tier_policy_changed`, `tier_policy_removed`). Timeline segments carry `archived`.
