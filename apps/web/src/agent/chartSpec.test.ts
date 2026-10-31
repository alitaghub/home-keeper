// The panel's show_chart parser judges the same fixture specs as the agent's schema
// (apps/agent/tests/unit/test_chart_spec.py), so the two cannot drift apart unnoticed.
import { describe, expect, it } from "vitest";
import fixtureText from "../../../agent/tests/fixtures/chart_specs.json?raw";
import { parseChartSpec } from "./chartSpec";

type Case = { case: string; spec: unknown };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

// JSON cannot write NaN or infinity; the fixture marks them as {"$float": "NaN"}.
const revive = (_key: string, value: unknown) =>
  isRecord(value) && Object.keys(value).length === 1 && typeof value.$float === "string"
    ? Number(value.$float)
    : value;

const fixtures: { valid: Case[]; invalid: Case[] } = JSON.parse(fixtureText, revive);

describe("parseChartSpec", () => {
  it("reads the shared fixture file", () => {
    expect(fixtures.valid.length).toBeGreaterThan(0);
    expect(fixtures.invalid.length).toBeGreaterThan(0);
  });

  it.each(fixtures.valid.map((entry) => [entry.case, entry.spec] as const))(
    "accepts a valid spec: %s",
    (_name, spec) => {
      expect(parseChartSpec({ spec })).not.toBeNull();
    },
  );

  it.each(fixtures.invalid.map((entry) => [entry.case, entry.spec] as const))(
    "rejects an invalid spec: %s",
    (_name, spec) => {
      expect(parseChartSpec({ spec })).toBeNull();
    },
  );
});
