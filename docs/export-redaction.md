# Export redaction: blur faces and licence plates

An export can blur faces and licence plates. The NVR looks for them in the range, shows what it found, lets the person reviewing it stop a blur (for the subject of a request) or add a region of their own, and only then renders the clips. The package says that it was redacted, by whom and with what settings. The recordings are never changed.

In **Playback > Export footage**, tick **Faces** and/or **Licence plates** under *Blur*. (The choices are offered only for the kinds the installed models can find.) The button then reads **Review blurs**; it looks through the range, and the review appears under *Blur*:

- every face and plate found, per camera, with the time it was on screen, a **Blur** tick (untick it to leave that subject visible) and **Show**, which puts a picture from the footage beside the list with what will be blurred outlined in red, what stays visible in green and manual regions in orange;
- **Add a region to blur**: a camera, a time range and either a track to follow or a place (left, top, width, height as a percentage of the picture; give a second position, "at the end", and the region moves from one to the other);
- **Confirm these blurs**. Only then does the button read **Export**.

Changing the cameras, the range or the kinds after starting a review makes it stale; start another.

## What is blurred

- Detection runs on the same picture the export renders (1280x720, privacy masks already drawn), five times a second. Detections are linked into tracks by overlap; each box is grown by 20% (at least 6 pixels) on every side and keeps covering its subject for 0.4 s before and after it was seen, because detections are sampled.
- Blurring is pixelation: a box is reduced to about five blocks across its longer side, so nothing readable survives and it cannot be reversed.
- A manual region that **follows a track** uses that track's boxes (scaled by a factor between a quarter and four) even when the track itself is kept, which is how to blur a badge on a subject who stays visible. A region **by position** moves in a straight line between its keyframes.
- Redaction applies to the **individual camera files**. A redacted export cannot include the synchronized grid (the request is refused), and audio, if asked for, is the camera's own.
- A review covers at most two hours of footage in total (for example four cameras for thirty minutes), and an administrator's or operator's review is limited to the cameras they may see, exactly as an export is.

## The package

- Each camera file is rendered once, then re-encoded with the blurs; the first render is deleted and is never in the package, nor anywhere else after the job ends. If blurring fails the job fails and no package is made.
- `metadata.json` has a `redaction` entry: `applied`, the review's id, the method, the kinds, who confirmed it (user id and name, with the last editor), the settings (model id, threshold, sampling rate, padding, hold), the ids of the tracks left unblurred, `unredacted_copy_included: false` and `original_footage_unchanged: true`. Each camera records how many tracks were blurred and kept, manual regions and frames blurred. The manifest is covered by `integrity.json` like everything else, so verification still passes and a changed clip fails it.
- A review makes one export. If that export fails or is cancelled, **Retry** uses the same confirmed review.
- Creating a review, editing it, confirming it and looking at footage in it are recorded: `redaction_review_requested`, `redaction_review_edited` (with the tracks kept) and `redaction_review_confirmed` in the audit log, and a `export` viewing event for each camera.

## Models

The administrator chooses the models, as for natural-language search. `NVR_AI_REDACTION_MODEL_DIR` (default `/opt/nvr/models/redaction`) holds `face.xml` / `face.bin` and `plate.xml` / `plate.bin`; either kind may be missing (it is then not offered). Both are SSD-style OpenVINO detectors whose output is rows of `[image, label, confidence, x1, y1, x2, y2]` and whose input is a BGR picture, as Open Model Zoo's `face-detection-0200` and `vehicle-license-plate-detection-barrier-0106` are. The plate model reports vehicles as label 1 and plates as label 2 (`NVR_AI_PLATE_LABEL` changes the plate label); a face model's every label is a face. Everything runs in this process; nothing is sent anywhere. Without a model, no review can be made.

Detection is only as good as the model: a face turned away, a plate too small at the 1280-pixel scale, or one the model misses is not found. That is what the review and manual regions are for; **check the picture before confirming**.
