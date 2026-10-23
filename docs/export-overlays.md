# Export overlays

Individual camera videos and synchronized grid videos carry burned-in labels so a reviewer can identify the camera and the source time without the metadata. This page records the presentation rules and the rendered-output checks behind them (#78).

## Rules

- **Size comes from the frame the text is drawn on.** Text is about 2.5% of that frame's height: about 18 px in a 720p individual video, 27 px in a 1080p single-tile grid and 54 px in a 4K single-tile grid. Grid labels are sized from each tile, not the whole composite. Dense grids keep a 12 px minimum (a 4×4 1080p tile renders about 12 px text), so labels stay readable and inside their tile.
- **Camera name and source time** sit top-left in a translucent dark box with modest proportional padding. The time always includes seconds and uses the date format, 12/24-hour clock and time zone captured when the export was submitted.
- **Long camera names** are cut at 40 characters with an ellipsis (`…`). The full name stays in `metadata.json`.
- **"Recording gap skipped"** notices sit bottom-centre, in space separate from every camera label. In grids the notice spans the whole frame and includes the skipped source interval. In individual videos it is its own cue rather than an extra label line. The full gap boundaries and source/output mappings remain in the metadata.
- **"No video available"** tiles centre the camera name, the notice, the shared source time and, where one exists, the last frame's capture time. They stay distinct from ordinary corner labels, so a frozen or dark tile never reads as current footage.
- Placement uses explicit ASS alignment overrides in each cue. The burn-in style only sets size, padding and colours, because FFmpeg reads a style-level `Alignment` with legacy SSA numbering, in which `7` means top-right.

## Verification

`tests/test_export_overlays.py` renders real exports from generated footage and measures decoded frames:

- label line heights in 720p individual videos, and in 1080p and 4K single-tile grids;
- every label inside its own tile in a 4×4 grid, including a long name;
- notice and label bounding boxes that never overlap, at the opening skipped interval and at a middle gap, in both output types;
- centred missing-footage notices.

The same tests fail against the previous implementation.

Before/after frames from synthetic footage are in [`evidence/export-overlays`](evidence/export-overlays). The `before-*` frames come from the previous implementation and show 90 px labels, with the gap notice drawn over the camera label at the opening gap. The `after-*` frames show the current rules.

## Post-merge testing required

- [ ] Submit a new individual and grid export on the installed NVR and confirm the labels are compact and readable, and that gap notices do not overlap them, in the normal video player. Record the version, output dimensions, tile count, owner and result. Earlier evidence packages are not changed.
