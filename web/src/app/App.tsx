import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet, RouterProvider } from "react-router";
import { onSessionExpired } from "@/api/client";
import { keys, useMe, usePublicBranding, useSetupStatus } from "@/api/queries";
import type { Me } from "@/api/types";
import { EnrolmentRequired } from "@/features/auth/EnrolmentRequired";
import { SetupPage } from "@/features/auth/SetupPage";
import { SignInPage } from "@/features/auth/SignInPage";
import { AdministratorOnly, NotFound } from "@/features/placeholders/Pages";
import { LiveWorkspace } from "@/features/live/LiveWorkspace";
import { useLiveStore } from "@/features/live/store";
import { CamerasInventory, CamerasShell } from "@/features/admin/cameras/CamerasWorkspace";
import { useCamerasStore } from "@/features/admin/cameras/store";
import { usePlaybackStore } from "@/features/playback/store";
import { useUnmaskedStore } from "@/features/privacy/store";
import { Shell } from "@/features/shell/Shell";
import { lazyPage, Page } from "@/app/lazyPage";
import { applyTheme, reapplyBranding, storedTheme } from "@/lib/appearance";
import { deviceLabel } from "@/features/alarms/pushDevice";
import { isNativeApp } from "@/native/platform";
import { refreshNativePush, watchNotificationTaps } from "@/native/push";
import { rememberThisPhone } from "@/native/session";

// Everything except the first screen (Live) and the shell loads when it is first opened (#281).
const PlaybackWorkspace = lazyPage(() => import("@/features/playback/PlaybackWorkspace"), "PlaybackWorkspace");
const InsightsWorkspace = lazyPage(() => import("@/features/insights/InsightsWorkspace"), "InsightsWorkspace");
const DigestsList = lazyPage(() => import("@/features/digests/DigestsWorkspace"), "DigestsList");
const DigestView = lazyPage(() => import("@/features/digests/DigestsWorkspace"), "DigestView");
const CasesList = lazyPage(() => import("@/features/cases/CasesWorkspace"), "CasesList");
const CaseView = lazyPage(() => import("@/features/cases/CasesWorkspace"), "CaseView");
const FleetHealth = lazyPage(() => import("@/features/admin/cameras/FleetHealth"), "FleetHealth");
const MaintenanceWorkspace = lazyPage(() => import("@/features/maintenance/MaintenanceWorkspace"), "MaintenanceWorkspace");
const AddCameras = lazyPage(() => import("@/features/admin/cameras/add/AddCameras"), "AddCameras");
const GroupsWorkspace = lazyPage(() => import("@/features/admin/groups/GroupsWorkspace"), "GroupsWorkspace");
const RulesWorkspace = lazyPage(() => import("@/features/admin/rules/RulesWorkspace"), "RulesWorkspace");
const SettingsWorkspace = lazyPage(() => import("@/features/settings/SettingsWorkspace"), "SettingsWorkspace");

function AdminRoute({ me, children }: { me: Me; children: ReactNode }) {
  return me.role === "administrator" ? <>{children}</> : <AdministratorOnly />;
}

function router(me: Me) {
  return createBrowserRouter(
    [
      {
        path: "/",
        element: <Shell me={me} />,
        children: [
          { index: true, element: <Navigate to="/live" replace /> },
          { path: "live/*", element: <LiveWorkspace /> },
          { path: "playback/*", element: <Page><PlaybackWorkspace /></Page> },
          { path: "insights", element: <Page><InsightsWorkspace /></Page> },
          { path: "digests", element: <Page><DigestsList /></Page> },
          { path: "digests/:digestId", element: <Page><DigestView /></Page> },
          { path: "cases", element: <Page><CasesList /></Page> },
          { path: "cases/:caseId", element: <Page><CaseView /></Page> },
          {
            path: "cameras",
            element: <CamerasShell admin={me.role === "administrator"}><Outlet /></CamerasShell>,
            children: [
              { index: true, element: <CamerasInventory /> },
              { path: "health", element: <Page><FleetHealth /></Page> },
              { path: "maintenance", element: <Page><MaintenanceWorkspace /></Page> },
              { path: "add", element: <AdminRoute me={me}><Page><AddCameras /></Page></AdminRoute> },
              { path: "groups", element: <AdminRoute me={me}><Page><GroupsWorkspace /></Page></AdminRoute> },
              { path: "rules", element: <AdminRoute me={me}><Page><RulesWorkspace /></Page></AdminRoute> },
              { path: "*", element: <NotFound /> },
            ],
          },
          // One route for "/settings" too (it redirects to General inside the page), so going back to Settings from a category keeps the page and its open groups.
          { path: "settings/:category?", element: <AdminRoute me={me}><Page><SettingsWorkspace /></Page></AdminRoute> },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
    { basename: import.meta.env.BASE_URL.replace(/\/$/, "") },
  );
}

function SignedIn({ me }: { me: Me }) {
  // Workspace state belongs to one person: forget it on sign-out or a change of user. Only the
  // cleanup resets: React runs every cleanup before any new effect, whereas a reset in this
  // effect's body would run after the workspaces' own effects and erase what they just loaded.
  useEffect(() => () => {
    useLiveStore.getState().reset();
    usePlaybackStore.getState().reset();
    useCamerasStore.getState().reset();
    useUnmaskedStore.getState().reset();
  }, [me.username]);
  // In the native app: keep a device secret in secure storage, tell the NVR which push token is current, and open the alarm a tapped notification is about.
  useEffect(() => {
    if (!isNativeApp()) return;
    void rememberThisPhone(me.username, deviceLabel()).catch(() => undefined);
    void refreshNativePush();
    let stop: () => void = () => undefined;
    let gone = false;
    void watchNotificationTaps(path => window.location.assign(path)).then(remove => (gone ? remove() : (stop = remove)));
    return () => { gone = true; stop(); };
  }, [me.username]);
  // One router per signed-in identity, so a role change rebuilds the route table.
  const routes = useMemo(() => router(me), [me.username, me.role]); // eslint-disable-line react-hooks/exhaustive-deps
  return <RouterProvider router={routes} />;
}

/** Decide between first-run setup, sign-in and the application. */
export function App() {
  const client = useQueryClient();
  const setup = useSetupStatus();
  const me = useMe(setup.data?.setup_required === false);
  const branding = usePublicBranding();

  useEffect(() => applyTheme(storedTheme()), []);
  useEffect(() => {
    if (branding.data) reapplyBranding(branding.data);
  }, [branding.data]);
  useEffect(() => onSessionExpired(() => client.setQueryData(keys.me, null)), [client]);

  if (setup.isPending || (setup.data?.setup_required === false && me.isPending)) {
    return <div className="app-loading" role="status"><span className="spinner" /> <span>Loading…</span></div>;
  }
  if (setup.isError || me.isError) {
    return (
      <div className="app-loading" role="alert">
        <p>The NVR could not be reached.</p>
        <button className="button" onClick={() => client.invalidateQueries()}>Retry</button>
      </div>
    );
  }
  if (setup.data?.setup_required) return <SetupPage />;
  if (!me.data) return <SignInPage />;
  if (me.data.mfa_enrolment_required) return <EnrolmentRequired me={me.data} />;
  return <SignedIn me={me.data} />;
}
