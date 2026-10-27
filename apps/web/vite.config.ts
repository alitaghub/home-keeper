import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Fail fast on variables from before the rename, with the same message as
// home_keeper_renamed: a leftover OBSERVER_API_ORIGIN would otherwise be ignored.
const oldPrefix = "OBSERVER_";
const renamed = Object.keys(process.env)
  .filter((name) => name.startsWith(oldPrefix) && process.env[name])
  .sort()
  .map((name) => `${name} was renamed to HOME_KEEPER_${name.slice(oldPrefix.length)}`);
if (renamed.length > 0) {
  throw new Error(`${renamed.join("; ")}. See docs/upgrade.md.`);
}

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
