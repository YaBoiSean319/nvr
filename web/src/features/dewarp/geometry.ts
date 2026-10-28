/**
 * Fisheye dewarping geometry (#146): the same definition `app/dewarp.py` follows for exports, written for the browser.
 *
 * Camera frame: x right, y up, z along the optical axis. World frame: z up. A ceiling mount looks straight down, a floor mount straight up, a wall mount
 * horizontally. A view looks along azimuth `pan` and elevation `tilt` (degrees); a panorama maps its width to azimuth and its height to elevation.
 */

export type Mount = "ceiling" | "wall" | "floor";
export type Mode = "raw" | "panorama" | "double_panorama" | "views";

export interface Lens { mount: Mount; center_x: number; center_y: number; radius: number; fov: number }
export interface VirtualView { pan: number; tilt: number; fov: number }
/** How one fisheye camera is shown: raw, a panorama (starting at azimuth `pan`), a double panorama, or one to four virtual views. */
export interface DewarpState { mode: Mode; pan: number; views: VirtualView[] }

export const MIN_VIEW_FOV = 20;
export const MAX_VIEW_FOV = 120;
export const MAX_VIEWS = 4;
export const PANORAMA_ASPECT = 1920 / 480;
export const DOUBLE_PANORAMA_ASPECT = 1280 / 720;
export const ELEVATION_RANGE: Record<Mount, [number, number]> = { ceiling: [0, -80], floor: [80, 0], wall: [50, -50] };
export const AZIMUTH_SPAN: Record<Mount, number> = { ceiling: 360, floor: 360, wall: 180 };
export const TILT_RANGE: Record<Mount, [number, number]> = { ceiling: [-90, 0], floor: [0, 90], wall: [-90, 90] };
export const PAN_RANGE: Record<Mount, [number, number]> = { ceiling: [-180, 180], floor: [-180, 180], wall: [-90, 90] };

export const RAW: DewarpState = { mode: "raw", pan: 0, views: [] };

const clamp = (value: number, low: number, high: number) => Math.min(Math.max(value, low), high);

export function clampView(mount: Mount, view: VirtualView): VirtualView {
  return {
    pan: clamp(view.pan, ...PAN_RANGE[mount]),
    tilt: clamp(view.tilt, ...TILT_RANGE[mount]),
    fov: clamp(view.fov, MIN_VIEW_FOV, MAX_VIEW_FOV),
  };
}

/** Rows are the camera's x, y and z axes in world coordinates (row-major, nine numbers). */
export function cameraAxes(mount: Mount): number[] {
  if (mount === "ceiling") return [1, 0, 0, 0, -1, 0, 0, 0, -1];
  if (mount === "floor") return [1, 0, 0, 0, 1, 0, 0, 0, 1];
  return [1, 0, 0, 0, 0, 1, 0, 1, 0];
}

function direction(mount: Mount, azimuth: number, elevation: number): [number, number, number] {
  const a = (azimuth * Math.PI) / 180;
  const e = (elevation * Math.PI) / 180;
  return mount === "wall"
    ? [Math.cos(e) * Math.sin(a), Math.cos(e) * Math.cos(a), Math.sin(e)]
    : [Math.cos(e) * Math.cos(a), Math.cos(e) * Math.sin(a), Math.sin(e)];
}

const cross = (a: number[], b: number[]): [number, number, number] => [a[1]! * b[2]! - a[2]! * b[1]!, a[2]! * b[0]! - a[0]! * b[2]!, a[0]! * b[1]! - a[1]! * b[0]!];

/** A 3x3 matrix (row-major) taking a view ray (x right, y up, z forward) to the camera frame, for a view looking along (pan, tilt) with the horizon level. */
export function viewMatrix(mount: Mount, pan: number, tilt: number): number[] {
  const forward = direction(mount, pan, tilt);
  const a = (pan * Math.PI) / 180;
  const e = (tilt * Math.PI) / 180;
  const up: [number, number, number] = mount === "wall"
    ? [-Math.sin(e) * Math.sin(a), -Math.sin(e) * Math.cos(a), Math.cos(e)]
    : [-Math.sin(e) * Math.cos(a), -Math.sin(e) * Math.sin(a), Math.cos(e)];
  const right = cross(forward, up);
  const axes = cameraAxes(mount);
  // camera-from-view = axes (rows are camera axes in world) x world-from-view (columns right, up, forward)
  const worldFromView = [right[0], up[0], forward[0], right[1], up[1], forward[1], right[2], up[2], forward[2]];
  const result: number[] = [];
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 3; column += 1) {
      result.push(axes[row * 3]! * worldFromView[column]! + axes[row * 3 + 1]! * worldFromView[3 + column]! + axes[row * 3 + 2]! * worldFromView[6 + column]!);
    }
  }
  return result;
}

// --- layout of what a tile shows ---------------------------------------------------------------------------------------------

/** A rectangle of the tile (fractions of its width and height, from the top left) and what it shows. */
export interface Region {
  x: number;
  y: number;
  width: number;
  height: number;
  /** "view" for a virtual view (`index` says which); the panorama modes for the strips. */
  kind: "view" | "panorama" | "double_panorama";
  index: number;
}

