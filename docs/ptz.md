# PTZ: pan, tilt, zoom, presets and tours

Cameras with an ONVIF PTZ service can be moved from Live and from the camera editor. Every command goes through the NVR to the camera; browsers never talk to cameras.

## Moving a camera

- **In Live**, the focused tile of a camera with an ONVIF device service shows a movement pad when the camera reports pan, tilt or zoom.
  - **Buttons:** press and hold an arrow to pan or tilt, and **+** or **−** to zoom. Letting go stops the camera.
  - **Keyboard:** with the tile focused, hold the arrow keys and **+** / **−**. Enter or Space also hold a focused button.
  - **Touch:** press and hold works on phones too. Every control is at least 40 px.
- **Centre on click**, on a Live tile: turn it on, then click a point in the picture to centre the camera there. It is offered for cameras that support relative moves in field-of-view space (`TranslationSpaceFov`). Absolute and generic relative moves give no way to know how far a point in the picture is, so those cameras get the arrows only.
- **Go to preset** recalls a saved position.
- A move lasts at most 1.5 seconds unless renewed, so a camera stops by itself if the connection drops mid-move. Leaving the window or tab also stops it. Commands reach the camera in the order given.

## One person at a time

- The first command takes control of the camera (the **lease**), and each command renews it. Control lapses 60 seconds after the last command, or when the holder presses **Release**.
- Others see **Controlled by <name>**, and their controls are disabled. The server refuses their commands with the holder's name.
- Administrators can **Take over**, which is recorded in the audit log.
- Operators may move cameras and recall presets. Only administrators save, rename and delete presets and edit tours.

## Presets and tours

Open a camera's **PTZ** tab in **Cameras**:

- **Presets:** move the camera, then **Save the current position as** a name. Presets can be renamed, recalled and deleted. A preset a tour uses cannot be deleted until the tour stops using it.
- **Tours** visit presets in order and stay a set time on each (3 seconds to an hour), optionally only at scheduled times. One tour per camera can be enabled.
- **A tour pauses while anyone holds the lease** and continues with its next step once control lapses or is released. A changed tour starts again from its first step.
- Tours run in the background worker. A camera that does not answer is tried again 5 seconds later.

## API

| Method | Path | Who | What |
|---|---|---|---|
| `GET` | `/api/cameras/{id}/ptz` | signed in | Whether the camera can move (and why not), its capabilities, the lease, presets and tours |
| `POST` | `/api/cameras/{id}/ptz/move` | signed in | `{pan, tilt, zoom}` velocities from −1 to 1, and optional `seconds` (default 1, at most 10). Takes or renews the lease |
| `POST` | `/api/cameras/{id}/ptz/stop` | signed in | Stop moving |
| `POST` | `/api/cameras/{id}/ptz/centre` | signed in | `{x, y}` from 0 to 1, top left first |
| `POST` | `/api/cameras/{id}/ptz/presets/{preset}/go` | signed in | Recall a preset |
| `POST` / `DELETE` | `/api/cameras/{id}/ptz/lease` | signed in | Take control (`{"take_over": true}` for administrators), or release it |
| `POST` | `/api/cameras/{id}/ptz/presets` | administrator | `{name}`: save the current position |
| `PATCH` / `DELETE` | `/api/cameras/{id}/ptz/presets/{preset}` | administrator | Rename or delete |
| `POST` / `PUT` / `DELETE` | `/api/cameras/{id}/ptz/tours[/{tour}]` | administrator | `{name, enabled, steps: [{preset_id, dwell_seconds}], schedule}` |

Commands from someone else while the lease is held answer `409` with the holder's name. A camera that refuses a command answers `502`, without echoing the camera's reply.

## Auto-tracking

A PTZ camera can follow a person or vehicle. The server steers it from the AI worker's detections, to keep the object centred and a set size.

- **Start from Live:** on the focused tile of a camera that can pan and tilt, press **Follow**, then click the box of the person or vehicle in the picture. (On the camera's PTZ tab, **Follow** lists the objects in view instead.) Only objects seen in the last few seconds can be chosen. **Stop following** ends it.
- **Start from a rule:** add the action **PTZ camera follows the object** to a rule. When the rule fires, the camera it fired on, if it can pan and tilt, follows the object that triggered it. A rule never takes a camera from a person who is driving it, nor from other tracking: it is left alone and the delivery log says why (`ptz_lease_held`, `ptz_already_tracking`).
- **The lease:** tracking holds the camera's control lease (shown as the person who started it, or the rule's author) and renews it every pass. Each pass, and each command, first checks the lease is still the tracking's own; without it the tracking ends and nothing is sent. (The check cannot be atomic with the camera: a manual command that lands just after it is followed by that one move, under a second long, and then nothing more from the tracker.) Tours wait while a camera is tracking.
- **Manual control wins:** a manual command from anyone who may move the camera, or **Take over**, takes the lease and ends the tracking at once. Choosing another object in Live switches the target.
- **Losing the object:** an object not seen for the lost time (default 5 seconds) stops the camera. If it comes back within the home timeout (default 30 seconds, counted from when it was counted lost) it is followed again; otherwise the camera goes to its home preset, if one is set, the tracking ends and the lease is let go.
- **Privacy masks:** an object in a privacy-masked area counts as not seen, so the camera never follows what a mask hides, and such objects are not offered for choosing.
- **Settings** (administrators, the camera's **PTZ** tab): the home preset, the lost time (1 to 60 seconds), the home timeout (5 seconds to an hour), the size to keep the object (share of the picture's height) and whether to zoom. A camera that stops answering ends the tracking after three failed commands.
- Starting, ending and interrupting are in the audit log (`ptz_tracking_started`, `ptz_tracking_ended` with the reason, `ptz_tracking_interrupted`, `ptz_tracking_settings_changed`). Tracking runs in the background worker; each command moves the camera for under a second, so a stalled NVR leaves it still.

How it steers: the object's newest box is compared with the centre of the picture, and pan and tilt speeds are proportional to the error, with a dead zone so a centred object gets no command; zoom follows the object's size once it is near the centre. Detections come from the AI worker, so how smoothly a camera follows depends on how often it analyses that camera.
