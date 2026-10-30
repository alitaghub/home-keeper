import type { LangChainMessage } from "@assistant-ui/react-langgraph";

/** A run as the panel sees it: its start, and its end with whether it failed. */
export type RunEvent =
  | { type: "start"; threadId: string | undefined }
  | {
      type: "end";
      threadId: string | undefined;
      resume: boolean;
      failed: boolean;
      /** The question of an ordinary run that failed before Aegra answered at all. */
      unsent: string | undefined;
    };

export type RunEvents = {
  emit: (event: RunEvent) => void;
  subscribe: (listener: (event: RunEvent) => void) => () => void;
};

export function createRunEvents(): RunEvents {
  const listeners = new Set<(event: RunEvent) => void>();
  return {
    emit: (event) => {
      for (const listener of listeners) listener(event);
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The text of the last question among the messages a run sends. */
export function questionOf(messages: readonly LangChainMessage[]): string | undefined {
  const question = messages.findLast((message) => message.type === "human");
  if (!question) return undefined;
  if (typeof question.content === "string") return question.content;
  return question.content.map((part) => (part.type === "text" ? part.text : "")).join("");
}
