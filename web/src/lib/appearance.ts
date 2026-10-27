import type { Branding, ThemePreference } from "@/api/types";
import { legibleOn, textOn } from "./color";

const THEME_KEY = "nvr-theme";
// The neutral surface in each theme with the least contrast against an accent (see tokens.css):
// the raised grey in light, the raised charcoal in dark. Passing there passes on the page and panels too.
const LIGHT_PANEL = "#f0f2f5";
const DARK_PANEL = "#22262e";

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

// The workspace the shell last put in the title. Branding can arrive after the shell has mounted; applying it again without this would drop the workspace.
let shownWorkspace: string | undefined;

/** Apply site branding again (it was fetched or changed) keeping the workspace the shell is showing in the title. */
export function reapplyBranding(branding: Branding): void {
  applyBranding(branding, shownWorkspace);
}

/** The shell is gone (signed out): the title is the plain name again. */
export function forgetWorkspace(): void {
  shownWorkspace = undefined;
}

/** Apply site branding: name in the title, a legible accent in both themes, and the icon. */
export function applyBranding(branding: Branding, workspace?: string): void {
  shownWorkspace = workspace;
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
  // The installed app follows branding: a new manifest URL makes the browser fetch it again.
  const manifest = document.querySelector<HTMLLinkElement>("link[data-brand-manifest]");
  if (manifest) manifest.href = `/manifest.webmanifest?v=${encodeURIComponent(branding.updated_at ?? "default")}`;
  document.querySelector<HTMLMetaElement>("meta[data-brand-theme]")?.setAttribute("content", branding.primary_color);
}
