# PTZ: pan, tilt, zoom, presets and tours

Cameras with an ONVIF PTZ service can be moved from Live and from the camera editor. Every command goes through the NVR to the camera; browsers never talk to cameras.

## Moving a camera

- **In Live**, the focused tile of a camera with an ONVIF device service shows a movement pad when the camera reports pan, tilt or zoom.
  - **Buttons:** press and hold an arrow to pan or tilt, and **+** or **−** to zoom. Letting go stops the camera.
  - **Keyboard:** with the tile focused, hold the arrow keys and **+** / **−**. Enter or Space also hold a focused button.
  - **Touch:** press and hold works on phones too. Every control is at least 40 px.
- **Centre on click** (for cameras that support relative moves in field-of-view space): turn it on, then click a point in the picture to centre the camera there.
- **Go to preset** recalls a saved position.
- A move lasts at most 1.5 seconds unless renewed, so a camera stops by itself if the connection drops mid-move.

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
