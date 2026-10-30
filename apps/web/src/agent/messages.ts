import type { LangChainMessage } from "@assistant-ui/react-langgraph";

// A saved thread's messages, parsed into the variants the panel renders: questions,
// answers with their tool calls, and tool results. Anything else is dropped.

type Json = null | string | number | boolean | readonly Json[] | { readonly [key: string]: Json };
type JsonObject = { readonly [key: string]: Json };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isJson = (value: unknown): value is Json =>
  value === null ||
  ["string", "number", "boolean"].includes(typeof value) ||
  (Array.isArray(value) && value.every(isJson)) ||
  (isRecord(value) && Object.values(value).every(isJson));

const isJsonObject = (value: unknown): value is JsonObject => isRecord(value) && isJson(value);

const optionalId = (id: unknown) => (typeof id === "string" ? { id } : {});

// Text only: a string, or the text parts of a content list.
function text(content: unknown): string | null {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  return content
    .map((part) => (isRecord(part) && typeof part.text === "string" ? part.text : ""))
    .join("");
}

function parseToolCall(call: unknown) {
  if (!isRecord(call) || typeof call.id !== "string" || typeof call.name !== "string") return [];
  if (!isJsonObject(call.args)) return [];
  return [{ id: call.id, name: call.name, args: call.args }];
}

function parseMessage(message: unknown): LangChainMessage[] {
  if (!isRecord(message)) return [];
  const content = text(message.content);
  if (content === null) return [];
  switch (message.type) {
    case "human":
      return [{ ...optionalId(message.id), type: "human", content }];
    case "ai": {
      const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
      return [
        {
          ...optionalId(message.id),
          type: "ai",
          content,
          tool_calls: calls.flatMap(parseToolCall),
        },
      ];
    }
    case "tool":
      if (typeof message.tool_call_id !== "string" || typeof message.name !== "string") return [];
      return [
        {
          ...optionalId(message.id),
          type: "tool",
          content,
          tool_call_id: message.tool_call_id,
          name: message.name,
          status: message.status === "error" ? "error" : "success",
        },
      ];
    default:
      return [];
  }
}

/** The messages of a thread's saved values, or none when the values hold no message list. */
export function parseMessages(values: unknown): LangChainMessage[] {
  if (!isRecord(values) || !Array.isArray(values.messages)) return [];
  return values.messages.flatMap(parseMessage);
}
