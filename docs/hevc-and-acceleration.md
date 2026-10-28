# H.265 and hardware acceleration

## Live view

Browsers that can play H.265 (`hvc1`) get a camera's video as it is, with no conversion. For every other browser, and
whenever a privacy mask is in force, the NVR converts the stream to H.264 here. It never falls back to the low-quality
snapshot stream just because a camera sends H.265; that only happens if both pass-through and conversion fail.

- One conversion is shared by every viewer of the same stream, and counts once toward `MAX_LIVE_TRANSCODES` (4). A
  fifth different stream is refused with "busy" until one ends. Privacy-masked streams are already re-encoded and are not counted.
- A camera's low-quality stream, whose codec is not recorded, is converted whenever the camera is H.265 and the browser is not given H.265 as it is.
- The tile reads "converted" when the video was converted on the server.

## Recorded video

Playback of an H.265 recording asks the server for an H.264 copy (`?compat=h264`) when the browser cannot play H.265.
The copy is cached next to the privacy-masked renders and removed with them.

## Hardware acceleration

The NVR looks for NVIDIA (NVENC/NVDEC), Intel Quick Sync and VAAPI once per process. A backend is chosen only if FFmpeg
has its encoder, the device exists, and a short test encode works. Used for live conversion, compatible playback copies,
export rendering and AI decoding.

| Setting | Meaning |
|---|---|
| `NVR_HWACCEL=auto` | Use the first backend that works (default) |
| `NVR_HWACCEL=none` | Software (libx264) only |
| `NVR_HWACCEL=nvenc|qsv|vaapi` | Try only that backend |
| `NVR_VAAPI_DEVICE` | Render device, default the first `/dev/dri/renderD*` |

If an accelerated job fails, it is redone in software; if that works, acceleration stays off until restart (a failure in both is the camera or file, not the device); Settings > General >
System status shows the reason. In a container, pass the device in (`devices: ["/dev/dri:/dev/dri"]` for Intel/AMD, or the
NVIDIA container runtime) and make sure the FFmpeg in the image has the encoder.

## Not covered by automated tests

Real GPUs are not available in CI: backend detection and argument building are tested with a fake runner, and the
fallback is tested with a device that does not exist. Please try your hardware and read the status panel.
