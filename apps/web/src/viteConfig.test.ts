import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

it("refuses to start the dev server while an old OBSERVER_ variable is set", async () => {
  vi.stubEnv("OBSERVER_API_ORIGIN", "http://127.0.0.1:9000");
  vi.resetModules();

  await expect(import("../vite.config")).rejects.toThrow(
    "OBSERVER_API_ORIGIN was renamed to HOME_KEEPER_API_ORIGIN. See docs/upgrade.md.",
  );
});

it("loads when only the new variables are set", async () => {
  vi.stubEnv("HOME_KEEPER_API_ORIGIN", "http://127.0.0.1:9000");
  vi.resetModules();

  await expect(import("../vite.config")).resolves.toHaveProperty("default");
});
