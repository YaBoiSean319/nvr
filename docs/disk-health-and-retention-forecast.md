# Disk health and retention forecast

Both are in **Settings › Recording & storage**; the forecast for one camera is also on its **Recording** tab.

## Disk health (SMART)

The NVR asks the disk behind each storage volume for its SMART report (`smartctl`, from the smartmontools package in the image) when it starts and every six hours (`NVR_SMART_INTERVAL_SECONDS`), and when an administrator chooses **Check disks now**. A reading is one of:

| State | Meaning |
|---|---|
| **Healthy** | no problem reported |
| **Warning** | signs that usually come before failure: an attribute that failed in the past; reallocated, pending or offline-uncorrectable sectors, spin retries or uncorrectable errors above zero; an NVMe drive 90% or more worn, low on spare capacity or reporting media errors |
| **Failing** | the drive's own health assessment is FAILED; an attribute failing now; an NVMe critical warning or its rated life used up |
| **Not available** | no reading could be had, and why |

A warning or failing disk raises an incident for the volume (`disk_health`, sent as `storage.failure` and `storage.recovered` webhooks), which recovers when a later reading is clean. **A disk that cannot be read is reported as not available, never as healthy**, and raises nothing: the reason is shown, for example "smartctl is not installed in this container", "the disk is not passed through to the container" or "the container may not read this disk's SMART data".

### Giving the container the disks

A container sees only what it is given. For each disk to be read:

```yaml
services:
  nvr:
    devices:
      - /dev/sda:/dev/sda      # the whole disk, not a partition
    cap_add:
      - SYS_RAWIO              # smartctl's ATA commands need it; some NVMe drives need SYS_ADMIN instead
    environment:
      NVR_SMART_DEVICES: '{"default":"/dev/sda","archive":"/dev/sdb"}'
```

`NVR_SMART_DEVICES` names the disk for a volume by its volume name. Without it the NVR tries to work the disk out from the mount through `/sys`, which succeeds only where the volume is on a real disk the container can see; on Docker's overlay filesystem or a network mount it reports "cannot see which disk holds this volume". Virtual disks, many USB bridges and hardware RAID volumes do not report SMART and show as not available; the SMART data of disks behind a RAID controller needs `smartctl -d` options this does not set. Nothing identifying a disk beyond its model is kept.

## Retention forecast

Footage is removed only when it is older than its camera's retention policy; nothing removes it to make room. When the cameras on a disk write faster than the disk can hold their policies' worth, the disk fills and what the policy promised is not kept. The forecast works out what the disk can really hold:

- **Write rate** per camera: the footage indexed in the last 48 hours over the time it has been recording in that window (a camera needs 6 hours of footage before it is forecast; a motion camera's rate is what it writes, not what it would write recording all day).
- **Budget** per disk: free space, less the reserve at which storage pressure is reported (`NVR_STORAGE_PRESSURE_FREE_BYTES`, 1 GiB), plus the footage already stored on it. Volumes on the same disk share one budget.
- **What a camera keeps**: its policy if all policies fit. If not, the cameras on the disk share the shortfall by each keeping the same number of days, no more than its own policy. For example, with a 300 GB budget, a camera writing 10 GB a day with a 30-day policy and another writing 5 GB a day with a 14-day policy need 370 GB; the second keeps its 14 days and the first keeps (300 − 70) / 10 = 23.
- **When it begins**: footage grows towards each policy at the camera's write rate; the forecast gives the days until what is stored first exceeds the budget.

A camera that will not keep its policy shows "Camera X keeps about N days, below its M-day policy", how far away the shortfall is, and what would fix it: lowering its bitrate by about a percentage (to a number of GB a day), moving it to another volume on a different disk that has the room, or shortening the retention of the cameras whose policy exceeds what the disk can hold.

An **incident** (`retention_forecast`, also sent as `storage.failure` and `storage.recovered`) is raised for a camera that will fall below its policy within **7 days**, and recovers when it no longer will: after lowering a bitrate, shortening a policy, freeing space or adding a disk. A shortfall further away is shown but not raised.

### API

- `GET /api/storage/disk-health`, `POST /api/storage/disk-health/refresh`, `GET /api/storage/forecast` (administrators)
- `GET /api/cameras/{id}/retention-forecast` (anyone who can see the camera)

Metrics: `nvr_disk_health_state{volume,state}`, `nvr_camera_retention_policy_days` and `nvr_camera_retention_forecast_days`.

## Limits

- The forecast assumes the last 48 hours are typical; a change in scene, schedule or bitrate changes it, and it is recalculated every five minutes.
- Other things stored on the same disk count against free space, which is why free space is measured rather than assumed.
- It does not model footage kept longer by holds, alarms or exports, which makes it a little optimistic.
- A volume that is not mounted is not forecast.
