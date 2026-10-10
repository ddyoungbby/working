import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Mounts POST /api/classify so the Anthropic call runs server-side
// (key stays out of the browser bundle). Dev-only; `vite build` is static.
function classifyApi() {
  return {
    name: "classify-api",
    configureServer(server) {
      server.middlewares.use("/api/classify", async (req, res) => {
        try {
          const { classifyHandler } = await import("./server/classify.mjs");
          await classifyHandler(req, res);
        } catch (e) {
          res.statusCode = 500;
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify({ error: "Server error: " + String(e?.message || e) }));
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), classifyApi()],
  server: {
    host: true,
    port: 5173,
  },
});
