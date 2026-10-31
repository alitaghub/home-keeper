import { Client } from "@langchain/langgraph-sdk";
import { describe, expect, it } from "vitest";
import { ai, createFakeAegra, human } from "../test/fake-aegra";
import { aegraThreadList } from "./threads";

// The real SDK client against the fake Aegra, so the adapter is tested through the
// requests Aegra actually receives.
function setup() {
  const aegra = createFakeAegra();
  const client = new Client({
    apiUrl: "http://dashboard.test/agent",
    callerOptions: { fetch: aegra.fetch, maxRetries: 0 },
  });
  return { aegra, adapter: aegraThreadList(client) };
}

describe("aegraThreadList", () => {
  it("lists only dashboard chats, newest first, titled by their first question", async () => {
    const { aegra, adapter } = setup();
    const older = aegra.seed({
      metadata: { source: "dashboard", thread_name: "How long was the desk lamp on?" },
      messages: [human("How long was the desk lamp on?"), ai("2h.")],
    });
    // Telegram creates its threads with no metadata of its own; Studio the same.
    aegra.seed({ metadata: { thread_name: "telegram question" } });
    aegra.seed({ metadata: { source: "studio", thread_name: "studio question" } });
    const newer = aegra.seed({
      metadata: { source: "dashboard", thread_name: "Which lights are on?" },
    });

    const { threads } = await adapter.list();

    expect(threads.map(({ remoteId, title, status }) => ({ remoteId, title, status }))).toEqual([
      { remoteId: newer.thread_id, title: "Which lights are on?", status: "regular" },
      { remoteId: older.thread_id, title: "How long was the desk lamp on?", status: "regular" },
    ]);
  });

  it("creates each new chat in Aegra as a dashboard chat", async () => {
    const { aegra, adapter } = setup();

    const { remoteId, externalId } = await adapter.initialize("local-1");

    const created = aegra.threads.get(remoteId);
    expect(created?.metadata.source).toBe("dashboard");
    // The LangGraph runtime streams to the external id, so it must be the Aegra thread too.
    expect(externalId ?? remoteId).toBe(remoteId);
    expect((await adapter.list()).threads.map((thread) => thread.remoteId)).toEqual([remoteId]);
  });

  it("reads one chat's title and ids back", async () => {
    const { aegra, adapter } = setup();
    const thread = aegra.seed({
      metadata: { source: "dashboard", thread_name: "Turn the desk lamp off" },
    });

    const fetched = await adapter.fetch(thread.thread_id);

    expect(fetched).toMatchObject({
      remoteId: thread.thread_id,
      title: "Turn the desk lamp off",
      status: "regular",
    });
    expect(fetched.externalId ?? fetched.remoteId).toBe(thread.thread_id);
  });

  it("deletes a chat from Aegra", async () => {
    const { aegra, adapter } = setup();
    const kept = aegra.seed({ metadata: { source: "dashboard", thread_name: "kept" } });
    const gone = aegra.seed({ metadata: { source: "dashboard", thread_name: "gone" } });

    await adapter.delete(gone.thread_id);

    expect(aegra.requestsTo("DELETE", `/threads/${gone.thread_id}`)).toHaveLength(1);
    expect([...aegra.threads.keys()]).toEqual([kept.thread_id]);
    expect((await adapter.list()).threads.map((thread) => thread.title)).toEqual(["kept"]);
  });
});
