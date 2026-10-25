# Camera tamper detection

The NVR can tell when a camera has been covered, moved or put out of focus. It is **off by default** and is switched on per camera under **Cameras › the camera › Tamper**.

## How it works

While detection is on, the recorder takes one small grayscale frame (160 × 90) from the camera's low-resolution stream (its main stream if it has none) every 15 seconds, at most two cameras at a time, in a task of its own. It compares each frame with a **reference**: the average of the first five frames it sees, which then slowly takes on the scene as it changes, so dusk and dawn are followed and raise nothing.

A frame is judged on three measurements that a change of light alone cannot fake:

| Kind | What it means | How it is told |
|---|---|---|
| **Covered** | a cloth, tape, paint or a hand over the lens | the frame has lost its contrast (flat black or flat white), or its brightness has collapsed against the reference |
| **Moved** | the camera was turned, knocked or taken down | the picture's layout no longer matches: the correlation of the average brightness of a 32 × 18 grid of blocks, which brightening or darkening the whole scene does not change |
| **Defocused** | lens out of focus, smeared or fogged | the layout still matches but the edges have softened, measured by edge strength relative to contrast (also unchanged by brightening or darkening) |

A change must last **the configured time** (default 60 seconds, 10 to 3,600) before it counts, so a passing lorry, a flash or a moment of glare raises nothing. Time nobody was watching does not count towards it: if no sample was taken for a while (the recorder was down, or the camera gave no picture), a change that had begun starts over. Then:

- an **incident** is raised for the camera, `tamper_covered`, `tamper_moved` or `tamper_defocused`, which shows with the other incidents and sends a `tamper.detected` webhook;
- any **rule** with the *Camera is covered, moved or defocused* condition fires (see below);
- it **recovers** when two samples in a row look normal again, with a `tamper.recovered` webhook.

**Sensitivity** (1 to 100, default 50) moves all the thresholds together: at 50 a camera has to lose about three quarters of its contrast, or its picture has to change layout noticeably, or its edges lose about a third of their strength; at 100 smaller changes count.

A camera with a very flat or very dark view (nothing for the comparison to hold on to) is not judged for the thing it cannot measure: a scene with no contrast cannot show a lost contrast, and one with no layout cannot show a move.

A camera is watched whether or not it is recording, since a covered camera matters most when it is not; a camera that is disabled or archived is not. Switching detection off ends any open detection and forgets nothing but the state; switching it on again learns the view afresh, because the scene may have changed meanwhile. Saving other settings while a detection is open ends it too (it is raised again if the camera is still tampered with). When one kind gives way to another, the first incident recovers and the second is raised once it has lasted the configured time.

## After a deliberate change

If you move the camera or rearrange what it watches on purpose, choose **Re-learn reference**. The view is learned again from the next five frames and any detection ends at once; nothing is judged while it learns, and a frame that was already being fetched when you asked is thrown away rather than mixed into the new view.

## Rules

A rule can use the condition *Camera is covered, moved or defocused*, with the kinds it should act on (all three by default). It needs no object classes. It fires once when a detection is confirmed, honours the rule's cameras and groups, schedule and cooldown, raises an alarm labelled with the kind ("Camera covered", "Camera moved" or "Camera defocused" in the alarm inbox, the Live banner, push messages and rule-action messages) and runs the rule's actions. If a rule cannot be run, the incident is still recorded and the failure is logged. A rule only fires for detections after it was saved. **Test mode** replays the tamper incidents of the last 24 hours.

## API

All under `/api/cameras/{id}/tamper`:

- `GET` returns the settings, the state (`off`, `learning`, `ok`, `covered`, `moved`, `defocused`), how many frames the reference has, and the last measurements. Anyone who can see the camera may read it.
- `PUT` (administrator) sets `{"enabled", "sensitivity", "sustain_seconds"}`. It is audited as `tamper_configured`. Switching it off ends any detection.
- `POST …/relearn` (administrator) forgets the reference. It is audited as `tamper_relearned`.

The state of each camera that has it on is also a metric: `nvr_camera_tamper_state{camera_id,camera,state}`.

## Limits

- A very slow tamper, such as a lens fogging over hours, is followed by the reference like any gradual change and is not seen.
- Switching the lights off at once looks like a covered camera, because it cannot be told apart from one. At the default sensitivity the light has to fall by about three quarters; a lower sensitivity needs more.
- A frame that cannot be had (the camera is unreachable) is not tampering: that is the recording health's to report.
- It looks at one frame at a time every 15 seconds (`NVR_TAMPER_SAMPLE_SECONDS` changes it), so a change is confirmed at the first sample after the configured time has passed.
- A moving crowd or a large vehicle parked in front of the camera for longer than the configured time can read as a moved or covered camera. Raise the time, lower the sensitivity, or re-learn.
