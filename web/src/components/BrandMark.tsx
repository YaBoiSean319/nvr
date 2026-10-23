import type { Branding } from "@/api/types";

/** The site logo (light/dark variants when provided), falling back to a lettermark. */
export function BrandMark({ branding, size = 28 }: { branding: Branding | undefined; size?: number }) {
  const name = branding?.app_name ?? "Workplace NVR";
  const assets = branding?.assets ?? {};
  const light = assets.light?.url ?? assets.primary?.url;
  const dark = assets.dark?.url ?? assets.primary?.url;
  if (light || dark) {
    return (
      <picture className="brand-logo" style={{ height: size }}>
        {dark ? <source srcSet={dark} media="(prefers-color-scheme: dark)" /> : null}
        <img src={light ?? dark} alt="" height={size} />
      </picture>
    );
  }
  return (
    <span className="brand-letter" style={{ width: size, height: size }} aria-hidden="true">
      {name.trim().charAt(0).toUpperCase() || "N"}
    </span>
  );
}
