"""Fisheye dewarping geometry (#146): the one definition the browser's WebGL renderer and the export's FFmpeg remap both follow.

A 360-degree camera has a circular image: the lens centre ``(cx, cy)`` (fractions of the picture's width and height, y down), its ``radius`` (a fraction of the
picture's height) and its ``fov`` (the angle across the circle's diameter, 180 degrees for most). The lens is taken to be equidistant: a ray ``theta`` away from the
optical axis lands ``theta / (fov / 2)`` of the radius from the centre.

Camera frame: x right, y up, z along the optical axis. World frame: z up. The mount says how the two relate:

* ceiling: the optical axis points straight down; x_c = X, y_c = -Y, z_c = -Z.
* floor: the optical axis points straight up; x_c = X, y_c = Y, z_c = Z.
* wall: the optical axis is horizontal (along world Y); x_c = X, y_c = Z, z_c = Y.

A view looks along ``d(pan, tilt)``: ``pan`` is the azimuth (degrees) about the vertical axis, ``tilt`` the elevation above the horizon. A virtual-PTZ view is a pinhole
camera along ``d`` with the horizon kept level and a horizontal field of view ``fov``. A panorama maps its width to azimuth (360 degrees, 180 on a wall) and its height to
elevation, both linearly, with azimuth running the way a person turning right looks (smaller on a ceiling or floor mount, larger on a wall mount). A double panorama stacks two
180-degree panoramas, the second continuing where the first ends. Output pixel ``(i, j)`` is sampled at its centre; ``y`` is up in the formulas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MOUNTS = ("ceiling", "wall", "floor")
MODES = ("view", "panorama", "double_panorama")
MIN_VIEW_FOV = 20.0
MAX_VIEW_FOV = 120.0
# Output sizes: what an export renders, and the aspect the browser draws each mode in.
SIZES = {"view": (1280, 720), "panorama": (1920, 480), "double_panorama": (1280, 720)}
# Elevation covered by a panorama's height, top then bottom (degrees above the horizon), and the azimuth its width covers.
ELEVATION_RANGE = {"ceiling": (0.0, -80.0), "floor": (80.0, 0.0), "wall": (50.0, -50.0)}
AZIMUTH_SPAN = {"ceiling": 360.0, "floor": 360.0, "wall": 180.0}
# What pan and tilt may be for a mount (degrees).
TILT_RANGE = {"ceiling": (-90.0, 0.0), "floor": (0.0, 90.0), "wall": (-90.0, 90.0)}
PAN_RANGE = {"ceiling": (-180.0, 180.0), "floor": (-180.0, 180.0), "wall": (-90.0, 90.0)}
SUPERSAMPLE = 2  # Exports map each output pixel from this many samples across, then average, because FFmpeg's remap does not interpolate


@dataclass(frozen=True)
class Lens:
    mount: str = "ceiling"
    cx: float = 0.5
    cy: float = 0.5
    radius: float = 0.5
    fov: float = 180.0


@dataclass(frozen=True)
class View:
    mode: str = "view"
    pan: float = 0.0
    tilt: float = -90.0
    fov: float = 90.0


def clamp_view(mount: str, view: View) -> View:
    """The view with its pan, tilt and field of view brought into what the mount allows."""
    pan_low, pan_high = PAN_RANGE[mount]
    tilt_low, tilt_high = TILT_RANGE[mount]
    return View(view.mode, min(max(view.pan, pan_low), pan_high), min(max(view.tilt, tilt_low), tilt_high), min(max(view.fov, MIN_VIEW_FOV), MAX_VIEW_FOV))


def camera_axes(mount: str) -> np.ndarray:
    """Rows are the camera's x, y and z axes expressed in world coordinates."""
    if mount == "ceiling":
        return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    if mount == "floor":
        return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    if mount == "wall":
        return np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]])
    raise ValueError(f"Unknown mount {mount!r}")


def direction(mount: str, azimuth: np.ndarray | float, elevation: np.ndarray | float) -> np.ndarray:
    """World unit vectors for azimuth and elevation in degrees (arrays broadcast): shape (..., 3)."""
    a, e = np.radians(azimuth), np.radians(elevation)
    if mount == "wall":
        return np.stack([np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e) * np.ones_like(a)], axis=-1)
    return np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e) * np.ones_like(a)], axis=-1)


