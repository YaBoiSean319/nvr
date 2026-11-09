import { lazy, Suspense, type ComponentType, type LazyExoticComponent, type ReactNode } from "react";

const RELOADED = "nvr-chunk-reload";

/**
 * A page that loads when it is first opened, so the first screen does not download every workspace (#281).
 *
 * After an upgrade, a tab that was open before it asks for chunk files that no longer exist. The first failure reloads the page once, which
 * brings the new release; a second failure in the same tab is shown as the error it is, instead of a reload loop or a blank page.
 */
export function lazyPage<Module, Name extends keyof Module>(load: () => Promise<Module>, name: Name): LazyExoticComponent<Extract<Module[Name], ComponentType<any>>> { // eslint-disable-line @typescript-eslint/no-explicit-any
  return lazy(() => load().then(
    module => {
      try { sessionStorage.removeItem(RELOADED); } catch { /* no storage: the guard below simply cannot remember */ }
      return { default: module[name] as Extract<Module[Name], ComponentType<any>> }; // eslint-disable-line @typescript-eslint/no-explicit-any
    },
    (error: unknown) => {
      try {
        if (sessionStorage.getItem(RELOADED) === null) {
          sessionStorage.setItem(RELOADED, "1");
          window.location.reload();
          return new Promise<never>(() => undefined); // The page is going away: render nothing meanwhile
        }
      } catch { /* fall through to the error */ }
      throw error;
    },
  ));
}

export function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<p className="muted" role="status" aria-live="polite">Loading…</p>}>{children}</Suspense>;
}
