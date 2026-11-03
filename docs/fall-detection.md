# Fall detection

**This is an aid, not a safety system.** It will miss some falls and it will raise alerts that are not falls. It does not replace call buttons, regular checks or supervision, and it has not been measured on real cameras (see "What has and has not been checked").

On the cameras you choose, the NVR looks for a person who has fallen and stayed down, and raises an alarm: with the clip and snapshot, in the alarm inbox, by push and by any rule action (webhook, Slack, Teams, relay), like every other alarm.

## Turning it on

1. **Settings > Fall detection**: tick **Look for falls on** each camera that should be watched. It is off for every camera until you do. Set the **sensitivity** (high by default: catches more falls, raises more alerts that are not falls) and **how many seconds a person must stay down** (3 by default).
2. **Rules** (Cameras > Rules, or the **Create an alert rule for these cameras** button in Settings > Fall detection, which also warns while watched cameras have no rule): make a rule with the condition **A person fell**, choose its cameras or groups, an optional schedule and cooldown, and the actions that tell staff. Turning detection on only records falls; the rule is what alerts anyone. A fall raises one alarm, labelled **Person fell**.
3. Changes reach the AI worker within about half a minute, with no restart.

## How it decides

The AI worker already finds and follows each person. For a camera with fall detection on, on every frame the AI worker analyses (the camera's **AI analysis rate**: 0.5 frames a second by default, and **at least 1 to 2 is recommended** for falls; Settings warns when it is too slow) it asks a pose model for the position of each person's shoulders, hips, knees and ankles, and watches how the body changes:

- A **fall** is a person who was **upright**, went **low** (the torso well away from vertical, or the body wider than tall) within a few seconds, and **stays low** for the stay-down time.
- Someone **first seen lying down** (a resident in bed, on a sofa) is **not** a fall: nothing is raised for someone never seen upright.
- Someone who **lowers themselves slowly** (sitting down, lying down on a bed) is not a fall.
- Someone who **gets up** within the stay-down time (a stumble that recovers) is not a fall.
- A person who goes **out of sight while low** (behind a bed or furniture) still counts, for a few seconds, because that is often what a fall looks like.
- One fall raises one alarm; the person has to be upright again for a moment before another can.

| Sensitivity | Low from a torso angle of | Upright below | A drop may take | Unseen time that still counts |
| --- | --- | --- | --- | --- |
| High (default) | 50° from upright | 40° | 4 s | 6 s |
| Normal | 60° | 35° | 3 s | 4 s |
| Low | 70° | 30° | 2 s | 3 s |

A body wider than tall counts as low 10° sooner than the angle above. Frames in between "upright" and "low", or where the body points cannot be seen, neither start nor end a fall; only an upright frame clears one.

## What it needs from the cameras

- A view where people are **large enough to see** and not mostly hidden. The AI worker analyses a small copy of the picture (448 × 256), so a person far from the camera is only a few dozen pixels tall and the body points are rough. Wide-angle views of corridors, lounges and dining rooms where people walk in the open suit it best; a camera on a bed behind rails, a bathroom, or a ceiling fisheye (which sees people from above) does not.
- Fall detection watches **people the NVR is already tracking**, so a camera needs AI analysis turned on as well, and the person has to be found as a person. It never looks at anyone it has not found.
- **Privacy masks:** a person inside a masked zone is never looked at by the pose model. Access grants, the viewing audit and retention apply to its alarms and clips as to every other. Whether a camera may cover a resident's room is the home's decision (consent and policy), not something the software settles.

## The model

MediaPipe **Pose Landmarker (lite)**, licensed Apache-2.0 by the MediaPipe project (the licence of the trained weights was taken from the project and not independently verified here). It is downloaded when the image is built and checked against a pinned SHA-256 (`app/pose_models.py` lists it and a test keeps the `Dockerfile` the same); only its landmark model is used, run with OpenVINO on the CPU, about 4 ms per person. Nothing that needs a licence is used: Ultralytics YOLO pose (AGPL-3.0 or a paid licence) is deliberately not part of this. The decision about falls works on a handful of body points only, so a different permissively licensed model could replace it later.

## What has and has not been checked

- The **decision** (what is and is not a fall) is tested on synthetic body-point sequences: a fall that stays down, a recovered stumble, bending, sitting, someone in bed, a slow lie-down, a fall behind furniture, one alarm per fall, sensitivity, and the stay-down time.
- The **whole path** is tested with a stand-in pose reader over a scripted scene: the AI worker records one fall, raises one alarm for a fall rule, raises none for a rule on another camera, and carries on analysing when the model is missing or fails.
- The **real model** is tested for loading, its input and output shape, and returning nothing for an empty picture (its pose score is a probability; points below 0.3 are discarded). It has **not** been run on pictures of people falling, and **no detection rate or false-alarm rate has been measured**. The numbers for your own cameras are unknown until you try it: have staff or a colleague perform staged falls, sits, bends and lying down in front of each camera, and note what is raised.
- Not built: a tool to run recorded clips through the detector and report detections and false alarms per camera-day. That is the next thing to add before relying on it.
- Not covered: ceiling fisheye cameras, cameras that see only part of a person, and crowds (at most 8 people per frame of a camera are looked at).

## For installers

- `NVR_POSE_MODELS_DIR` (default `/opt/nvr/models/pose`) holds `pose_landmarks_detector.tflite`. The Docker image has it; a bare install without it shows "The pose model is not installed" and cannot turn a camera on.
- `GET /api/admin/falls` and `PUT /api/admin/falls/cameras/{id}` (administrators only; changes are audited as `falls_camera_changed`).
- Falls are kept in `fall_events` with the camera, the time and how long the person stayed down, and are deleted with the footage they were seen in.
