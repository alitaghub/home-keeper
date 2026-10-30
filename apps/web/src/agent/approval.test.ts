import { describe, expect, it } from "vitest";
import { describeAction, type ProposedAction, parseApproval, resumeFor } from "./approval";

// The interrupt value HumanInTheLoopMiddleware raises for the agent's approval tools
// (apps/agent/src/home_keeper_agent/graph.py), the shape the Telegram bot parses too.
const request = (
  ...actions: { name: string; args: Record<string, unknown>; description?: string }[]
) => ({
  action_requests: actions.map((action) => ({
    description: `Tool execution requires approval\n\nTool: ${action.name}`,
    ...action,
  })),
  review_configs: actions.map((action) => ({
    action_name: action.name,
    allowed_decisions: ["approve", "reject"],
  })),
});

describe("parseApproval", () => {
  it("reads each proposed action in the interrupt's order", () => {
    const value = request(
      { name: "monitor", args: { entity_id: "light.hue_desk" } },
      { name: "stop_monitoring", args: { entity_id: "switch.kettle" } },
      { name: "turn_off", args: { entity_id: "light.hue_reading" } },
      { name: "turn_on", args: { entity_id: "light.hue_desk", brightness_percent: 30 } },
      // The model may pass an explicit null for "keep the light's own brightness".
      { name: "turn_on", args: { entity_id: "switch.kettle", brightness_percent: null } },
    );

    expect(parseApproval(value)).toEqual<ProposedAction[]>([
      { name: "monitor", entityId: "light.hue_desk" },
      { name: "stop_monitoring", entityId: "switch.kettle" },
      { name: "turn_off", entityId: "light.hue_reading" },
      { name: "turn_on", entityId: "light.hue_desk", brightnessPercent: 30 },
      { name: "turn_on", entityId: "switch.kettle" },
    ]);
  });

  const monitorDesk = request({ name: "monitor", args: { entity_id: "light.hue_desk" } });

  it.each<[string, unknown]>([
    ["nothing", undefined],
    ["null", null],
    ["a string", "approve?"],
    ["an empty object", {}],
    ["no actions", { action_requests: [], review_configs: [] }],
    ["a tool the agent cannot get approved", request({ name: "execute", args: { command: "ls" } })],
    ["an action without an entity", request({ name: "monitor", args: {} })],
    ["a non-text entity", request({ name: "turn_off", args: { entity_id: 7 } })],
    [
      "a non-number brightness",
      request({ name: "turn_on", args: { entity_id: "light.a", brightness_percent: "30" } }),
    ],
    // Only turn_on takes a brightness; elsewhere it would be accepted and never shown.
    [
      "a brightness on monitor",
      request({ name: "monitor", args: { entity_id: "light.a", brightness_percent: 30 } }),
    ],
    [
      "a brightness on turn_off",
      request({ name: "turn_off", args: { entity_id: "light.a", brightness_percent: 30 } }),
    ],
    ["an action without arguments", { ...monitorDesk, action_requests: [{ name: "monitor" }] }],
    ["fewer review configs than actions", { ...monitorDesk, review_configs: [] }],
    [
      "a review that cannot reject",
      {
        ...monitorDesk,
        review_configs: [{ action_name: "monitor", allowed_decisions: ["approve"] }],
      },
    ],
  ])("rejects %s", (_, value) => {
    expect(parseApproval(value)).toBeNull();
  });
});

describe("resumeFor", () => {
  it("sends one decision per action in the given order", () => {
    expect(resumeFor(["approve", "reject"])).toEqual({
      decisions: [{ type: "approve" }, { type: "reject" }],
    });
  });
});

describe("describeAction", () => {
  it.each<[ProposedAction, string | undefined, string]>([
    [{ name: "monitor", entityId: "light.hue_desk" }, "Desk lamp", "Start monitoring Desk lamp"],
    [
      { name: "stop_monitoring", entityId: "light.hue_desk" },
      "Desk lamp",
      "Stop monitoring Desk lamp",
    ],
    [{ name: "turn_off", entityId: "light.hue_desk" }, "Desk lamp", "Turn off Desk lamp"],
    [{ name: "turn_on", entityId: "light.hue_desk" }, "Desk lamp", "Turn on Desk lamp"],
    [
      { name: "turn_on", entityId: "light.hue_desk", brightnessPercent: 30 },
      "Desk lamp",
      "Turn on Desk lamp at 30%",
    ],
    [{ name: "turn_off", entityId: "light.hue_desk" }, undefined, "Turn off light.hue_desk"],
  ])("describes %j named %s as %s", (action, name, text) => {
    expect(describeAction(action, name)).toBe(text);
  });
});
