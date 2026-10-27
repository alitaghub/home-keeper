import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The keeper rejects unknown Host headers and cross-origin mutations, so the
// dev proxy presents itself as the API origin.
const apiOrigin = process.env.HOME_KEEPER_API_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: apiOrigin,
        changeOrigin: true,
        headers: { origin: apiOrigin },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/test/**", "src/**/*.test.{ts,tsx}", "src/api/schema.d.ts"],
    },
  },
});