def view_basis(mount: str, pan: float, tilt: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """World right, up and forward vectors of a virtual camera looking along (pan, tilt) with the horizon level."""
    forward = direction(mount, pan, tilt)
    a, e = math.radians(pan), math.radians(tilt)
    if mount == "wall":
        up = np.array([-math.sin(e) * math.sin(a), -math.sin(e) * math.cos(a), math.cos(e)])
    else:
        up = np.array([-math.sin(e) * math.cos(a), -math.sin(e) * math.sin(a), math.cos(e)])
    return np.cross(forward, up), up, forward


def view_matrix(mount: str, pan: float, tilt: float) -> np.ndarray:
    """3x3 matrix taking a view ray (x right, y up, z forward) to the camera frame."""
    right, up, forward = view_basis(mount, pan, tilt)
    world_from_view = np.stack([right, up, forward], axis=1)
    return camera_axes(mount) @ world_from_view


def output_size(mode: str) -> tuple[int, int]:
    return SIZES[mode]


def rays(mount: str, view: View, width: int, height: int) -> np.ndarray:
    """Camera-frame rays (height, width, 3) through the centre of every pixel of a width x height output of the view's mode."""
    columns = (np.arange(width) + 0.5) / width * 2.0 - 1.0  # -1 (left) to 1 (right)
    rows = (np.arange(height) + 0.5) / height  # 0 (top) to 1 (bottom)
    x = np.broadcast_to(columns[None, :], (height, width))
    t = np.broadcast_to(rows[:, None], (height, width))
    axes = camera_axes(mount)
    if view.mode == "view":
        half = math.tan(math.radians(view.fov) / 2.0)
        y = 1.0 - 2.0 * t
        ray = np.stack([x * half, y * half * height / width, np.ones_like(x)], axis=-1)
        ray /= np.linalg.norm(ray, axis=-1, keepdims=True)
        return ray @ view_matrix(mount, view.pan, view.tilt).T
    top, bottom = ELEVATION_RANGE[mount]
    # A person turning right looks at a smaller azimuth on a ceiling or floor mount (azimuth grows counter-clockwise seen from above) and a larger one on a wall mount,
    # so a panorama runs towards smaller azimuths to the right on the first two: it agrees with a virtual view looking at the same place.
    sign = 1.0 if mount == "wall" else -1.0
    if view.mode == "panorama":
        span = AZIMUTH_SPAN[mount]
        azimuth = view.pan + sign * x * span / 2.0
        elevation = top + t * (bottom - top)
    elif view.mode == "double_panorama":
        strip = np.where(t < 0.5, 0, 1)
        within = np.where(t < 0.5, t * 2.0, t * 2.0 - 1.0)
        azimuth = view.pan + sign * (strip * 180.0 + (x + 1.0) / 2.0 * 180.0)
        elevation = top + within * (bottom - top)
    else:
        raise ValueError(f"Unknown mode {view.mode!r}")
    return direction(mount, azimuth, elevation) @ axes.T


def source_coordinates(lens: Lens, ray: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Where in a width x height fisheye picture each camera-frame ray lands: (x pixels, y pixels, inside the lens circle)."""
    z = np.clip(ray[..., 2], -1.0, 1.0)
    theta = np.arccos(z)
    r = theta / math.radians(lens.fov / 2.0)
    psi = np.arctan2(ray[..., 1], ray[..., 0])
    radius_px = lens.radius * height
    x = lens.cx * width + r * radius_px * np.cos(psi)
    y = lens.cy * height - r * radius_px * np.sin(psi)
    return x, y, r <= 1.0


def render(frame: np.ndarray, lens: Lens, view: View, width: int | None = None, height: int | None = None) -> np.ndarray:
    """The view of an RGB frame (height, width, 3, uint8) with bilinear sampling: the reference the browser's and the export's output are checked against."""
    out_w, out_h = (width, height) if width and height else output_size(view.mode)
    source_h, source_w = frame.shape[:2]
    x, y, inside = source_coordinates(lens, rays(lens.mount, clamp_view(lens.mount, view), out_w, out_h), source_w, source_h)
    x0, y0 = np.floor(x - 0.5), np.floor(y - 0.5)  # Pixel centres are at +0.5
    fx, fy = (x - 0.5 - x0)[..., None], (y - 0.5 - y0)[..., None]
    x0i, y0i = np.clip(x0.astype(int), 0, source_w - 1), np.clip(y0.astype(int), 0, source_h - 1)
    x1i, y1i = np.clip(x0i + 1, 0, source_w - 1), np.clip(y0i + 1, 0, source_h - 1)
    f = frame.astype(np.float64)
    out = (f[y0i, x0i] * (1 - fx) * (1 - fy) + f[y0i, x1i] * fx * (1 - fy) + f[y1i, x0i] * (1 - fx) * fy + f[y1i, x1i] * fx * fy)
    out[~inside] = 0
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def remap_maps(lens: Lens, view: View, source_width: int, source_height: int, supersample: int = SUPERSAMPLE) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    """16-bit pixel maps for FFmpeg's remap filter at ``supersample`` times the output size, and that output size. Outside the lens circle maps to pixel 0,0 of a black border (see ``graph``)."""
    out_w, out_h = output_size(view.mode)
    x, y, inside = source_coordinates(lens, rays(lens.mount, clamp_view(lens.mount, view), out_w * supersample, out_h * supersample), source_width, source_height)
    # remap reads whole pixels: the pixel whose area holds the point. The source is padded by one black pixel all round (index 0 and last), so outside maps there.
    xi = np.clip(np.floor(x), 0, source_width - 1).astype(np.int64) + 1
    yi = np.clip(np.floor(y), 0, source_height - 1).astype(np.int64) + 1
    xi[~inside], yi[~inside] = 0, 0
    return xi.astype(np.uint16), yi.astype(np.uint16), (out_w, out_h)


def write_pgm(path: Path, values: np.ndarray) -> None:
    with path.open("wb") as handle:
        handle.write(b"P5\n%d %d\n65535\n" % (values.shape[1], values.shape[0]))
        handle.write(values.astype(">u2").tobytes())


def graph(xmap: Path, ymap: Path, size: tuple[int, int], supersample: int = SUPERSAMPLE) -> str:
    """A filter graph (one input, one output) that dewarps a fisheye picture with the maps ``remap_maps`` made, then reduces it to ``size`` by averaging."""
    def source(name: Path) -> str:
        text = str(name)
        if any(character in text for character in "'\\[],;=\n"):
            raise ValueError("The dewarp map path has a character a filter graph cannot carry")  # Export work directories are named for a UUID, so this is never met
        return f"movie='{text.replace(':', chr(92) + ':')}',format=gray16le,loop=loop=-1:size=1"
    return (f"format=rgb24,pad=iw+2:ih+2:1:1:black[v];{source(xmap)}[xm];{source(ymap)}[ym];[v][xm][ym]remap,"
            f"scale={size[0]}:{size[1]}:flags=area,setsar=1")


