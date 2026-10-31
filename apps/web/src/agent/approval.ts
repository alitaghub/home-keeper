// Before a device change the agent raises a HumanInTheLoopMiddleware interrupt that lists
// the proposed tool calls and waits for one decision per call.

export type Decision = "approve" | "reject";

const ACTIONS = ["monitor", "stop_monitoring", "turn_on", "turn_off"] as const;
type ActionName = (typeof ACTIONS)[number];

// Only turning a light on takes a brightness.
export type ProposedAction =
  | { name: Exclude<ActionName, "turn_on">; entityId: string }
  | { name: "turn_on"; entityId: string; brightnessPercent?: number };

/** An approval interrupt: its actions, or how many it holds when one cannot be read. */
export type Approval =
  | { kind: "actions"; actions: ProposedAction[] }
  | { kind: "unreadable"; count: number };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isAction = (name: unknown): name is ActionName => ACTIONS.some((action) => action === name);

function parseAction(request: unknown): ProposedAction | null {
  if (!isRecord(request) || !isAction(request.name) || !isRecord(request.args)) return null;
  const { entity_id: entityId, brightness_percent: brightness } = request.args;
  if (typeof entityId !== "string" || !entityId) return null;
  // The model may send null for "keep the light's own brightness".
  if (brightness == null) return { name: request.name, entityId };
  if (request.name !== "turn_on" || typeof brightness !== "number") return null;
  return { name: request.name, entityId, brightnessPercent: brightness };
}

// Each review must name its action and allow both answers, as the Telegram bot requires.
const isReviewOf = (review: unknown, action: ProposedAction) =>
  isRecord(review) &&
  review.action_name === action.name &&
  Array.isArray(review.allowed_decisions) &&
  review.allowed_decisions.includes("approve") &&
  review.allowed_decisions.includes("reject");

/** An approval interrupt's proposed actions, or null for any other interrupt value. */
export function parseApproval(value: unknown): Approval | null {
  if (!isRecord(value)) return null;
  const { action_requests: requests, review_configs: reviews } = value;
  if (!Array.isArray(requests) || requests.length === 0) return null;
  // The run waits for one decision per request, so even an unreadable one can be rejected.
  const unreadable: Approval = { kind: "unreadable", count: requests.length };
  if (!Array.isArray(reviews) || reviews.length !== requests.length) return unreadable;
  const actions: ProposedAction[] = [];
  for (const [index, request] of requests.entries()) {
    const action = parseAction(request);
    if (!action || !isReviewOf(reviews[index], action)) return unreadable;
    actions.push(action);
  }
  return { kind: "actions", actions };
}

/** The resume value for an approval interrupt: one decision per action, in its order. */
export const resumeFor = (decisions: readonly Decision[]) => ({
  decisions: decisions.map((type) => ({ type })),
});

/** What an action does, naming the device by its dashboard name, or its id when unknown. */
export function describeAction(action: ProposedAction, name?: string): string {
  const device = name || action.entityId;
  switch (action.name) {
    case "monitor":
      return `Start monitoring ${device}`;
    case "stop_monitoring":
      return `Stop monitoring ${device}`;
    case "turn_off":
      return `Turn off ${device}`;
    case "turn_on":
      return action.brightnessPercent === undefined
        ? `Turn on ${device}`
        : `Turn on ${device} at ${action.brightnessPercent}%`;
  }
}

/** A tool call as the conversation shows it. */
export type ShownToolCall = {
  toolCallId: string;
  toolName: string;
  args: unknown;
  result?: unknown;
};

const proposes = (call: ShownToolCall, action: ProposedAction) => {
  if (call.result !== undefined || call.toolName !== action.name || !isRecord(call.args)) {
    return false;
  }
  const brightness = call.args.brightness_percent ?? undefined;
  const proposed = action.name === "turn_on" ? action.brightnessPercent : undefined;
  return call.args.entity_id === action.entityId && brightness === proposed;
};

/** The unanswered tool call each action proposes, or undefined. The interrupt carries no call
 * ids, so actions pair with calls by their arguments, in order. */
export function callsFor(
  actions: readonly ProposedAction[],
  calls: readonly ShownToolCall[],
): (string | undefined)[] {
  const taken = new Set<string>();
  return actions.map((action) => {
    const call = calls.find((call) => !taken.has(call.toolCallId) && proposes(call, action));
    if (call) taken.add(call.toolCallId);
    return call?.toolCallId;
  });
}
