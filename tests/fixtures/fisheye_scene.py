"""A synthetic fisheye picture of a room with straight lines, for the dewarping tests (#146).

The camera sits at the origin of a round room (radius ``WALL`` metres, a floor ``FLOOR`` metres below the camera). The wall has vertical stripes every 15 degrees
and horizontal bands at fixed heights; the floor is a grid. All are straight lines in the world, so a correct dewarp shows them straight: vertical stripes and
horizontal bands (constant elevation) in a panorama, the floor grid in a virtual view that looks straight down. Sectors of the wall are tinted so the direction of
a pan can be checked. The picture is what an equidistant ceiling-mounted lens would record (``app.dewarp`` defines the lens model).
"""
from __future__ import annotations

import math

import numpy as np

from app import dewarp

WALL = 3.0
FLOOR = 2.5
WIDTH, HEIGHT = 1280, 720
STRIPE_DEGREES = 15.0
STRIPE_WIDTH = 0.8  # degrees
BAND_HEIGHTS = (-1.0, 0.0, 1.0)  # metres relative to the camera, on the wall
BAND_WIDTH = 0.035  # metres
GRID_SPACING = 0.5
GRID_WIDTH = 0.025
TINTS = {0: (255, 190, 190), 1: (190, 255, 190), 2: (190, 190, 255), 3: (255, 255, 170)}  # By quarter of the azimuth, 0 to 90 degrees first


def scene_color(world: np.ndarray, mount: str = "ceiling") -> np.ndarray:
    """RGB for each world ray (..., 3) from the camera, which looks along the mount's optical axis."""
    wx, wy, wz = world[..., 0], world[..., 1], world[..., 2]
    horizontal = np.hypot(wx, wy)
    sign = -1.0 if mount != "floor" else 1.0  # The floor-mount camera looks up, at a ceiling that is the same distance away
    hits_floor = (sign * wz > 0) & (FLOOR * horizontal < WALL * np.abs(wz))
    out = np.full(world.shape[:-1] + (3,), 255.0)
    # Wall
    azimuth = np.degrees(np.arctan2(wy, wx)) % 360.0
    tint = np.array([TINTS[int(q)] for q in range(4)], dtype=np.float64)[np.minimum((azimuth // 90).astype(int), 3)]
    out[:] = tint
    distance_to_stripe = np.abs(((azimuth + STRIPE_DEGREES / 2) % STRIPE_DEGREES) - STRIPE_DEGREES / 2)
    height_on_wall = WALL * wz / np.maximum(horizontal, 1e-9)
    on_stripe = distance_to_stripe < STRIPE_WIDTH / 2
    on_band = np.zeros(wz.shape, dtype=bool)
    for band in BAND_HEIGHTS:
        on_band |= np.abs(height_on_wall - band) < BAND_WIDTH / 2
    out[on_stripe | on_band] = 0
    # Floor (or ceiling, for a floor mount): a white plane with a black grid
    scale = FLOOR / np.maximum(np.abs(wz), 1e-9)
    gx, gy = wx * scale, wy * scale
    on_grid = (np.abs(((gx + GRID_SPACING / 2) % GRID_SPACING) - GRID_SPACING / 2) < GRID_WIDTH / 2) | (np.abs(((gy + GRID_SPACING / 2) % GRID_SPACING) - GRID_SPACING / 2) < GRID_WIDTH / 2)
    plane = np.where(on_grid[..., None], 0.0, 235.0) * np.ones(3)
    out[hits_floor] = plane[hits_floor]
    return out


def fisheye_frame(lens: dewarp.Lens | None = None, width: int = WIDTH, height: int = HEIGHT, samples: int = 3) -> np.ndarray:
    """The fisheye picture (height, width, 3, uint8): black outside the lens circle, antialiased by averaging samples x samples rays per pixel."""
    lens = lens or dewarp.Lens()
    axes = dewarp.camera_axes(lens.mount)
    accumulated = np.zeros((height, width, 3))
    inside_count = np.zeros((height, width))
    for sy in range(samples):
        for sx in range(samples):
            px = (np.arange(width)[None, :] + (sx + 0.5) / samples - lens.cx * width) * np.ones((height, 1))
            py = (lens.cy * height - (np.arange(height)[:, None] + (sy + 0.5) / samples)) * np.ones((1, width))
            r = np.hypot(px, py) / (lens.radius * height)
            inside = r <= 1.0
            theta = r * math.radians(lens.fov / 2.0)
            psi = np.arctan2(py, px)
            ray_c = np.stack([np.sin(theta) * np.cos(psi), np.sin(theta) * np.sin(psi), np.cos(theta)], axis=-1)
            world = ray_c @ axes  # camera-frame -> world: rows of `axes` are the camera axes in world coordinates
            color = scene_color(world, lens.mount)
            accumulated += np.where(inside[..., None], color, 0.0)
            inside_count += inside
    return np.clip(accumulated / (samples * samples) + 0.5, 0, 255).astype(np.uint8)
