import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, type ReactNode } from "react";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";
import { onSessionExpired } from "@/api/client";
import { keys, useMe, usePublicBranding, useSetupStatus } from "@/api/queries";
import type { Me } from "@/api/types";
import { SetupPage } from "@/features/auth/SetupPage";
import { SignInPage } from "@/features/auth/SignInPage";
import { AdministratorOnly, NotFound, WorkspacePlaceholder } from "@/features/placeholders/Pages";
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
          { path: "live/*", element: <WorkspacePlaceholder title="Live" legacyHint="Live viewing is available in the current interface until this workspace is complete." /> },
          { path: "playback/*", element: <WorkspacePlaceholder title="Playback" legacyHint="Footage search and export are available in the current interface." /> },
          { path: "cameras/*", element: <AdminRoute me={me}><WorkspacePlaceholder title="Cameras" legacyHint="Camera administration is available in the current interface." /></AdminRoute> },
          { path: "settings/*", element: <AdminRoute me={me}><WorkspacePlaceholder title="Settings" legacyHint="Settings are available in the current interface." /></AdminRoute> },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
    { basename: import.meta.env.BASE_URL.replace(/\/$/, "") },
  );
}

function SignedIn({ me }: { me: Me }) {
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
