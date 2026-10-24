/** WCAG contrast helpers used to keep branded accents legible in both themes. */

export function parseHex(hex: string): [number, number, number] | null {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match?.[1]) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function toHex([r, g, b]: [number, number, number]): string {
  return `#${[r, g, b].map(channel => Math.round(channel).toString(16).padStart(2, "0")).join("")}`;
}

function luminance([r, g, b]: [number, number, number]): number {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

export function contrast(a: string, b: string): number {
  const first = parseHex(a);
  const second = parseHex(b);
  if (!first || !second) return 1;
  const [light, dark] = [luminance(first), luminance(second)].sort((x, y) => y - x) as [number, number];
  return (light + 0.05) / (dark + 0.05);
}

function mix(color: [number, number, number], target: [number, number, number], amount: number): [number, number, number] {
  return color.map((channel, index) => channel + (target[index]! - channel) * amount) as [number, number, number];
}

/**
 * Nudge a colour toward black or white until it reaches the contrast ratio
 * against a background. Returns the original colour when it already passes.
 */
export function legibleOn(color: string, background: string, ratio: number): string {
  const parsed = parseHex(color);
  const backdrop = parseHex(background);
  if (!parsed || !backdrop) return color;
  if (contrast(color, background) >= ratio) return toHex(parsed);
  const target: [number, number, number] = luminance(backdrop) > 0.5 ? [0, 0, 0] : [255, 255, 255];
  for (let step = 1; step <= 20; step += 1) {
    const candidate = toHex(mix(parsed, target, step / 20));
    if (contrast(candidate, background) >= ratio) return candidate;
  }
  return toHex(target);
}

/** Black or white, whichever reads better on the colour. */
export function textOn(color: string): string {
  return contrast(color, "#ffffff") >= contrast(color, "#0b1220") ? "#ffffff" : "#0b1220";
}
