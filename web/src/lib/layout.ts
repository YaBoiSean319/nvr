/** Auto layout from the approved table: selected cameras → columns × rows. */
export const MAX_DISPLAYED_CAMERAS = 16;

export function autoGrid(count: number): { columns: number; rows: number } {
  if (count <= 1) return { columns: 1, rows: 1 };
  if (count === 2) return { columns: 2, rows: 1 };
  if (count <= 4) return { columns: 2, rows: 2 };
  if (count <= 6) return { columns: 3, rows: 2 };
  if (count <= 9) return { columns: 3, rows: 3 };
  if (count <= 12) return { columns: 4, rows: 3 };
  return { columns: 4, rows: 4 };
}

/** Largest 16:9 tile that fits the grid in the available space. */
export function fitTiles(width: number, height: number, columns: number, rows: number, gap: number) {
  const byWidth = (width - gap * (columns - 1)) / columns;
  const byHeight = ((height - gap * (rows - 1)) / rows) * (16 / 9);
  const tileWidth = Math.max(0, Math.floor(Math.min(byWidth, byHeight)));
  return { tileWidth, tileHeight: Math.floor((tileWidth * 9) / 16) };
}
