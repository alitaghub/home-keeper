import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { installFakeApi } from "./test/fake-api";
import { useRecentUsage } from "./useUsage";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("state changes while a usage report is pending do not start overlapping reports", async () => {
  const fake = installFakeApi();
  const release = fake.hold("GET /api/usage");
  const { rerender } = renderHook(({ key }) => useRecentUsage(key), {
    initialProps: { key: "brightness-100" },
  });
  // A dimming light changes the key on each 3 s poll while the slow report is still running.
  for (const key of ["brightness-90", "brightness-80", "brightness-70", "brightness-60"]) {
    rerender({ key });
  }
  await waitFor(() => expect(fake.callsTo("GET /api/usage").length).toBeGreaterThan(0));
  await new Promise((resolve) => setTimeout(resolve, 50));
  const inFlight = fake.callsTo("GET /api/usage").length;
  release();
  expect(inFlight).toBeLessThanOrEqual(2);
});
