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

Home Assistant fires no change for a light that is already in the requested
state, so such a command could only end as `not_observed`. The API refuses it
instead.

## Command history

`GET /api/commands` lists recorded commands, newest first: time, light,
command, sender, outcome and the matching event. It takes `start`, `end`,
`limit` (1 to 500, default 50) and `cursor`, and pages like `/api/events`.
Commands expire with the same retention as events; see
[collection.md](collection.md#retention).

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
- The dashboard has no switch yet; use the API.
