# Agent

The agent answers questions about your home in plain words: how long a lamp was
on, at which hours, what a power meter reads, and whether Home Assistant can be
reached. It can also start monitoring a device or switch one, after you
approve.

The agent is optional. Without the `agent` profile, nothing on this page runs.

## Start

1. [Set the model](#set-the-model) in `.env`.
2. Add `--profile agent` to your usual command:

   ```sh
   docker compose --profile agent up --build --wait                       # bundled Home Assistant
   docker compose -f compose.demo.yaml --profile agent up --build --wait  # demo
   ```

3. Open <http://localhost:8000> and select **Ask** in the header.

The profile adds one service:

| Service | Port                                  | Role                                                                 |
| ------- | ------------------------------------- | -------------------------------------------------------------------- |
| `agent` | `127.0.0.1:2026` (`AGENT_PORT`)       | [Aegra](https://github.com/ibbybuilds/aegra) serving the agent with the LangGraph API |

The dashboard reaches the agent through `web` at `/agent`. Port 2026 is only
for LangGraph Studio and other local tools.

To use the agent from Telegram too, see [telegram.md](telegram.md).

## What it can do

The agent reaches Home Keeper only through the public API, as the dashboard
does. It never reads the database or Home Assistant.

| Tool                         | Does                                                                                  | Needs approval |
| ---------------------------- | ------------------------------------------------------------------------------------- | -------------- |
| `list_monitored`             | Lists monitored devices with their kind and current state, such as "Heat" or `"412" "W"` | No          |
| `get_usage`                  | On-time per day and per hour, switch-ons and unknown time for on/off devices; average brightness for lights | No |
| `get_measurements`           | Min, mean, max and latest value with unit over 24 hours, 7 days or 30 days            | No             |
| `get_events`                 | Recorded changes                                                                      | No             |
| `get_health`                 | Whether Home Assistant can be reached                                                 | No             |
| `list_devices`               | Lists the devices in Home Assistant with their kind                                   | No             |
| `show_chart`                 | Draws a chart in the agent panel; see [Charts](#charts)                               | No             |
| `monitor`, `stop_monitoring` | Starts or stops monitoring a device                                                   | Yes            |
| `turn_on`, `turn_off`        | Switches a monitored light, plug or switch; a light can take a brightness of 1–100%   | Yes, and the `control` profile |

Usage lines use Home Assistant's words, for example
`"Open" for 3h 12m, ..., became "Open" 4 time(s)` for a door sensor. A
measurement line reads
`- sensor.desk_power "Desk power": min 3.2, mean 41.5, max 412, latest 380 "W"`,
or `no statistics` when the range has none.

### Rules the agent follows

- Every number comes from the API, never from the model's own arithmetic.
- Every total comes with its unknown time. Unknown time is never "off".
- It states only values `get_measurements` returns. It never estimates power or
  energy use.
- It may suggest a change, but proposes a command only after you ask for one or
  agree.

Before it shows an approval prompt, the agent checks the device and control
state. It shows no prompt, and says why, when:

- control is off,
- the device is not monitored (it offers to monitor it),
- the device is unavailable or its state is unknown,
- the device is already in that state,
- the brightness is outside 1–100%, or a brightness was asked for a plug or
  switch,
- the device is not a light, plug or switch ("This device cannot be switched.").

After you approve, the agent reports the [outcome](control.md#outcome). A
rejection changes nothing.

## The agent panel

The panel opens beside the current tab, so the device cards stay in view.

- **Size.** It starts at 40% of the window. Drag its edge, or focus the edge and
  use the arrow keys, for a width between 360 pixels and 70%. **Expand agent
  panel** toggles 70%. On a narrow screen it fills the screen. The width and
  open state persist across reloads.
- **Answers** stream in. Tool calls show collapsed; open one to see its
  arguments and result. **Stop generating** ends an answer early.
- **Failures.** A failed question goes back into the message box so you can
  resend it.
- **Approvals.** A proposed change shows a card with the device, the action,
  **Approve** and **Reject**. With several changes, decide each; the agent
  continues once all are decided. A proposal the panel cannot display offers
  only **Reject**. A device card changes on its next refresh, never before Home
  Keeper sees the change.
- **Chats.** **Recent chats** lists dashboard chats, newest first, titled by
  their first question. Telegram chats do not appear. **New chat** starts one;
  **Delete chat** deletes the open chat. A reload reopens the last chat.

Without the `agent` profile, the panel says the agent is not running and links
here.

Chats live in the agent's own database. `docker compose --profile agent down`
keeps them; add `--volumes` to delete them.

## Charts

The agent chooses the kind and the data. The dashboard draws the chart in its
own colors for light and dark mode. The agent cannot set colors, sizes or
fonts.

| Kind           | Shows                                                                          | Data from               |
| -------------- | ------------------------------------------------------------------------------ | ----------------------- |
| `bar`          | Values across categories, such as switch-ons per device. Up to 5 series.        | The agent's tool results |
| `line`         | Values over ordered categories, such as minutes on per hour.                    | The agent's tool results |
| `usage_by_day` | On time and unknown time per day, stacked, for up to 5 devices and 366 days.    | The dashboard loads usage itself |

The **⋯** menu on a chart offers:

- **Show as table** / **Show as chart**.
- **Download PNG**: the chart with its title, legend and, for `usage_by_day`,
  each device's totals. Saved as `<title>-<YYYY-MM-DD>.png`.
- **Download CSV**: the table with a header row and raw numbers (usage in hours,
  2 decimals). Saved as `<title>-<YYYY-MM-DD>.csv`. A cell that starts with `=`,
  `+`, `-` or `@` gets a leading `'`, so spreadsheets do not run it as a
  formula.

Details:

- `usage_by_day` uses the time zone the usage report names. A range without
  history draws every day as unknown.
- Hover a bar or point to see its value. Each chart has a text summary for
  screen readers.
- A chart the panel cannot draw says so. A `usage_by_day` chart whose usage
  fails offers **Retry**.
- A reopened chat redraws its charts. `usage_by_day` loads usage again, so days
  Home Assistant has purged show as unknown.
- Device and series names render as text everywhere, including downloads.
- The chart code loads with the first chart.
- Telegram answers are text only. The model is never offered `show_chart` there.

## Set the model

Put the settings in `.env` in the repository root. [.env.example](../.env.example)
lists them.

| Variable                | Description                                                                  |
| ----------------------- | ---------------------------------------------------------------------------- |
| `AGENT_MODEL`           | `provider:model`. Default: `azure:gpt-6.1-sol`.                              |
| `AZURE_OPENAI_ENDPOINT` | Your Azure OpenAI resource, such as `https://my-resource.openai.azure.com`   |
| `AZURE_OPENAI_API_KEY`  | Its API key                                                                  |
| `OPENAI_API_KEY`        | For an `openai:` model                                                       |

- With `azure:`, the part after the colon is your deployment name.
- Any other value goes to LangChain's
  [`init_chat_model`](https://python.langchain.com/docs/how_to/chat_models_universal_init/),
  such as `anthropic:claude-sonnet-4-5`.
- Another provider needs two changes: its LangChain package in
  `apps/agent/pyproject.toml`, and its key variable under the `agent` service's
  `environment` in `compose.agent.yaml`. Compose passes only listed variables
  into the container.
- Model choice lives in `apps/agent/src/home_keeper_agent/model.py`.

A missing required setting stops the agent at startup. `docker compose logs
agent` names the variable, and the service never turns healthy.

## What leaves your machine

Your questions, earlier messages in the chat, and the tool results go to your
model provider. Tool results contain device names, states, timestamps, usage
numbers, the control state and command outcomes.

The Home Assistant token, the database and the service credential never leave.
See [security.md](security.md).

## Example questions

- Which devices are you watching, and are they on?
- How long was the desk lamp on this week, per day? Show it as a chart.
- At which hours is the reading light usually on?
- What did the desk power meter read today?
- Was the desk lamp unavailable at any time yesterday?
- Start monitoring the hallway light.
- Set the reading light to 30 percent.

## Limits

> [!WARNING]
> Aegra has no authentication. Anyone who can reach port 2026, or `/agent` on
> the dashboard, can read and write every chat and run the agent. With the
> `control` profile, they can also approve their own device commands. Both bind
> to `127.0.0.1` for this reason. Do not publish them.

- `web` refuses `/agent` requests that the browser marks as cross-site. Port
  2026 has no such check, so a page you visit can send simple requests to it.
- Aegra does not check the `Host` header, so neither route blocks DNS
  rebinding.
- Aegra's CORS allows no origin, so no other website can read its answers.
- If port 2026 is taken, set `AGENT_PORT`. The panel does not use that port.
- The container writes its Aegra config at startup. `apps/agent/aegra.json` is
  only for `aegra dev`.
- Chats from the earlier Agent Chat UI do not appear in **Recent chats**.
