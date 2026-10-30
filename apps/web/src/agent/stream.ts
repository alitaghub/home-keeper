// Makes Aegra's run stream readable by assistant-ui.

export type StreamEvent = { event: string; data: unknown };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

// The SDK types a streamed AI chunk as "ai"; Aegra sends "AIMessageChunk".
const isAiChunk = (type: unknown) => type === "ai" || type === "AIMessageChunk";

/** Adapts a run streamed with `streamMode: ["messages-tuple", "updates"]`. */
export async function* adaptAegraStream(
  events: AsyncIterable<StreamEvent>,
): AsyncGenerator<StreamEvent> {
  // One model step can stream under two ids (OpenAI Responses API); one id per step keeps
  // it one reply.
  const stepIds = new Map<string, unknown>();
  for await (const event of events) {
    if (event.event === "messages" && Array.isArray(event.data)) {
      const [chunk, metadata, ...rest] = event.data;
      if (isRecord(chunk) && isAiChunk(chunk.type) && isRecord(metadata)) {
        const step = `${String(metadata.run_id)}:${String(metadata.langgraph_step)}`;
        const id = stepIds.get(step) ?? chunk.id;
        if (id !== undefined) stepIds.set(step, id);
        yield { ...event, data: [{ ...chunk, id }, metadata, ...rest] };
        continue;
      }
      // assistant-ui drops tool result chunks, so they pass on as tool messages.
      if (isRecord(chunk) && chunk.type === "ToolMessageChunk") {
        yield { ...event, data: [{ ...chunk, type: "tool" }, metadata, ...rest] };
        continue;
      }
    }
    // Messages come from the tuples; an update keeps only its interrupt, and an update
    // without one clears a pending approval.
    if (event.event === "updates") {
      const data = event.data;
      yield {
        ...event,
        data:
          isRecord(data) && "__interrupt__" in data ? { __interrupt__: data.__interrupt__ } : {},
      };
      continue;
    }
    yield event;
  }
}
