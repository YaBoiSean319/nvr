import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served by FastAPI at /next until the cutover replaces the legacy interface.
export default defineConfig({
  base: "/next/",
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: fileURLToPath(new URL("../app/web_dist", import.meta.url)),
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8280", ws: true },
      "/healthz": "http://127.0.0.1:8280",
    },
  },
});
