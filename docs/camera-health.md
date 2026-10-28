# Camera health dashboard

**Cameras > Health** is a fleet view of every camera you may see: make and model, firmware, recording uptime, bitrate and frame rate (as set in the camera and as the NVR
measures), dropped frames, reconnects and the last time the camera answered over ONVIF. It is sortable, filterable, and downloads as CSV.

It only reads values the NVR has stored. Opening it (even with 200 cameras) starts no video, no FFmpeg and no connection to any camera.

## Where each value comes from

| Value | Source | When there is none |
|---|---|---|
| Make, model, firmware | ONVIF device information, read every 10 minutes for cameras added with an ONVIF address | Not reported |
| Bitrate and frame rate "set" | the camera's recording profile (ONVIF), the one the NVR records | Not reported |
| Bitrate "seen" | the recorded files of the last 30 minutes: bytes over seconds | Not reported |
| Frame rate "seen" | the newest recording's frame rate (FFprobe, run by the background sampler, never by the page) | Not reported |
| Recording uptime | how long the recorder has been recording since its last break | Not reported while it is not recording |
| Dropped frames | FFmpeg's own `drop_frames` report for the recorder's processes, added up (also on `/metrics`) | Not reported until the recorder has counted the camera |
| Reconnects | counted whenever the recorder's state changes to reconnecting, whichever part of the recorder sets it | Not reported until the recorder has counted the camera |
| Last ONVIF contact | the last time the camera answered; a failed attempt shows "last try failed" with the reason, and keeps the previous values | Not reported |

Cameras with no ONVIF address have nothing from the camera itself and say so. Nothing is guessed. Stream copy rarely drops frames, so dropped frames are usually 0.

## Firmware

Within each make and model, among the cameras you may see, versions are compared in natural order (2.10 is newer than 2.9). Cameras below the newest are marked "Older than ..." and
counted in a banner; "Older firmware only" shows just them. A model with one version, or only one camera, is not flagged. A camera you cannot see plays no part in what you are shown.

## Not covered by automated tests

A real camera's report, FFmpeg's progress on a live stream and the supervised recorder process are not exercised: the ONVIF read is tested against the mock camera, the counters
against the database and FFmpeg's report format.
