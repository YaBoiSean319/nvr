import type { Branding, ThemePreference } from "@/api/types";
import { legibleOn, textOn } from "./color";

const THEME_KEY = "nvr-theme";
// The neutral panel colours each accent must stay legible on (see tokens.css).
const LIGHT_PANEL = "#ffffff";
const DARK_PANEL = "#171a20";

export function storedTheme(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: ThemePreference): void {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage may be unavailable; the server-side preference still applies after sign-in.
  }
}

/** Apply site branding: name in the title, a legible accent in both themes, and the icon. */
export function applyBranding(branding: Branding, workspace?: string): void {
  const root = document.documentElement.style;
  const lightAccent = legibleOn(branding.accent_color, LIGHT_PANEL, 4.5);
  const darkAccent = legibleOn(branding.accent_color, DARK_PANEL, 4.5);
  root.setProperty("--n-accent-light", lightAccent);
  root.setProperty("--n-accent-dark", darkAccent);
  root.setProperty("--n-on-accent-light", textOn(lightAccent));
  root.setProperty("--n-on-accent-dark", textOn(darkAccent));
  root.setProperty("--n-brand-primary", branding.primary_color);
  document.title = workspace ? `${workspace} · ${branding.app_name}` : branding.app_name;
  const icon = document.querySelector<HTMLLinkElement>("link[data-brand-icon]");
  if (icon) icon.href = branding.assets.icon?.url ?? "data:,";
}
