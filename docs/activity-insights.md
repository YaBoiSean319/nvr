# Activity insights

**Insights** (in the top navigation) shows how many people and vehicles the cameras saw, where they went, and lets you download the numbers.
It reads the tracks the AI stored; nothing here is a new measurement.

## What is counted

- **Camera:** each track counts once, in the hour or day it began.
- **Zone:** for each zone drawn in an enabled *zone entry* rule on a camera, a track counts once, when it first enters.
- **Line:** for each line in an enabled *line crossing* rule, every crossing counts (in the rule's direction, or both), so someone who
  walks across and back counts twice. The rule's schedule, cooldown and alarm settings do not matter here, only its shape and the kinds
  of object it watches (a line that watches people only counts people).
- Hours and days follow the site clock; a day with a clock change is 23 or 25 hours long. Hourly counts cover up to 31 days, daily up to 92.

## Time that was not analysed

Every hour or day shows how much of it the AI was analysing the camera. Where it was not all of it (the AI was off, delayed, failed or
interrupted) the chart shades it and the CSV says "Not fully analysed: counts may be lower than what happened". An hour that has not
happened yet is not called unanalysed. A quiet bar in a shaded hour means nothing was looked at, not that nothing happened.

## Heatmap

For one camera and the chosen period: how many times an object's centre was sampled in each cell of a 32 × 18 grid of the frame, drawn
over a still from the camera's recordings (with any privacy masks applied; with no recording it is drawn on a plain background).
Brighter cells are where objects spent more time.

## CSV

`/api/insights/export.csv` (the Download CSV button) has one row per hour or day for each camera, zone and line:
`bucket_start_local, bucket_start_utc, kind, name, camera, person, vehicle, analysed_percent, note`. Names that a spreadsheet could run
as a formula are prefixed with an apostrophe.

## Access

Insights are for anyone signed in to the main interface (administrators and operators), and only for the cameras they may see: a camera
they cannot see is left out, and asking for it answers like a camera that does not exist. Tokens need the read scope (the reference still
also needs media).
