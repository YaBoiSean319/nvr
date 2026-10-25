import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet, RouterProvider } from "react-router";
import { onSessionExpired } from "@/api/client";
import { keys, useMe, usePublicBranding, useSetupStatus } from "@/api/queries";
import type { Me } from "@/api/types";
import { SetupPage } from "@/features/auth/SetupPage";
import { SignInPage } from "@/features/auth/SignInPage";
import { AdministratorOnly, NotFound } from "@/features/placeholders/Pages";
import { LiveWorkspace } from "@/features/live/LiveWorkspace";
import { useLiveStore } from "@/features/live/store";
import { CamerasInventory, CamerasShell } from "@/features/admin/cameras/CamerasWorkspace";
import { useCamerasStore } from "@/features/admin/cameras/store";
import { AddCameras } from "@/features/admin/cameras/add/AddCameras";
import { GroupsWorkspace } from "@/features/admin/groups/GroupsWorkspace";
import { RulesWorkspace } from "@/features/admin/rules/RulesWorkspace";
import { SettingsWorkspace } from "@/features/settings/SettingsWorkspace";
import { PlaybackWorkspace } from "@/features/playback/PlaybackWorkspace";
import { usePlaybackStore } from "@/features/playback/store";
import { useUnmaskedStore } from "@/features/privacy/store";
import { Shell } from "@/features/shell/Shell";
import { applyBranding, applyTheme, storedTheme } from "@/lib/appearance";

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
          { path: "playback/*", element: <PlaybackWorkspace /> },
          {
            path: "cameras",
            element: <CamerasShell admin={me.role === "administrator"}><Outlet /></CamerasShell>,
            children: [
              { index: true, element: <CamerasInventory /> },
              { path: "add", element: <AdminRoute me={me}><AddCameras /></AdminRoute> },
              { path: "groups", element: <AdminRoute me={me}><GroupsWorkspace /></AdminRoute> },
              { path: "rules", element: <AdminRoute me={me}><RulesWorkspace /></AdminRoute> },
              { path: "*", element: <NotFound /> },
            ],
          },
          { path: "settings", element: <AdminRoute me={me}><Navigate to="/settings/general" replace /></AdminRoute> },
          { path: "settings/:category", element: <AdminRoute me={me}><SettingsWorkspace /></AdminRoute> },
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
    if (branding.data) applyBranding(branding.data);
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
  return <SignedIn me={me.data} />;
}
