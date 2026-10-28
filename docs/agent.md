# Chat with the agent

The agent helps you look after your lights. It answers questions in plain
words: how long a lamp was on, at which hours, how often it was switched on,
and whether collection was healthy. It works only from the evidence Home Keeper
recorded. With the `control` profile, it can also switch a monitored light
after you approve the command. It is opt-in: without the `agent` profile,
nothing below runs.

## What it can and cannot do

The agent reaches Home Keeper only through the public API, the same one the
dashboard uses. It can:

- list the lights you monitor and their current state
- report usage over a time range: on-time per day and per hour, switch-ons,
  average brightness, and unknown time
- show recorded changes, collection gaps and collector health
- list discovered devices and refresh discovery
- start or stop monitoring a light, after you approve it in the chat. You can
  approve or reject the proposed change; a rejection changes nothing
- turn a monitored light on, optionally at a brightness from 1 to 100 percent,
  or off, after you approve it in the chat. This needs the `control` profile
  (see [control.md](control.md)). The agent reports what Home Keeper then
  observed: the change, no change within 5 seconds, a refusal from Home
  Assistant, or an unreachable controller. A rejection sends nothing

The agent can suggest a light change inside an answer, but it proposes a
command only after you ask for one or agree to its suggestion. Before you see
the approval prompt, the agent checks the light and the control state itself.
It shows no prompt, and says why, when light control is off, the light is not
monitored (it offers to monitor it), the light is unavailable or has no known
on/off state, the light is already in that state, or the brightness is outside
1 to 100 percent. In Telegram, only people on the control allowlist can
switch lights; see [Telegram light control](telegram.md#switch-lights).

It cannot:

- see power or energy. Home Keeper does not record it. The agent says so.
- reach Home Assistant. It sends light commands to the Home Keeper API, which
  forwards them to the controller.
- read the database. It sees only what the API returns.

Usage numbers come from the API, not from the model's own arithmetic. Every
total comes with the time that is unknown because collection stopped. Unknown
time is never counted as "off".

## Start

Set the model first (see below), then add `--profile agent` to the command you
already use:

```sh
docker compose --profile agent up --build --wait                     # bundled Home Assistant
docker compose -f compose.demo.yaml --profile agent up --build --wait  # synthetic demo
```

Open the chat at <http://localhost:3000>. To use Telegram too, see
[Telegram setup](telegram.md). It uses this same agent with separate
conversations and explicit monitoring approvals.

The profile adds three containers:

| Service    | Port (on `127.0.0.1`) | Job                                                         |
| ---------- | --------------------- | ----------------------------------------------------------- |
| `agent`    | 2026 (`AGENT_PORT`)   | [Aegra](https://github.com/ibbybuilds/aegra) serving the agent with the LangGraph API |
| `chat`     | 3000 (`CHAT_PORT`)    | [Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui), unmodified |
| `postgres` | none                  | Stores chat threads for Aegra                               |

Threads are stored in Postgres, so a conversation survives a restart.
`docker compose --profile agent down` keeps them; add `--volumes` to delete them
with everything else.

If a port is taken, override it, for example
`AGENT_PORT=12026 CHAT_PORT=13000 docker compose --profile agent up --build --wait`.
The chat page in your browser calls Aegra directly on its port. Aegra allows
only the chat page's origin, `http://localhost:$CHAT_PORT`, and refuses every
other website. Open the chat at `localhost`, not `127.0.0.1`: the browser treats them as
different origins, so the page at `127.0.0.1` cannot reach Aegra. `localhost` also
works through an SSH port forward. The chat image has Aegra's address built in, so changing
`AGENT_PORT` rebuilds it. Agent Chat UI also ships an `/api` proxy to Aegra; it
is left without a target, because it would let any website through.
The container writes its Aegra config at startup from `CHAT_ORIGIN`; `apps/agent/aegra.json` is only for running `aegra dev` locally.

## Set the model

Put the model settings in a `.env` file in the repository root. Compose reads it
automatically and Git ignores it. [.env.example](../.env.example) lists them.

| Variable                | Meaning                                                                 |
| ----------------------- | ----------------------------------------------------------------------- |
| `AGENT_MODEL`           | `provider:model`. Default `azure:gpt-6.1-sol`.                          |
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI resource, for example `https://my-resource.openai.azure.com` |
| `AZURE_OPENAI_API_KEY`  | Its API key                                                             |

With an `azure:` model, the part after the colon is your deployment name. Any
other value is passed to LangChain's
[`init_chat_model`](https://python.langchain.com/docs/how_to/chat_models_universal_init/),
for example `anthropic:claude-sonnet-4-5`. Model choice lives in one file,
`apps/agent/src/home_keeper_agent/model.py`. `openai:` models work with
`OPENAI_API_KEY` in `.env`. Any other provider needs two changes: its LangChain
package in `apps/agent/pyproject.toml`, and its key variable listed under the
`agent` service's `environment` in `compose.agent.yaml`. Compose passes only the
listed variables into the container, not the whole `.env`. The rest of the agent
does not change.

If a required setting is missing, the agent fails at startup, names the
variable in `docker compose logs agent`, and never turns healthy.

## What leaves your machine

When the agent is on, your questions and the tool results go to the model
provider you configured. Tool results contain light names, states, timestamps,
usage numbers, the light control state and the outcome of each light command. The Home Assistant token, the database and the service
credential never leave. [security.md](security.md) has the full boundary.

## Example questions

- Which lights are you watching, and are they on?
- How long was the desk lamp on this week, per day?
- At which hours is the reading light usually on?
- Which light was on longest in the last 7 days?
- Were there any collection gaps yesterday?
- Is collection healthy? When did Home Assistant last answer?
- Start monitoring the hallway light.
- Turn the desk lamp off.
- Set the reading light to 30 percent.

## Limits

- Neither Aegra nor the chat page has authentication. Anyone who can reach
  port 2026 can read and write every thread and start the agent. With the
  `control` profile, they can also propose and approve their own light
  commands. Both bind to `127.0.0.1` for that reason. Do not publish them on
  your network.
- CORS only stops other websites from reading Aegra's answers. A page you visit
  can still send simple cross-site requests to it, and Aegra does not check the
  `Host` header, so DNS rebinding is not blocked.
- Known Aegra issue
  [#468](https://github.com/ibbybuilds/aegra/issues/468): when you switch
  threads, the chat can show an approval prompt that belongs to another thread.
  Check the proposed light before you approve.
