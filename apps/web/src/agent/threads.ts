import type { RemoteThreadListAdapter } from "@assistant-ui/react";
import type { Client, Thread } from "@langchain/langgraph-sdk";
import { createAssistantStream } from "assistant-stream";

// Telegram conversations live in the same Aegra. Only threads the dashboard started carry
// this tag, so Recent chats never shows a Telegram chat.
const DASHBOARD = { source: "dashboard" };
const PAGE = 50;

type ThreadsApi = {
  threads: Pick<Client["threads"], "search" | "create" | "get" | "update" | "delete">;
};

type ThreadMetadata = Awaited<ReturnType<RemoteThreadListAdapter["fetch"]>>;

const text = (value: unknown) => (typeof value === "string" && value ? value : undefined);

function metadataOf({ thread_id, metadata, updated_at }: Thread): ThreadMetadata {
  return {
    status: metadata?.archived === true ? "archived" : "regular",
    remoteId: thread_id,
    externalId: thread_id,
    // Aegra names a thread after its first question; the dashboard cannot rename one.
    title: text(metadata?.title) ?? text(metadata?.thread_name),
    lastMessageAt: new Date(updated_at),
  };
}

/** The chat does not exist in Aegra, or is not one of the dashboard's. */
export class ChatNotFound extends Error {
  constructor(threadId: string) {
    super(`${threadId} is not a dashboard chat in Aegra.`);
  }
}

// The SDK's HTTPError carries the response status.
const isNotFound = (error: unknown) =>
  typeof error === "object" && error !== null && "status" in error && error.status === 404;

/** Recent chats from Aegra's own threads: no second store to keep in step. */
export function aegraThreadList({ threads }: ThreadsApi): RemoteThreadListAdapter {
  return {
    async list(options) {
      const offset = Number(options?.after ?? 0);
      const found = await threads.search({
        metadata: DASHBOARD,
        limit: PAGE,
        offset,
        sortBy: "updated_at",
        sortOrder: "desc",
      });
      return {
        threads: found.map(metadataOf),
        nextCursor: found.length === PAGE ? String(offset + PAGE) : undefined,
      };
    },
    async initialize() {
      const { thread_id } = await threads.create({ metadata: DASHBOARD });
      return { remoteId: thread_id, externalId: thread_id };
    },
    async fetch(threadId) {
      const thread = await threads.get(threadId).catch((error: unknown) => {
        throw isNotFound(error) ? new ChatNotFound(threadId) : error;
      });
      // A saved id may point anywhere; only the dashboard's own chats open here.
      if (thread.metadata?.source !== DASHBOARD.source) throw new ChatNotFound(threadId);
      return metadataOf(thread);
    },
    async rename(threadId, title) {
      await threads.update(threadId, { metadata: { title } });
    },
    async archive(threadId) {
      await threads.update(threadId, { metadata: { archived: true } });
    },
    async unarchive(threadId) {
      await threads.update(threadId, { metadata: { archived: false } });
    },
    async delete(threadId) {
      await threads.delete(threadId);
    },
    // Aegra titles the thread when its first run starts, so the title is read, not made.
    async generateTitle(threadId) {
      const title = text((await threads.get(threadId)).metadata?.thread_name);
      return createAssistantStream((controller) => {
        if (title) controller.appendText(title);
      });
    },
  };
}
