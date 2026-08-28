import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // host: true → listen on 0.0.0.0 so the dev server is reachable from the
  // LAN (e.g. http://192.168.2.177:5173), not just localhost.
  // usePolling: native macOS file events are unreliable on this machine — the
  // dev server kept serving stale modules after edits. Polling is slightly
  // heavier on CPU but guarantees changes are picked up.
  server: { host: true, port: 5173, watch: { usePolling: true, interval: 300 } },
});
