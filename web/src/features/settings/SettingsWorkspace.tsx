import { NavLink, Navigate, useParams } from "react-router";
import { DraftGuard } from "@/features/admin/drafts";
import { AccessSettings } from "./AccessSettings";
import { AuditSettings } from "./AuditSettings";
import { AppearanceSettings } from "./AppearanceSettings";
import { CoverageSettings } from "./CoverageSettings";
import { DiagnosticsSettings } from "./DiagnosticsSettings";
import { GeneralSettings } from "./GeneralSettings";
import { IntegrationsSettings } from "./IntegrationsSettings";
import { NotificationsSettings } from "./NotificationsSettings";
import { StorageSettings } from "./StorageSettings";

const CATEGORIES = {
  general: { label: "General", page: GeneralSettings },
  appearance: { label: "Appearance & branding", page: AppearanceSettings },
  access: { label: "Users & access", page: AccessSettings },
  recording: { label: "Recording & storage", page: StorageSettings },
  coverage: { label: "Coverage & self-test", page: CoverageSettings },
  audit: { label: "Viewing audit", page: AuditSettings },
  notifications: { label: "Notifications", page: NotificationsSettings },
  integrations: { label: "Integrations", page: IntegrationsSettings },
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
