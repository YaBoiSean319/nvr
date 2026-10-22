from __future__ import annotations

from typing import Sequence, cast


LIVE_GRID_SIZES = {1, 4, 9, 16}


def normalize_live_grid_selection(
    camera_ids: Sequence[str],
    grid_size: int,
    tile_camera_ids: Sequence[str | None] | None,
    *,
    allow_empty: bool,
    empty_tiles_are_unset: bool,
    duplicate_context: str,
    overflow_error: str,
) -> tuple[list[str], int, list[str | None]]:
    """Validate and canonicalize camera selections and their grid positions."""
    selected = list(camera_ids)
    tiles = list(tile_camera_ids) if tile_camera_ids is not None else None
    tiles_unset = tiles is None or (empty_tiles_are_unset and not tiles)
    if tiles_unset and grid_size == 4 and len(selected) > 4:
        grid_size = 9 if len(selected) <= 9 else 16
    if grid_size not in LIVE_GRID_SIZES:
        raise ValueError("Grid size must be 1, 4, 9, or 16")
    if not tiles_unset and len(tiles) != grid_size:
        raise ValueError("Tile positions must match the selected grid size")
    if tiles_unset:
        tiles = [*selected, *([None] * max(0, grid_size - len(selected)))]

    tile_ids = [camera_id for camera_id in tiles if camera_id is not None]
    if not tile_ids and not selected and not allow_empty:
        raise ValueError("A saved view must contain at least one camera")
    if len(set(selected)) != len(selected):
        raise ValueError(f"A camera can appear only once in a {duplicate_context}")
    if len(set(tile_ids)) != len(tile_ids):
        raise ValueError(f"A camera can appear only once in a {duplicate_context}")
    if selected and set(selected) != set(tile_ids):
        raise ValueError("Camera selection must match the populated tile positions")
    if len(tiles) != grid_size:
        raise ValueError(overflow_error)
    return tile_ids, grid_size, tiles


def normalize_group_name(value: str | None, *, required: bool = False) -> str | None:
    """Trim a group name and normalize optional blank values to ``None``."""
    normalized = value.strip() if isinstance(value, str) else value
    if not normalized:
        if required:
            raise ValueError("Group name is required")
        return None
    return normalized


def normalize_required_group_name(value: str) -> str:
    return cast(str, normalize_group_name(value, required=True))
