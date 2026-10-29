import { NavLink, Navigate, useParams } from "react-router";
import { DraftGuard } from "@/features/admin/drafts";
import { AccessSettings } from "./AccessSettings";
import { AuditSettings } from "./AuditSettings";
import { AppearanceSettings } from "./AppearanceSettings";
import { CoverageSettings } from "./CoverageSettings";
import { DigestsSettings } from "./DigestsSettings";
import { DiagnosticsSettings } from "./DiagnosticsSettings";
import { EventSourcesSettings } from "./EventSourcesSettings";
import { FailoverSettings } from "./FailoverSettings";
import { GeneralSettings } from "./GeneralSettings";
import { HardeningSettings } from "./HardeningSettings";
import { IntegrationsSettings } from "./IntegrationsSettings";
import { NotificationsSettings } from "./NotificationsSettings";
import { PlatesSettings } from "./PlatesSettings";
import { PrivacySettings } from "./PrivacySettings";
import { RemoteAccessSettings } from "./RemoteAccessSettings";
import { SitesSettings } from "./SitesSettings";
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
  coverage: { label: "Coverage & self-test", page: CoverageSettings },
  audit: { label: "Viewing audit", page: AuditSettings },
  hardening: { label: "Camera hardening", page: HardeningSettings },
  notifications: { label: "Notifications", page: NotificationsSettings },
  digests: { label: "Digests", page: DigestsSettings },
  events: { label: "Event sources", page: EventSourcesSettings },
  failover: { label: "Failover", page: FailoverSettings },
  plates: { label: "Licence plates", page: PlatesSettings },
  sites: { label: "Sites (console)", page: SitesSettings },
  integrations: { label: "Integrations", page: IntegrationsSettings },
  updates: { label: "Updates", page: UpdatesSettings },
  diagnostics: { label: "Diagnostics", page: DiagnosticsSettings },
} as const;

type Category = keyof typeof CATEGORIES;

/** Every administrative category, one page at a time, with explicit saves. */
export function SettingsWorkspace() {
  const { category } = useParams();
  if (!category || !(category in CATEGORIES)) return <Navigate to="/settings/general" replace />;
  const Page = CATEGORIES[category as Category].page;
  return (
    <div className="settings-workspace">
      <nav className="settings-nav" aria-label="Settings categories">
        {(Object.keys(CATEGORIES) as Category[]).map(key => (
          <NavLink key={key} to={`/settings/${key}`} className="section-link">{CATEGORIES[key].label}</NavLink>
        ))}
      </nav>
      <section className="settings-page" aria-label={CATEGORIES[category as Category].label}>
        <h1 className="settings-title">{CATEGORIES[category as Category].label}</h1>
        <Page />
      </section>
      <DraftGuard />
    </div>
  );
}
