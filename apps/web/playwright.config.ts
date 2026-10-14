import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "../../e2e/playwright",
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8000",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
