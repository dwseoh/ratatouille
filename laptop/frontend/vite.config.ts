import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import type { Plugin } from "vite";

// Python backend (laptop/coach/dashboard.py). Default port is DASHBOARD_PORT=8000.
const BACKEND = process.env.BACKEND_URL ?? "http://localhost:8000";
const paths = ["/state", "/stream", "/recipe-image", "/cook-qr.svg", "/info"];

// Two pages: index.html (desktop dashboard, served at /) and cook.html (phone page, served at /cook), same as the backend.
const cookRoute: Plugin = {
  name: "cook-route",
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      if (req.url === "/cook" || req.url?.startsWith("/cook?")) req.url = "/cook.html" + req.url.slice(5);
      next();
    });
  },
};

export default defineConfig({
  plugins: [react(), cookRoute],
  server: {
    proxy: Object.fromEntries(paths.map((p) => [p, BACKEND])),
  },
  build: {
    // The Python app serves the built files from laptop/coach/static/ (committed, so the demo laptop needs no Node).
    outDir: fileURLToPath(new URL("../coach/static", import.meta.url)),
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        cook: fileURLToPath(new URL("./cook.html", import.meta.url)),
      },
    },
  },
});
