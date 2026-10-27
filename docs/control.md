# Light control

Home Keeper can switch a monitored light on or off. Control is off unless you
start the `control` profile. Without it, nothing can switch a light.

## Turn it on

Bundled Home Assistant:

```sh
docker compose --profile control up --build --wait
```

Demo:

```sh
docker compose -f compose.demo.yaml --profile control up --build --wait
```

Your own Home Assistant needs a token for a separate non-admin user first. See
[Turn on light control](home-assistant.md#turn-on-light-control).

The profile adds the `controller` service. It publishes no port. Every 5 seconds
it tells the API that it can switch lights. `GET /api/control` returns `on` while
that heartbeat is younger than 15 seconds, and `off` otherwise. Stop the
controller and control turns off within 15 seconds.

The controller sends the heartbeat only while Home Assistant accepts its token.
It checks at start and every 30 seconds, and a command rejected for its token
stops the heartbeat at once. So a revoked or wrong control token shows control
`off`, within about 15 seconds of the rejection. Control comes back on within 30
seconds of Home Assistant accepting the token again.

## Switch a light from the dashboard

While control is `on`, the dashboard's "Monitored lights" panel shows
`CONTROL ON` instead of `READ ONLY`. Each monitored light that is available and
recorded `on` or `off` gets a button: **Turn off** for a light that is on,
**Turn on** for a light that is off. A pending, unknown or unavailable light gets
no button. With control `off`, the dashboard shows no buttons and looks as it
does without the profile.

One click sends the command, with no confirmation and sender `dashboard`. The
button reads **Sending…** and stays disabled until the API answers. The API
waits up to 5 seconds for the light's reading, so with a slow controller the
answer can take a few seconds more. Monitor, Stop monitoring and Refresh
discovery keep working meanwhile. When the API answers, the button is free
again, the dashboard reloads in the background, and it shows one message:

| Outcome        | Message                                                         |
| -------------- | --------------------------------------------------------------- |
| `observed`     | `<light> is now on.` or `<light> is now off.`                   |
| `not_observed` | `Sent to <light>, but no change was seen yet.`                  |
| `refused`      | `Home Assistant refused the command for <light>.`               |
| `unreachable`  | `Cannot reach the light controller. <light> may not have changed.` |

A command the API refuses before sending (see [Refusals](#refusals)) shows the
light's name and the API's message, such as `Desk lamp: The light is already in
that state`, and the dashboard reloads. Any other error shows "Request failed.
Please retry."

The message stays for 15 seconds, or until you send the next command. Collection
status messages appear beside it and do not replace it.

The button always follows the recorded state, not the requested one. Until the
reload finishes, and after a `not_observed` until the collector records the new
state, it still offers the old action. The dashboard has no brightness control;
use the API for that.

## Send a command

Only a light you monitor can be switched, so every command leaves evidence.
Use the light's ID from `/api/catalog`:

```sh
curl -X POST http://127.0.0.1:8000/api/lights/light.hue_desk/command \
  -H 'X-Home-Keeper: 1' -H 'Content-Type: application/json' \
  -d '{"command": {"action": "on", "brightness_pct": 30}, "sender": {"kind": "dashboard"}}'
```

The commands are fixed:

| Command                                    | Home Assistant call                             |
| ------------------------------------------ | ----------------------------------------------- |
| `{"action": "on"}`                         | `light.turn_on`                                 |
| `{"action": "on", "brightness_pct": 1-100}` | `light.turn_on` with that `brightness_pct`      |
| `{"action": "off"}`                        | `light.turn_off`                                |

`sender` names who asked: `{"kind": "dashboard"}`, `{"kind": "browser_chat"}`,
or `{"kind": "telegram", "id": 123456789, "name": "Ada"}`. It is a label for the
record, not authentication. See [security.md](security.md).

### Outcome

The API records the command as `pending`, forwards it to the controller, then
waits up to 5 seconds for a recorded reading of that light. It answers `200`
with the command record and one `outcome`:

| Outcome        | Meaning                                                                                      |
| -------------- | -------------------------------------------------------------------------------------------- |
| `observed`     | A reading received after the send shows the commanded state. `event_id` names the event, when an event showed it. |
| `not_observed` | Home Assistant accepted the call, but no matching reading arrived within 5 seconds.         |
| `refused`      | Home Assistant rejected the call.                                                            |
| `unreachable`  | The controller or Home Assistant did not answer.                                             |

In the command history, a command still waiting shows `pending`. A command the
API was restarted during shows `interrupted`: it may or may not have switched
the light.

A reading received before the send never counts. With a brightness, a recorded
brightness within 2 of `percent × 255 / 100`, rounded half up, counts as a match: 30% is
`77`, so `75` to `79` match.

### Refusals

The API refuses these before it sends anything. It records nothing for them.

| Status | `detail`                                  | When                                                        |
| ------ | ----------------------------------------- | ----------------------------------------------------------- |
| 404    | `Unknown entity`                          | The ID is not in the catalog.                               |
| 422    | `Entity is unsupported`                   | Home Keeper cannot monitor this entity.                     |
| 409    | code `control_off`                        | `GET /api/control` says `off`.                              |
| 409    | code `not_monitored`                      | You do not monitor the light. Monitor it first.             |
| 409    | code `state_unknown`                      | No state is recorded yet, or the state is `unknown`.        |
| 409    | code `unavailable`                        | The light's recorded state is `unavailable`.                |
| 409    | code `already_in_state`                   | The light is already in that state, at that brightness.    |
| 409    | code `busy`                               | Four commands are being sent and this one waited 3 seconds. |

Home Assistant fires no change for a light that is already in the requested
state, so such a command could only end as `not_observed`. The API refuses it
instead.

The API sends at most four commands at a time. A command that waits for a free
send is checked again before it is sent: if the light stopped being monitored or
control turned off meanwhile, it gets that refusal and nothing is sent. A command
that waits more than 3 seconds gets `busy`, so it never switches a light after
its sender has given up.

## Command history

`GET /api/commands` lists recorded commands, newest first: time, light,
command, sender, outcome and the matching event. It takes `start`, `end`,
`limit` (1 to 500, default 50) and `cursor`, and pages like `/api/events`.
Commands expire with the same retention as events; see
[collection.md](collection.md#retention).

### Command history in the dashboard

The "Recorded changes" panel shows **Light commands** above the recorded
changes. The list appears while control is `on`, or while any recorded command
exists. With control `on` and no commands, it reads "No light commands yet."

Each row shows the light, the command (`Turn on`, `Turn on at 30%` or
`Turn off`), the outcome, the sender and the time sent. The sender reads
`Dashboard`, `Browser chat`, or the Telegram user's name and ID, such as
`Sam (Telegram 12345)`. An observed command also shows `Evidence ID:` with the
ID of the recorded change it matched, the same ID the change shows below.

| Outcome        | Shown as                    |
| -------------- | --------------------------- |
| `observed`     | `Observed`                  |
| `not_observed` | `Not observed`              |
| `refused`      | `Refused by Home Assistant` |
| `unreachable`  | `Controller unreachable`    |
| `pending`      | `Pending`                   |
| `interrupted`  | `Interrupted`               |

The list shows the newest 20 commands and refreshes with the dashboard.
**Load older commands** adds the next 20 and stops the refresh until you click
**Refresh history**, like **Load older changes** for recorded changes. The two lists page
apart and are not merged. If the commands cannot be read, the list keeps what it
showed and the dashboard works on.

## The demo

In the demo, the controller forwards each command to the demo collector. The
collector switches the synthetic light, records the change at once, and keeps
that light out of its replay sequence for three collection passes. So a command
on a monitored demo light returns `observed`.

## Limits

- Lights only. No color, color temperature, scenes, switches or plugs.
- A Home Assistant token cannot be limited to some entities. The controller's
  fixed command set and the API's checks are the only limits on what the control
  user does.
- The controller gives Home Assistant 4 seconds per command, from connecting to
  the answer. A slower answer reads as `unreachable`, even if the light switched.
- The dashboard switches lights on and off only. Brightness needs the API.