/** Where the views of a state sit in a tile of the given aspect (width / height). One view fills the tile; two sit side by side; three are one large and two small; four make a grid. */
export function regions(state: DewarpState, tileAspect: number): Region[] {
  if (state.mode === "panorama" || state.mode === "double_panorama") {
    const aspect = state.mode === "panorama" ? PANORAMA_ASPECT : DOUBLE_PANORAMA_ASPECT;
    // Keep the strip's own proportions, centred, with black bars where the tile is a different shape.
    const width = Math.min(1, aspect / tileAspect);
    const height = Math.min(1, tileAspect / aspect);
    return [{ x: (1 - width) / 2, y: (1 - height) / 2, width, height, kind: state.mode, index: 0 }];
  }
  if (state.mode !== "views") return [];
  const count = Math.min(state.views.length, MAX_VIEWS);
  const cell = (x: number, y: number, width: number, height: number, index: number): Region => ({ x, y, width, height, kind: "view", index });
  if (count <= 1) return count === 1 ? [cell(0, 0, 1, 1, 0)] : [];
  if (count === 2) return [cell(0, 0, 0.5, 1, 0), cell(0.5, 0, 0.5, 1, 1)];
  if (count === 3) return [cell(0, 0, 2 / 3, 1, 0), cell(2 / 3, 0, 1 / 3, 0.5, 1), cell(2 / 3, 0.5, 1 / 3, 0.5, 2)];
  return [cell(0, 0, 0.5, 0.5, 0), cell(0.5, 0, 0.5, 0.5, 1), cell(0, 0.5, 0.5, 0.5, 2), cell(0.5, 0.5, 0.5, 0.5, 3)];
}

/** The views a state starts with when someone chooses N views: spread round the mount, looking at a useful height. */
export function defaultViews(mount: Mount, count: number): VirtualView[] {
  const n = Math.min(Math.max(count, 1), MAX_VIEWS);
  const tilt = mount === "ceiling" ? -45 : mount === "floor" ? 45 : 0;
  const fov = n === 1 ? 90 : 70;
  if (mount === "wall") {
    return Array.from({ length: n }, (_, index) => clampView(mount, { pan: n === 1 ? 0 : -60 + (120 * index) / (n - 1), tilt, fov }));
  }
  return Array.from({ length: n }, (_, index) => clampView(mount, { pan: -180 + (360 * index) / n, tilt, fov }));
}

export function stateFor(mount: Mount, choice: "raw" | "panorama" | "double_panorama" | 1 | 2 | 3 | 4): DewarpState {
  if (choice === "raw") return RAW;
  if (choice === "panorama" || choice === "double_panorama") return { mode: choice, pan: 0, views: [] };
  return { mode: "views", pan: 0, views: defaultViews(mount, choice) };
}

/** What the mode menu shows for a state. */
export function choiceOf(state: DewarpState): string {
  return state.mode === "views" ? String(state.views.length) : state.mode;
}

// --- moving a view -------------------------------------------------------------------------------------------------------------------

/** Dragging by (dx, dy), fractions of the view's width and height, grabs the picture: the view turns the opposite way. */
export function drag(mount: Mount, view: VirtualView, dx: number, dy: number, aspect: number): VirtualView {
  const horizontal = view.fov;
  const vertical = (Math.atan(Math.tan((view.fov * Math.PI) / 360) / aspect) * 360) / Math.PI;
  // Ceiling and floor: view-right is decreasing azimuth, so grabbing right turns towards larger azimuth. A wall mount is the other way round.
  const turn = mount === "wall" ? -1 : 1;
  return clampView(mount, { pan: view.pan + turn * dx * horizontal, tilt: view.tilt + dy * vertical, fov: view.fov });
}

/** The view after a step of zoom: positive zooms in (narrower field of view). */
export function zoom(mount: Mount, view: VirtualView, steps: number): VirtualView {
  return clampView(mount, { ...view, fov: view.fov * Math.pow(0.85, steps) });
}

export const KEY_STEP_DEGREES = 5;

/** The view after an arrow key (turn the way the arrow points) or + / − (zoom); null for any other key. */
export function key(mount: Mount, view: VirtualView, name: string, big = false): VirtualView | null {
  const step = KEY_STEP_DEGREES * (big ? 4 : 1);
  const turn = mount === "wall" ? 1 : -1; // Looking right increases the azimuth on a wall mount, decreases it on a ceiling or floor mount
  if (name === "ArrowLeft") return clampView(mount, { ...view, pan: view.pan - turn * step });
  if (name === "ArrowRight") return clampView(mount, { ...view, pan: view.pan + turn * step });
  if (name === "ArrowUp") return clampView(mount, { ...view, tilt: view.tilt + step });
  if (name === "ArrowDown") return clampView(mount, { ...view, tilt: view.tilt - step });
  if (name === "+" || name === "=") return zoom(mount, view, 1);
  if (name === "-" || name === "_") return zoom(mount, view, -1);
  return null;
}

/** A panorama scrolls: dragging right (dx, a fraction of its width) moves the picture right, so its left edge shows what was further left. A ceiling or floor mount wraps round; a wall mount stops at its sides. */
export function scrollPanorama(mount: Mount, mode: "panorama" | "double_panorama", pan: number, dx: number): number {
  const span = mode === "double_panorama" ? 180 : AZIMUTH_SPAN[mount];
  // The azimuth runs to the left on a ceiling or floor mount (pan grows as the picture is dragged right) and to the right on a wall mount.
  const next = mount === "wall" ? pan - dx * span : pan + dx * span;
  return mount === "wall" ? clamp(next, ...PAN_RANGE.wall) : wrap(next);
}

/** Keys scroll a panorama sideways by a few degrees. */
export function keyPanorama(mount: Mount, mode: "panorama" | "double_panorama", pan: number, name: string, big = false): number | null {
  const step = KEY_STEP_DEGREES * (big ? 4 : 1);
  if (name !== "ArrowLeft" && name !== "ArrowRight") return null;
  return scrollPanorama(mount, mode, pan, (name === "ArrowRight" ? -step : step) / (mode === "double_panorama" ? 180 : AZIMUTH_SPAN[mount]));
}

export function wrap(angle: number): number {
  const value = ((angle + 180) % 360 + 360) % 360 - 180;
  return value;
}
