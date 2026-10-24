import { NavLink, Outlet, useLocation } from "react-router";
import { useEffect, useRef } from "react";
import { usePreferences, usePublicBranding } from "@/api/queries";
import type { Me } from "@/api/types";
import { BrandMark } from "@/components/BrandMark";
import { applyBranding, applyTheme, storedTheme } from "@/lib/appearance";
import { StatusArea } from "@/features/status/StatusArea";
import { UserMenu } from "./UserMenu";

const DESTINATIONS = [
  { to: "/live", label: "Live", admin: false },
  { to: "/playback", label: "Playback", admin: false },
  { to: "/cameras", label: "Cameras", admin: false },
  { to: "/settings", label: "Settings", admin: true },
];

export function Shell({ me }: { me: Me }) {
  const branding = usePublicBranding();
  const preferences = usePreferences(true);
  const location = useLocation();
  const theme = preferences.data?.theme ?? storedTheme();
  const workspace = DESTINATIONS.find(destination => location.pathname.startsWith(destination.to))?.label;

  const header = useRef<HTMLElement>(null);

  useEffect(() => applyTheme(theme), [theme]);
  // Overlays start below the header; on phones it wraps onto more rows, so publish its real height.
  useEffect(() => {
    const element = header.current;
    if (!element) return;
    const publish = () => document.documentElement.style.setProperty("--n-header-actual", `${Math.ceil(element.getBoundingClientRect().height)}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (branding.data) applyBranding(branding.data, workspace);
  }, [branding.data, workspace]);

  const destinations = DESTINATIONS.filter(destination => !destination.admin || me.role === "administrator");
  return (
    <div className="shell">
      <header className="shell-header" ref={header}>
        <a className="shell-brand" href="#main" aria-label="Skip to workspace">
          <BrandMark branding={branding.data} />
          <span className="shell-app-name">{branding.data?.app_name ?? "Workplace NVR"}</span>
        </a>
        <nav className="shell-nav" aria-label="Workspaces">
          {destinations.map(destination => (
            <NavLink key={destination.to} to={destination.to} className="shell-nav-link">
              {destination.label}
            </NavLink>
          ))}
        </nav>
        <StatusArea />
        <UserMenu me={me} theme={theme} />
      </header>
      <main id="main" className="shell-main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
