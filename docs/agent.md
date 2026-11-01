# Chat with the agent

The agent helps you look after your lights. It answers questions in plain
words: how long a lamp was on, at which hours, how often it was switched on,
and whether Home Assistant can be reached. It works only from Home Assistant's
history and current states, as the Home Keeper API reports them, for the devices
you monitor. With the `control` profile, it can also switch a monitored light
after you approve the command. It is opt-in: without the `agent` profile,
nothing below runs.

## What it can and cannot do

The agent reaches Home Keeper only through the public API, the same one the
dashboard uses. It can:

- list the devices you monitor (`list_monitored`), with each one's kind
  (`kind=light`, `kind=switch`, `kind=on_off` for another on/off device such as
  a fan or a door sensor, `kind=measurement` for a sensor with a value and a
  unit, such as a power meter, or `kind=state` for any other device) and its
  current state in Home Assistant's words, such as "Heat", or its value and
  unit, such as `"412" "W"`
- report usage over a time range for lights, plugs, switches and other on/off
  devices: on-time per day and per hour, switch-ons, and unknown time. Lights
  also get their average brightness; plugs and switches have none. An on/off
  device's line uses Home Assistant's words, such as
  `"Open" for 3h 12m, ..., became "Open" 4 time(s)` for a door sensor
- report a monitored measurement's lowest, mean, highest and latest value with
  its unit over the last 24 hours, 7 days or 30 days (`get_measurements`), from
  Home Assistant's statistics, one line per device:
  `- sensor.desk_power "Desk power": min 3.2, mean 41.5, max 412, latest 380 "W"`.
  A device without statistics for the range reads `no statistics`
- show the changes Home Assistant recorded, and whether Home Assistant can be reached
- list the devices in Home Assistant, with their kind
- start or stop monitoring a device (`monitor`, `stop_monitoring`),
  after you approve it in the chat. You can
  approve or reject the proposed change; a rejection changes nothing
- turn a monitored light, plug or switch on or off (`turn_on`, `turn_off`),
  after you approve it in the chat. A light can also take a brightness from 1
  to 100 percent; a plug or switch cannot. No other device can be switched: a
  command for an `on_off`, `measurement` or `state` device answers "This device
  cannot be switched." This
  needs the `control` profile
  (see [control.md](control.md)). The agent reports what Home Keeper then
  observed: the change, no change within 5 seconds, a refusal from Home
  Assistant, or an unreachable controller. A rejection sends nothing

The agent can suggest a change inside an answer, but it proposes a command
only after you ask for one or agree to its suggestion. Before you see the
approval prompt, the agent checks the device and the control state itself.
It shows no prompt, and says why, when light control is off, the device is not
monitored (it offers to monitor it), the device is unavailable or has no known
on/off state, the device is already in that state, the brightness is outside
1 to 100 percent, or a brightness was asked for a plug or switch. In Telegram,
only people on the control allowlist can switch lights, plugs and switches; see
[Telegram device control](telegram.md#switch-lights-plugs-and-switches).

It cannot:

- see a value it was not given. It states only the numbers `get_measurements`
  returns, with their unit, and never estimates power or energy use. Without
  statistics, or for a device that is not a monitored measurement, it says it
  cannot see the value.
- reach Home Assistant. It sends device commands to the Home Keeper API, which
  forwards them to the controller.
- read the database. It sees only what the API returns.

Usage numbers come from the API, not from the model's own arithmetic. Every
total comes with the time that is unknown: time Home Assistant has no history
for, or reported the device unavailable. Unknown time is never counted as "off".

## Start

Set the model first (see below), then add `--profile agent` to the command you
already use:

```sh
docker compose --profile agent up --build --wait                     # bundled Home Assistant
docker compose -f compose.demo.yaml --profile agent up --build --wait  # synthetic demo
```

Open the dashboard at <http://localhost:8000> and select **Ask** in the header.
The agent panel opens beside the current tab, so the device cards stay in view
while you talk about them. It starts at 40% of the window. Drag its edge, or
focus the edge and use the arrow keys, to make it between 360 pixels and 70% of
the window wide; **Expand agent panel** jumps to 70% and back. On a narrow screen
it fills the screen; **Close agent panel** returns to the tab you were on. The
panel's width, and whether it is open, are remembered across reloads in the same
browser. Without the `agent` profile, the panel says the
agent isn't running and links here; the rest of the dashboard works as before.

To use Telegram too, see [Telegram setup](telegram.md). It uses this same agent
with separate conversations and explicit monitoring approvals.

The profile adds one container:

| Service | Port (on `127.0.0.1`) | Job                                                                                    |
| ------- | --------------------- | -------------------------------------------------------------------------------------- |
| `agent` | 2026 (`AGENT_PORT`)   | [Aegra](https://github.com/ibbybuilds/aegra) serving the agent with the LangGraph API |

The dashboard reaches Aegra through the web container, at `/agent` on the
dashboard's own address. Port 2026 is only for
LangGraph Studio and other local tools.

## Use the agent panel

- Answers stream in as the agent writes them. When the agent uses a tool, the
  panel shows the call collapsed; open it to see the arguments and the result.
  **Stop generating** ends an answer early.
- When a question fails, the panel says so and puts the question back in that
  chat's message box, so you can send it again.
- When the agent proposes a device change (`monitor`, `stop_monitoring`,
  `turn_on`, `turn_off`), a card on that call names the device and the action,
  with **Approve** and **Reject**. The device name comes from the dashboard; a
  device the dashboard does not know shows its entity id. When one proposal
  holds several changes, you decide each one; the agent continues once all of
  them have a decision. A card disappears once you decide. If the proposal is in
  a form the dashboard cannot show, the panel offers only **Reject**.
- After you approve a command, the agent reports what Home Keeper observed (see
  [control.md](control.md)). The device card on the page changes on its next
  refresh, never before Home Keeper sees the change.
- **Recent chats** lists the chats you started in the dashboard, newest first,
  titled by their first question. Telegram conversations do not appear.
  **New chat** starts another; **Delete chat** deletes the open chat from the
  agent's database. A reload reopens the chat you had open. If a chat's history
  cannot be loaded, the panel says so and offers **Try again** instead of a
  message box.

Threads are stored in the agent's own database in the shared Postgres, so a
conversation survives a restart.
`docker compose --profile agent down` keeps them; add `--volumes` to delete them
with everything else.

If port 2026 is taken, override it, for example
`AGENT_PORT=12026 docker compose --profile agent up --build --wait`. The
dashboard does not use that port, so the panel keeps working.
Aegra's CORS allows no browser origin: the panel calls it on the dashboard's own
origin, and no other website may read its answers. The container writes its Aegra
config at startup; `apps/agent/aegra.json` is only for running `aegra dev` locally.

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
- Was the desk lamp unavailable at any time yesterday?
- Can you reach Home Assistant right now?
- Start monitoring the hallway light.
- Turn the desk lamp off.
- Turn on the kettle plug.
- Set the reading light to 30 percent.

## Limits

- Aegra has no authentication. Anyone who can reach port 2026, or `/agent` on
  the dashboard's address, can read and write every thread and start the agent.
  With the `control` profile, they can also propose and approve their own device
  commands. Both bind to `127.0.0.1` for that reason. Do not publish them on
  your network.
- The web container refuses `/agent` requests that browsers mark as sent by
  another website. Port 2026 has no such check: a page you visit can still send
  simple cross-site requests to it. Aegra does not check the `Host` header, so
  DNS rebinding is not blocked on either.
- There is no separate chat page. Threads made in the earlier Agent Chat UI do
  not appear in Recent chats.
