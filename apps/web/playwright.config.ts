import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "../../e2e/playwright",
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:8000",
  },
  projects: [
    // The demo stack's dashboard. The agent panel needs its own stack with the agent graph.
    {
      name: "dashboard",
      testIgnore: /agent-panel\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    // Started by e2e/compose_agent_panel_proof.py.
    { name: "agent", testMatch: /agent-panel\.spec\.ts/, use: { ...devices["Desktop Chrome"] } },
  ],
});
