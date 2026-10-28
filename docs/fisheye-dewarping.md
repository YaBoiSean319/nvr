# 360° cameras: dewarping in Live and in exports

A 360° (fisheye) camera records a round picture. Tell the NVR where the circle is and how the camera is mounted and it can show the picture as a panorama, a double panorama or up to four virtual PTZ views, and an export can render one. **Recordings, playback, snapshots and the synchronized grid stay the raw circle.**

## Set the lens

Administrators open the camera in **Cameras**, tab **Lens (360°)**:

- **Camera type:** *360° fisheye camera*.
- **Mount:** ceiling (looking down), wall (looking out) or floor or table (looking up).
- **Circle centre** (across and down, % of the picture), **radius** (% of the picture's height) and the **field of view** across the circle. The defaults suit most cameras: a circle filling the picture's height, centred, 180°. Choose *Raw 360°* on a Live tile to see the circle and check the numbers.

Changes are in the audit log (`camera_lens_changed`). Everyone who may see the camera can read its lens; it is not offered to anyone else.

## In Live

A fisheye camera's tile has a menu next to its quality menu: **Raw 360°**, **Panorama**, **Double panorama** (ceiling and floor mounts) or **1 to 4 views**. The browser dewarps with WebGL; the video keeps playing as usual.

- A **panorama** is the whole way round (180° for a wall mount) with azimuth running left to right the way a person turning right would look. A **double panorama** is two half circles, the second continuing where the first ends. Drag sideways or use the left and right arrow keys to scroll; it wraps round.
- A **virtual view** is a pinhole camera looking where you point it, with the horizon level. One view fills the tile; two sit side by side; three are one large and two small; four make a grid. Each view has its own pan, tilt and zoom.
  - **Mouse:** drag to turn (the picture follows the pointer), the wheel zooms.
  - **Touch:** drag with one finger, pinch with two.
  - **Keyboard:** focus a view (Tab; each is a group named *Virtual view 2 of 3*), then the arrow keys turn it (Shift for bigger steps), **+** and **−** zoom.
  A ceiling camera cannot look above the horizon, nor a wall camera beyond its sides: pan, tilt and field of view (20° to 120°) are held to what the mount allows.
- **Saved views keep them.** *Save as…* and *Save* keep, for each fisheye camera in the view, its mode and each virtual view's pan, tilt and zoom; loading the view brings them back, and the workspace remembers them across a reload. Changing a view's direction makes a loaded view show *Unsaved changes*. Personal and shared views both keep them.
- Without WebGL the tile shows the raw picture with a notice.
- Snapshots from a tile are the raw picture.

## In exports

In Playback's export drawer, each selected fisheye camera has a menu: **Raw 360° circle**, **Panorama**, **Double panorama** or **Virtual view** (pan, tilt and field of view, starting from how the camera is shown in Live). The camera's individual file is rendered from the raw recording with the geometry below, so it matches the screen. It is 1280×720 for a view, 1920×480 for a panorama and 1280×720 for a double panorama. The package's manifest records the view and the lens used.

A dewarped view cannot be combined with blurring faces and plates (those regions are marked on the raw picture), and needs the individual output; the grid shows the raw circle. Privacy masks are applied to the raw picture first, so they are covered in the dewarped file too.

Through the API: `dewarp: {"<camera id>": {"mode": "view" | "panorama" | "double_panorama", "pan": 0, "tilt": -90, "fov": 90}}` on `POST /api/exports`.

## The geometry

One definition serves the browser (`web/src/features/dewarp/`) and the export (`app/dewarp.py`):

- The lens is equidistant: a ray `theta` away from the optical axis lands `theta / (fov / 2)` of the radius from the circle's centre.
- Camera frame: x right, y up, z along the optical axis. World frame: z up. Ceiling: the axis points down; floor: up; wall: horizontal.
- A virtual view looks along azimuth `pan` and elevation `tilt` (degrees above the horizon: −90 is straight down for a ceiling camera) with a horizontal field of view `fov`. A panorama maps width to azimuth and height to elevation, both linearly: elevation 0° to −80° for a ceiling camera, 80° to 0° for a floor camera, +50° to −50° for a wall camera.
- The export maps every output pixel with FFmpeg's `remap` at twice the size and averages down, because `remap` does not interpolate.

## Limits

- An export renders one view for each camera (the first Live view's direction is offered as the start), not every view of a tile. A file is 16:9 (panorama 4:1), so a Live view in a cell of another shape (two or three views side by side) shows more or less above and below than the file does; the direction and horizontal field of view are the same. A retry renders with the lens of the original request.
- Each dewarped tile uses a WebGL context and browsers allow about sixteen at once: a wall of many 360° tiles may lose the oldest, which then shows the raw picture with a notice.
- Dewarping is Live and exports only: Playback shows the raw circle.
- Lenses are modelled as equidistant. A camera whose lens follows another curve will show straight lines slightly curved near the rim; there is no per-lens calibration beyond centre, radius and field of view.
- A tile of mixed cameras keeps each camera's own mode; low-data mode and the quality governor work on the stream as before.
