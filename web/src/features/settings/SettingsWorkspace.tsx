import { useEffect, useState } from "react";
import { NavLink, Navigate, useNavigate, useParams } from "react-router";
import { DraftGuard } from "@/features/admin/drafts";
import { AccessSettings } from "./AccessSettings";
import { AuditSettings } from "./AuditSettings";
import { AppearanceSettings } from "./AppearanceSettings";
import { CoverageSettings } from "./CoverageSettings";
import { DigestsSettings } from "./DigestsSettings";
import { DiagnosticsSettings } from "./DiagnosticsSettings";
import { EmailSettings } from "./EmailSettings";
import { EventSourcesSettings } from "./EventSourcesSettings";
import { FailoverSettings } from "./FailoverSettings";
import { GeneralSettings } from "./GeneralSettings";
import { HardeningSettings } from "./HardeningSettings";
import { IntegrationsSettings } from "./IntegrationsSettings";
import { NotificationsSettings } from "./NotificationsSettings";
import { PlatesSettings } from "./PlatesSettings";
import { FallsSettings } from "./FallsSettings";
import { PrivacySettings } from "./PrivacySettings";
import { RemoteAccessSettings } from "./RemoteAccessSettings";
import { SitesSettings } from "./SitesSettings";
import { CapacitySettings } from "./CapacitySettings";
import { StorageSettings } from "./StorageSettings";
import { TieringSettings } from "./TieringSettings";
import { UpdatesSettings } from "./UpdatesSettings";

const CATEGORIES = {
  general: { label: "General", page: GeneralSettings },
  appearance: { label: "Appearance & branding", page: AppearanceSettings },
  access: { label: "Users & access", page: AccessSettings },
  remote: { label: "Remote access", page: RemoteAccessSettings },
  privacy: { label: "Privacy & notices", page: PrivacySettings },
  recording: { label: "Recording & storage", page: StorageSettings },
  tiering: { label: "Storage tiering", page: TieringSettings },
  capacity: { label: "Capacity planner", page: CapacitySettings },
  coverage: { label: "Coverage & self-test", page: CoverageSettings },
  audit: { label: "Viewing audit", page: AuditSettings },
  hardening: { label: "Camera hardening", page: HardeningSettings },
  notifications: { label: "Notifications", page: NotificationsSettings },
  email: { label: "Email alerts", page: EmailSettings },
  digests: { label: "Digests", page: DigestsSettings },
  events: { label: "Event sources", page: EventSourcesSettings },
  failover: { label: "Failover", page: FailoverSettings },
  plates: { label: "Licence plates", page: PlatesSettings },
  falls: { label: "Fall detection", page: FallsSettings },
  sites: { label: "Sites (console)", page: SitesSettings },
  integrations: { label: "AI analysis", page: IntegrationsSettings },
  updates: { label: "Updates", page: UpdatesSettings },
  diagnostics: { label: "Diagnostics", page: DiagnosticsSettings },
} as const;

type Category = keyof typeof CATEGORIES;

/** The categories in the order they are listed, under a few headings (every category is in exactly one group). The addresses do not change. */
const GROUPS: { label: string; items: Category[] }[] = [
  { label: "Site", items: ["general", "appearance", "privacy"] },
  { label: "People & access", items: ["access", "remote", "audit", "hardening"] },
  { label: "Recording & storage", items: ["recording", "tiering", "capacity", "coverage", "failover"] },
  { label: "Alerts", items: ["notifications", "email", "digests", "events"] },
  { label: "AI", items: ["integrations", "plates", "falls"] },
  { label: "System", items: ["sites", "updates", "diagnostics"] },
];

/** Other words someone might type to find a category, besides its name. */
const KEYWORDS: Record<Category, string> = {
  general: "date time zone clock format display live video start-up ready stream wait keep open",
  appearance: "branding logo colour color theme name",
  privacy: "mask notices consent",
  access: "users passwords roles tokens passkeys two factor grants api",
  remote: "internet tunnel vpn outside",
  audit: "viewing log who watched",
  hardening: "camera passwords security checks firmware",
  recording: "storage disks retention volumes cameras space",
  tiering: "cold archive s3 move old footage",
  capacity: "planner sizing cameras cpu memory",
  coverage: "gaps self test missing footage",
  failover: "standby backup recorder",
  notifications: "push web hooks webhook slack teams phone",
  email: "microsoft mail smtp alerts",
  digests: "summary daily report",
  events: "external mqtt sources",
  integrations: "ai model device cameras analysed cap confidence detection people vehicles",
  plates: "licence license plate number region",
  falls: "fall detection pose sensitivity",
  sites: "console multi site remote nvr",
  updates: "version upgrade release",
  diagnostics: "support bundle logs capture",
};

const groupOf = (category: Category) => GROUPS.find(group => group.items.includes(category))?.label ?? GROUPS[0]!.label;
const ALL = GROUPS.flatMap(group => group.items);

function matches(query: string): Category[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return ALL.filter(key => {
    const text = `${CATEGORIES[key].label} ${KEYWORDS[key]} ${groupOf(key)}`.toLowerCase();
    return words.every(word => text.includes(word));
  });
}

/** Every administrative category, one page at a time, with explicit saves. */
export function SettingsWorkspace() {
  const { category } = useParams();
  const navigate = useNavigate();
  const current = category && category in CATEGORIES ? (category as Category) : null;
  const [open, setOpen] = useState<Set<string>>(() => new Set(current ? [groupOf(current)] : []));
  const [query, setQuery] = useState("");
  // Arriving at a category (by link, search or the picker) opens its group.
  useEffect(() => {
    if (current) setOpen(groups => (groups.has(groupOf(current)) ? groups : new Set(groups).add(groupOf(current))));
  }, [current]);
  if (!current) return <Navigate to="/settings/general" replace />;
  const Page = CATEGORIES[current].page;
  const found = query.trim() ? matches(query) : null;
  const link = (key: Category) => <NavLink key={key} to={`/settings/${key}`} className="section-link">{CATEGORIES[key].label}</NavLink>;
  return (
    <div className="settings-workspace">
      <div className="settings-side">
        <input type="search" className="settings-find" aria-label="Find a setting" placeholder="Find a setting…" value={query}
               onChange={event => setQuery(event.target.value)}
               onKeyDown={event => { if (event.key === "Enter" && found?.length) { navigate(`/settings/${found[0]}`); setQuery(""); } else if (event.key === "Escape") setQuery(""); }} />
        <nav className="settings-nav" aria-label="Settings categories">
          {found ? (
            found.length ? found.map(link) : <p className="muted settings-none" role="status">No setting matches “{query.trim()}”.</p>
          ) : GROUPS.map(group => (
            <details key={group.label} className="settings-group" open={open.has(group.label)}
                     onToggle={event => { const isOpen = event.currentTarget.open; setOpen(groups => { const next = new Set(groups); if (isOpen) next.add(group.label); else next.delete(group.label); return next; }); }}>
              <summary>{group.label}</summary>
              {group.items.map(link)}
            </details>
          ))}
        </nav>
        <select className="settings-picker" aria-label="Settings category" value={current} onChange={event => navigate(`/settings/${event.target.value}`)}>
          {GROUPS.map(group => (
            <optgroup key={group.label} label={group.label}>
              {group.items.map(key => <option key={key} value={key}>{CATEGORIES[key].label}</option>)}
            </optgroup>
          ))}
        </select>
      </div>
      <section className="settings-page" aria-label={CATEGORIES[current].label}>
        <h1 className="settings-title">{CATEGORIES[current].label}</h1>
        <Page />
      </section>
      <DraftGuard />
    </div>
  );
}
