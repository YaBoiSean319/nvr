import { AZIMUTH_SPAN, ELEVATION_RANGE, MAX_VIEW_FOV, MIN_VIEW_FOV, PAN_RANGE, TILT_RANGE, cameraAxes, viewMatrix, type DewarpState, type Lens, type Region } from "./geometry";

/**
 * The WebGL dewarper (#146). One shader draws every region of a tile: for each output pixel it makes the ray the mode says (a virtual view's pinhole ray, or a
 * panorama's azimuth and elevation), turns it into the camera frame, and finds where an equidistant lens put it. The maths is `app/dewarp.py`'s, line for line,
 * so an export of a view matches what was on screen.
 */

const VERTEX = `
attribute vec2 a_position;
void main() { gl_Position = vec4(a_position, 0.0, 1.0); }
`;

const FRAGMENT = `
precision highp float;
uniform sampler2D u_tex;
uniform vec2 u_size;
uniform vec2 u_origin;
uniform int u_mode;
uniform mat3 u_view;
uniform mat3 u_axes;
uniform float u_half;
uniform float u_pan;
uniform vec2 u_elev;
uniform float u_span;
uniform int u_wall;
uniform float u_sign;
uniform vec2 u_center;
uniform float u_radius;
uniform float u_aspect;
uniform float u_lens_half;

const float PI = 3.14159265358979;

vec3 direction(float az, float el) {
  float a = az * PI / 180.0;
  float e = el * PI / 180.0;
  if (u_wall == 1) return vec3(cos(e) * sin(a), cos(e) * cos(a), sin(e));
  return vec3(cos(e) * cos(a), cos(e) * sin(a), sin(e));
}

void main() {
  vec2 p = (gl_FragCoord.xy - u_origin) / u_size;
  float x = p.x * 2.0 - 1.0;
  float t = 1.0 - p.y;
  vec3 ray;
  if (u_mode == 0) {
    float y = 1.0 - 2.0 * t;
    ray = u_view * normalize(vec3(x * u_half, y * u_half * u_size.y / u_size.x, 1.0));
  } else {
    float az;
    float el;
    if (u_mode == 1) {
      az = u_pan + u_sign * x * u_span * 0.5;
      el = mix(u_elev.x, u_elev.y, t);
    } else {
      float strip = t < 0.5 ? 0.0 : 1.0;
      float within = t < 0.5 ? t * 2.0 : t * 2.0 - 1.0;
      az = u_pan + u_sign * (strip * 180.0 + (x + 1.0) * 0.5 * 180.0);
      el = mix(u_elev.x, u_elev.y, within);
    }
    ray = u_axes * direction(az, el);
  }
  float theta = acos(clamp(ray.z, -1.0, 1.0));
  float r = theta / u_lens_half;
  if (r > 1.0) {
    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }
  float psi = atan(ray.y, ray.x);
  vec2 uv = vec2(u_center.x + r * u_radius * cos(psi) / u_aspect, u_center.y - r * u_radius * sin(psi));
  gl_FragColor = vec4(texture2D(u_tex, uv).rgb, 1.0);
}
`;

export type Source = HTMLVideoElement | HTMLImageElement | HTMLCanvasElement;

/** One thing to draw: a region of the tile and what it shows. */
export interface Drawing { region: Region; mode: "view" | "panorama" | "double_panorama"; pan: number; tilt: number; fov: number }

function columnMajor(rowMajor: number[]): Float32Array {
  const out = new Float32Array(9);
  for (let row = 0; row < 3; row += 1) for (let column = 0; column < 3; column += 1) out[column * 3 + row] = rowMajor[row * 3 + column]!;
  return out;
}

export function sourceSize(source: Source): { width: number; height: number } {
  if (source instanceof HTMLVideoElement) return { width: source.videoWidth, height: source.videoHeight };
  if (source instanceof HTMLImageElement) return { width: source.naturalWidth, height: source.naturalHeight };
  return { width: source.width, height: source.height };
}

export class DewarpRenderer {
  private readonly program: WebGLProgram;
  private readonly texture: WebGLTexture;
  private readonly uniforms: Record<string, WebGLUniformLocation | null> = {};

