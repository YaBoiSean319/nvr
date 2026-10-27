# Object tracks in Playback

Playback can draw the objects the AI worker found over the video, and follow one object through the camera's view.

## Using it

- **Boxes** (button, or **B**) shows a box on each person and vehicle in the stored detections for the time on the shared playback clock. It is off every time you open Playback.
- Click a box to **follow** that object: its path so far is drawn as a trail that fades with age (the last 12 seconds), and **◀ Object** / **Object ▶** (or **[** and **]**) jump the clock to the object's previous or next stored appearance. A jump crosses any gap where the object was hidden for a few seconds, because it is the same track. **Stop following** (or clicking the box again) ends it. A box with a dashed outline has no track (the worker did not follow it), so it cannot be followed.
- The message under the controls says when there is no earlier or later appearance, or when it is outside the searched range.

## What is drawn, and from what

Only detections the AI worker stored: nothing is inferred live, and nothing is drawn where no frame was analysed. The boxes shown at a moment are those of the latest analysed frame at or before the clock, kept on screen for a little over two analysis intervals (worked out from the frames, between 1 and 10 seconds) and then removed, so a box never lingers after its frame is stale. At the usual one or two analysed frames a second the box and the video are within a frame or two; at the default 0.5 frames a second the box holds for the two seconds until the next analysed frame, then jumps. It does not move between analysed frames.

Boxes are placed on the picture, so they stay on their objects when a tile is letterboxed or resized.

## Privacy masks and access

A box that touches a privacy-masked area is never sent to the browser, so the overlay cannot show that something is behind a mask. The same holds for the points of a followed object's path. An administrator holding an unmasked grant for the camera sees the boxes too. Cameras a person may not see are not found, exactly as for missing ones.

## API

- `GET /api/playback/overlay?camera_id=&start=&end=` (at most 10 minutes): the stored detections of one camera, one entry for each analysed frame (`frames[].at` and `frames[].boxes[]` with `track_id`, `label`, `confidence`, `box`), with `withheld` (boxes left out because of masks), `unanalyzed` (intervals not analysed, with why) and `truncated`.
- `GET /api/playback/overlay/tracks/{track_id}`: the whole stored path of one object, `points[]` (`at`, `box`).

Both accept `unmasked=true` with an administrator's grant, as video does.

## Exports

Overlays are drawn in the browser only. Exports are cut from the recordings and never contain boxes or trails, and there is no option to burn them in.

## Limits

- The time of a detection is when the worker analysed the frame, which can lag the recorded picture by the analysis delay; boxes can therefore be a little behind fast movement.
- Only analysed time has boxes: an unanalysed stretch shows none, which does not mean nothing was there (the timeline and the answers say which time was not analysed).
- A box covers up to 5000 stored detections for each minute of a camera; a busier minute is cut and flagged `truncated`.
