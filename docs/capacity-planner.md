# Capacity planner

Settings, **Capacity planner** (administrators) estimates what a set of cameras needs: disk space, write speed, network load and processing (cores, with and without an accelerator).

**It is an estimate, not a certification of capacity.** The page and every export say so. Real footage varies with the scene, the camera's settings and the hardware: measure with a few cameras before buying for many, and keep headroom.

## Using it

1. Add lines of cameras alike in every way (a count, picture size, codec, frame rate, bitrate, recording mode, days kept, AI on or off), or choose **Start from current cameras** to load this NVR's own cameras.
2. Say how many people watch Live at once and how many cameras each watches.
3. The estimate follows as you type. **Download CSV** or **Download JSON** keeps it, with the plan, the assumptions and the disclaimer.

Starting from the cameras here uses what the NVR knows: each camera's probed picture size and codec, its recording mode and retention, whether AI is on, and its **measured bitrate while recording** and (for motion recording) **measured share of time recorded**, both over the last 48 hours and only when there is at least an hour of footage. Anything it cannot know (the frame rate is never known; a camera without enough footage has no measured bitrate) is left empty and the estimate works it from a default and marks it **assumed**, in the table and in the list of assumptions.

## How the figures are worked out

All of it is plain arithmetic, so it can be checked by hand. With the defaults:

- **Bitrate** of a camera: the one entered or measured, else a default for its picture size at 15 frames a second (up to 0.35 MP 512 kbps, 1 MP 1024, 2.2 MP 2048, 4.2 MP 3072, 5.5 MP 4096, 8.5 MP 6144, larger 8192), scaled by frame rate / 15, and by 0.6 for H.265.
- **Share of the day recorded**: 1 for continuous, the measured share (or 25 %) for motion, 0 for not recorded.
- **Footage kept** = bitrate / 8 x 86 400 s x share recorded x days kept x cameras. **Disk needed** = footage x 1.05 (container and filesystem). **Disk recommended** = needed x 1.2 (headroom).
- **Write speed**: average = sum of bitrate x share recorded; peak = sum of bitrate of recording cameras.
- **Network in** = sum of every camera's bitrate (cameras stay connected whether or not they record). **Network out** = viewers x cameras each x average bitrate x 0.25 (Live grids use the low-quality stream).
- **Processing**: 0.01 core per Mbps in and 0.005 per Mbps out (stream copy); H.265 shown to a browser that cannot play it is converted (half of the viewed H.265 streams, 0.5 core per 1080p 15 fps stream, scaled by size); AI costs, per analysed frame (0.5 a second by default), 6 ms per megapixel to decode a 0.3 MP frame plus 40 ms of inference on the processor or 8 ms on an accelerator. The accelerator line is the processor's share only: the accelerator needs its own capacity.

Example: 10 cameras at 4096 kbps recording all day for 30 days need 4096 / 8 x 86 400 = 44 236 800 000 bytes a day each, 13.27 TB for the lot, 13.93 TB with overhead and 16.72 TB with headroom; 40.96 Mbps arrive and the disks are written at 5.12 MB/s.

Every default can be changed under **Change the assumptions** (within sensible limits), and the figures then say the value was changed. Each result names the assumptions it rests on.

## Limits

- The defaults are round numbers for typical H.264 and H.265 cameras, not measurements of any particular make. A measured bitrate beats them.
- Frame rate is not known to the NVR, so it is assumed unless entered.
- The processing figures are rough: they ignore the web server, the database and other work, and assume the work spreads over the cores evenly.
- The plan is not saved: download it to keep it.