  private constructor(private readonly canvas: HTMLCanvasElement, private readonly gl: WebGLRenderingContext) {
    const compile = (type: number, text: string) => {
      const shader = gl.createShader(type)!;
      gl.shaderSource(shader, text);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) ?? "shader failed");
      return shader;
    };
    this.program = gl.createProgram()!;
    gl.attachShader(this.program, compile(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(this.program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(this.program);
    if (!gl.getProgramParameter(this.program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(this.program) ?? "program failed");
    gl.useProgram(this.program);
    for (const name of ["u_tex", "u_size", "u_origin", "u_mode", "u_view", "u_axes", "u_half", "u_pan", "u_elev", "u_span", "u_wall", "u_sign", "u_center", "u_radius", "u_aspect", "u_lens_half"]) {
      this.uniforms[name] = gl.getUniformLocation(this.program, name);
    }
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(this.program, "a_position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    this.texture = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.uniform1i(this.uniforms.u_tex!, 0);
  }

  /** A renderer for a canvas, or null when the browser has no WebGL. */
  static create(canvas: HTMLCanvasElement): DewarpRenderer | null {
    try {
      const gl = canvas.getContext("webgl", { antialias: false, preserveDrawingBuffer: true, alpha: false }) as WebGLRenderingContext | null;
      return gl ? new DewarpRenderer(canvas, gl) : null;
    } catch {
      return null;
    }
  }

  draw(source: Source, lens: Lens, drawings: Drawing[]): void {
    const { gl, canvas } = this;
    const size = sourceSize(source);
    if (!size.width || !size.height) return;
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, 0);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.disable(gl.SCISSOR_TEST);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.SCISSOR_TEST);
    const u = this.uniforms;
    gl.uniform2f(u.u_center!, lens.center_x, lens.center_y);
    gl.uniform1f(u.u_radius!, lens.radius);
    gl.uniform1f(u.u_aspect!, size.width / size.height);
    gl.uniform1f(u.u_lens_half!, ((lens.fov / 2) * Math.PI) / 180);
    gl.uniform1i(u.u_wall!, lens.mount === "wall" ? 1 : 0);
    gl.uniform1f(u.u_sign!, lens.mount === "wall" ? 1 : -1);
    gl.uniformMatrix3fv(u.u_axes!, false, columnMajor(cameraAxes(lens.mount)));
    gl.uniform2f(u.u_elev!, ...ELEVATION_RANGE[lens.mount]);
    for (const drawing of drawings) {
      const { region } = drawing;
      const x = Math.round(region.x * canvas.width);
      const width = Math.max(1, Math.round((region.x + region.width) * canvas.width) - x);
      const top = Math.round(region.y * canvas.height);
      const height = Math.max(1, Math.round((region.y + region.height) * canvas.height) - top);
      const y = canvas.height - top - height;
      gl.viewport(x, y, width, height);
      gl.scissor(x, y, width, height);
      gl.uniform2f(u.u_size!, width, height);
      gl.uniform2f(u.u_origin!, x, y);
      if (drawing.mode === "view") {
        const fov = Math.min(Math.max(drawing.fov, MIN_VIEW_FOV), MAX_VIEW_FOV);
        const pan = Math.min(Math.max(drawing.pan, PAN_RANGE[lens.mount][0]), PAN_RANGE[lens.mount][1]);
        const tilt = Math.min(Math.max(drawing.tilt, TILT_RANGE[lens.mount][0]), TILT_RANGE[lens.mount][1]);
        gl.uniform1i(u.u_mode!, 0);
        gl.uniformMatrix3fv(u.u_view!, false, columnMajor(viewMatrix(lens.mount, pan, tilt)));
        gl.uniform1f(u.u_half!, Math.tan((fov * Math.PI) / 360));
      } else {
        gl.uniform1i(u.u_mode!, drawing.mode === "panorama" ? 1 : 2);
        gl.uniform1f(u.u_pan!, Math.min(Math.max(drawing.pan, PAN_RANGE[lens.mount][0]), PAN_RANGE[lens.mount][1]));
        gl.uniform1f(u.u_span!, AZIMUTH_SPAN[lens.mount]);
      }
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  }

  dispose(): void {
    const { gl } = this;
    // The context itself is left to go with its canvas: a context that was lost cannot be had again from the same canvas.
    gl.deleteTexture(this.texture);
    gl.deleteProgram(this.program);
  }
}

/** What to draw for a state: each region with its view's pan, tilt and field of view, or the panorama's start. */
export function drawingsFor(state: DewarpState, regionList: Region[]): Drawing[] {
  return regionList.map(region => {
    if (region.kind === "view") {
      const view = state.views[region.index]!;
      return { region, mode: "view" as const, pan: view.pan, tilt: view.tilt, fov: view.fov };
    }
    return { region, mode: region.kind, pan: state.pan, tilt: 0, fov: 0 };
  });
}
