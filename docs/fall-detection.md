# Fall detection

**This is an aid, not a safety system.** It will miss some falls and it will raise alerts that are not falls. It does not replace call buttons, regular checks or supervision, and it has not been measured on real cameras (see "What has and has not been checked").

On the cameras you choose, the NVR looks for a person who has fallen and stayed down, and raises an alarm: with the clip and snapshot, in the alarm inbox, by push and by any rule action (webhook, Slack, Teams, relay), like every other alarm.

## Turning it on

1. **Settings > Fall detection**: tick **Look for falls on** each camera that should be watched. It is off for every camera until you do. Set the **sensitivity** (high by default: catches more falls, raises more alerts that are not falls) and **how many seconds a person must stay down** (8 by default: see "What has been measured").
2. **Rules** (Cameras > Rules, or the **Create an alert rule for these cameras** button in Settings > Fall detection, which also warns while watched cameras have no rule): make a rule with the condition **A person fell**, choose its cameras or groups, an optional schedule and cooldown, and the actions that tell staff. Turning detection on only records falls; the rule is what alerts anyone. A fall raises one alarm, labelled **Person fell**.
3. Changes reach the AI worker within about half a minute, with no restart.

## How it decides

The AI worker already finds and follows each person. For a camera with fall detection on, on every frame the AI worker analyses (the camera's **AI analysis rate**: 0.5 frames a second by default, and **at least 1 to 2 is recommended** for falls; Settings warns when it is too slow) it asks a pose model for the position of each person's shoulders, hips, knees and ankles, and watches how the body changes:

- A **fall** is a person who was **upright**, went **low** (the torso well away from vertical, or the body wider than tall) within a few seconds, and **stays low** for the stay-down time.
- Someone **first seen lying down** (a resident in bed, on a sofa) is **not** a fall: nothing is raised for someone never seen upright.
- Someone who **sits down** is not a fall, and neither is someone who **bends or crouches and gets up again** within the stay-down time (8 s by default).
- **Known gap:** someone who **walks to a bed or sofa and lies down on purpose** is likely to be raised as a fall, even when they lie down slowly, because the decision looks at how far the torso leans, and only a few seconds of that change distinguish a fall from lying down. Do not put fall detection on a camera that watches a bed or sofa people lie on by choice, or expect those alerts.
- Someone who **gets up** within the stay-down time (a stumble that recovers) is not a fall.
- A person who goes **out of sight while low** (behind a bed or furniture) still counts, for a few seconds, because that is often what a fall looks like.
- One fall raises one alarm; the person has to be upright again for a moment before another can.

| Sensitivity | Low from a torso angle of | Upright below | A drop may take | Unseen time that still counts |
| --- | --- | --- | --- | --- |
| High (default) | 50° from upright | 40° | 4 s | 6 s |
| Normal | 60° | 35° | 3 s | 4 s |
| Low | 70° | 30° | 2 s | 3 s |

A body wider than tall counts as low 10° sooner than the angle above. Frames in between "upright" and "low", or where the body points cannot be seen, neither start nor end a fall; only an upright frame clears one.

## Body outlines in Playback

The body points the pose model finds for each person (nose, shoulders, elbows, wrists, hips, knees and ankles; only those it could see, to three decimal places of the picture) are kept with that person's detection, so **Playback > Skeleton** can draw them. They are kept and deleted exactly like the detection they belong to (retention, footage deletion), are never read for anyone inside a privacy mask, and are left out of Playback when any of them lies in a masked area. The fall decision still uses only its nine points (the arms are for drawing). This costs no extra analysis: the points were already being read.

## What it needs from the cameras

- A view where people are **large enough to see** and not mostly hidden. The AI worker analyses a small copy of the picture (448 × 256), so a person far from the camera is only a few dozen pixels tall and the body points are rough. Wide-angle views of corridors, lounges and dining rooms where people walk in the open suit it best; a camera on a bed behind rails, a bathroom, or a ceiling fisheye (which sees people from above) does not.
- Fall detection watches **people the NVR is already tracking**, so a camera needs AI analysis turned on as well, and the person has to be found as a person. It never looks at anyone it has not found.
- **Privacy masks:** a person inside a masked zone is never looked at by the pose model. Access grants, the viewing audit and retention apply to its alarms and clips as to every other. Whether a camera may cover a resident's room is the home's decision (consent and policy), not something the software settles.

## The model

MediaPipe **Pose Landmarker (lite)**, licensed Apache-2.0 by the MediaPipe project (the licence of the trained weights was taken from the project and not independently verified here). It is downloaded when the image is built and checked against a pinned SHA-256 (`app/pose_models.py` lists it and a test keeps the `Dockerfile` the same); only its landmark model is used, run with OpenVINO on the CPU, about 4 ms per person. Nothing that needs a licence is used: Ultralytics YOLO pose (AGPL-3.0 or a paid licence) is deliberately not part of this. The decision about falls works on a handful of body points only, so a different permissively licensed model could replace it later.

## What has been measured

Two things measure this, and neither is a substitute for trying it on your own cameras.

**1. The decision on made-up body angles** (`python -m tests.fixtures.fall_accuracy`, deterministic). It feeds the real fall decision with a person's torso angle over a minute (falling, stumbling, sitting, bending, crouching, lying down slowly, already lying, walking) with random error added and frames in which the person cannot be made out. It measures the **decision logic only**, not the pose model on real people or any camera:

```
Fall decision on made-up body angles (200 trials per cell, one analysed frame a second)

Sensitivity high, stay down 3 s
| Scenario | 0° error | 10° error | 10° error, 20% unseen |
| --- | --- | --- | --- |
| Falls and stays down (a fall) | 100% | 100% | 98% |
| Falls in one frame and stays down (a fall) | 100% | 100% | 99% |
| Stumbles and gets up within 2 s | 0% | 0% | 10% |
| Sits down on a chair | 0% | 0% | 0% |
| Bends to pick something up | 100% | 80% | 69% |
| Crouches to tie a shoe | 0% | 7% | 11% |
| Lies down slowly on a bed (12 s) | 100% | 99% | 93% |
| Was already lying (in bed) when first seen | 0% | 0% | 0% |
| Walks about | 0% | 0% | 0% |

Sensitivity high, stay down 8 s
| Scenario | 0° error | 10° error | 10° error, 20% unseen |
| --- | --- | --- | --- |
| Falls and stays down (a fall) | 100% | 100% | 98% |
| Falls in one frame and stays down (a fall) | 100% | 100% | 100% |
| Stumbles and gets up within 2 s | 0% | 0% | 0% |
| Sits down on a chair | 0% | 0% | 0% |
| Bends to pick something up | 0% | 0% | 0% |
| Crouches to tie a shoe | 0% | 0% | 0% |
| Lies down slowly on a bed (12 s) | 100% | 98% | 90% |
| Was already lying (in bed) when first seen | 0% | 0% | 0% |
| Walks about | 0% | 0% | 0% |

Sensitivity normal, stay down 3 s
| Scenario | 0° error | 10° error | 10° error, 20% unseen |
| --- | --- | --- | --- |
| Falls and stays down (a fall) | 100% | 100% | 90% |
| Falls in one frame and stays down (a fall) | 100% | 100% | 96% |
| Stumbles and gets up within 2 s | 0% | 0% | 11% |
| Sits down on a chair | 0% | 0% | 0% |
| Bends to pick something up | 100% | 64% | 62% |
| Crouches to tie a shoe | 0% | 8% | 12% |
| Lies down slowly on a bed (12 s) | 0% | 75% | 60% |
| Was already lying (in bed) when first seen | 0% | 0% | 0% |
| Walks about | 0% | 0% | 0% |

Sensitivity low, stay down 3 s
| Scenario | 0° error | 10° error | 10° error, 20% unseen |
| --- | --- | --- | --- |
| Falls and stays down (a fall) | 100% | 99% | 64% |
| Falls in one frame and stays down (a fall) | 100% | 100% | 88% |
| Stumbles and gets up within 2 s | 0% | 0% | 6% |
| Sits down on a chair | 0% | 0% | 0% |
| Bends to pick something up | 0% | 22% | 24% |
| Crouches to tie a shoe | 0% | 1% | 0% |
| Lies down slowly on a bed (12 s) | 0% | 13% | 10% |
| Was already lying (in bed) when first seen | 0% | 0% | 0% |
| Walks about | 0% | 0% | 0% |

Each cell is the share of trials in which an alert was raised: high for the fall rows is good, low for the others is good.

```

What it says: falls are found (at least 98% in every condition except 'low', which misses more as the person goes unseen); **the 3-second stay-down raises alerts for deep bends, crouching and stumbles that an 8-second stay-down does not, without missing a fall, which is why 8 is the default**; walking and sitting never raise one; and **lying down on purpose is raised at high sensitivity whatever the stay-down** (the known gap above). 'Normal' and 'low' raise fewer of those, at the cost of missing more falls.

**2. Your own clips** (`python -m app.fall_eval labels.json`). Record the cameras while staff or a colleague perform staged falls, sits, bends and lying down, and a lot of ordinary footage; write down in `labels.json` the second at which each fall happens; the tool runs the real AI worker loop (the person detector, tracker, pose model and decision) over each clip and reports labelled falls found and missed, false alarms, and **false alarms per camera-day**. Do this per camera before relying on it. The format is described at the top of `app/fall_eval.py`.

## What has and has not been checked

- The **decision** (what is and is not a fall) is tested on synthetic body-point sequences: a fall that stays down, a recovered stumble, bending, sitting, someone in bed, a slow lie-down, a fall behind furniture, one alarm per fall, sensitivity, and the stay-down time.
- The **whole path** is tested with a stand-in pose reader over a scripted scene: the AI worker records one fall, raises one alarm for a fall rule, raises none for a rule on another camera, and carries on analysing when the model is missing or fails.
- The **real model** is tested for loading, its input and output shape, and returning nothing for an empty picture (its pose score is a probability; points below 0.3 are discarded). It has **not** been run on pictures of people falling, and **no detection rate or false-alarm rate has been measured**. The numbers for your own cameras are unknown until you try it: have staff or a colleague perform staged falls, sits, bends and lying down in front of each camera, and note what is raised.
- The tool to run recorded clips and report detections and false alarms per camera-day exists (`app.fall_eval`, tested with stand-in models); it has **not been run on real footage**.
- Not covered: ceiling fisheye cameras, cameras that see only part of a person, and crowds (at most 8 people per frame of a camera are looked at).

## For installers

- `NVR_POSE_MODELS_DIR` (default `/opt/nvr/models/pose`) holds `pose_landmarks_detector.tflite`. The Docker image has it; a bare install without it shows "The pose model is not installed" and cannot turn a camera on.
- `GET /api/admin/falls` and `PUT /api/admin/falls/cameras/{id}` (administrators only; changes are audited as `falls_camera_changed`).
- Falls are kept in `fall_events` with the camera, the time and how long the person stayed down, and are deleted with the footage they were seen in.
