import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Bewusst relative Basis: das Frontend liegt unter beliebigen Pfaden im LXC.
// Dev-Proxy zeigt auf die FastAPI, damit `npm run dev` gegen echte Daten läuft.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8477",
      "/feed.xml": "http://127.0.0.1:8477",
    },
  },
});