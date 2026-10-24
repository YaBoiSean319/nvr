import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/app/App";
import { WallApp } from "@/features/wall/WallApp";
import "@/styles/tokens.css";
import "@/styles/base.css";
import "@/styles/app.css";

const client = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: true },
  },
});

// Video wall mode is its own page: no shell, no menus, signed in with a token.
const base = import.meta.env.BASE_URL.replace(/\/$/, "");
const isWall = window.location.pathname === `${base}/wall` || window.location.pathname.startsWith(`${base}/wall/`);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      {isWall ? <WallApp /> : <App />}
    </QueryClientProvider>
  </StrictMode>,
);

// Installable app: the worker serves the application shell only (see app/api/web_interface.py).
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => undefined));
}
