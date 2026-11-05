# Device control

With the `control` profile, Home Keeper can switch a monitored light, plug or
switch on or off. Without the profile, nothing can switch a device.

## Turn on control

| Stack                 | Command                                                                    |
| --------------------- | -------------------------------------------------------------------------- |
| Bundled Home Assistant | `docker compose --profile control up --build --wait`                      |
| Demo                  | `docker compose -f compose.demo.yaml --profile control up --build --wait` |
| Own Home Assistant    | Create a control token first. See [Turn on control](home-assistant.md#3-turn-on-control-optional). |

The profile adds the `controller` service. It publishes no port.

### Control state

`GET /api/control` reports `on` or `off`:

- The controller sends a heartbeat to the API every 5 seconds.
- Control is `on` while the last heartbeat is younger than 15 seconds.
- The controller sends heartbeats only while Home Assistant accepts its token.
  It checks at start and every 30 seconds. A command rejected for its token
  stops the heartbeat at once.

| Event                                  | Control turns         |
| -------------------------------------- | --------------------- |
| Controller stops                       | `off` within 15 s     |
| Token revoked or wrong                 | `off` within about 15 s of the rejection |
| Home Assistant accepts the token again | `on` within 30 s      |

## Switch from the dashboard

While control is `on`, the **Home** tab shows `CONTROL ON` instead of
`READ ONLY`.

| Device          | Controls                                         |
| --------------- | ------------------------------------------------ |
| Light           | **Turn on** or **Turn off**, and a brightness slider |
| Plug or switch  | **Turn on** or **Turn off**                      |

A device shows controls only when Home Assistant reports it `on` or `off`. A
pending, unknown or unavailable device has none. With control `off`, the
dashboard shows no controls.

- One click sends the command, with sender `dashboard`. There is no
  confirmation.
- The button reads **Sending…** until the API answers. The API waits up to
  5 seconds for the new state.
- The slider sends `on` at the chosen percent 0.4 seconds after it stops. A drag
  sends one command. Moving the slider of a light that is off turns it on. A
  button click before the slider settles cancels the slider move.
- Buttons and sliders always follow the state Home Assistant reports, not the
  requested one.

After the answer, the dashboard shows one message for 15 seconds or until the
next command:

| Outcome        | Message                                                                     |
| -------------- | --------------------------------------------------------------------------- |
| `observed`     | `<device> is now on.`, `<device> is now on at 40%.` or `<device> is now off.` |
| `not_observed` | `Sent to <device>, but no change was seen yet.`                             |
| `refused`      | `Home Assistant refused the command for <device>.`                          |
| `unreachable`  | `Cannot reach the controller. <device> may not have changed.`               |
| [Refusal](#refusals) | The device name and the API's message, such as `Desk lamp: The light is already in that state`. |
| Other error    | `Request failed. Please retry.`                                             |

## Send a command

Use the device's entity ID from `/api/catalog`:

```sh
curl -X POST http://localhost:8000/api/devices/light.hue_desk/command \
  -H 'X-Home-Keeper: 1' -H 'Content-Type: application/json' \
  -d '{"command": {"action": "on", "brightness_pct": 30}, "sender": {"kind": "dashboard"}}'
```

### Commands

The command set is fixed. The API picks the Home Assistant call from the
device's kind.

| Kind     | Command                                     | Home Assistant call                         |
| -------- | ------------------------------------------- | ------------------------------------------- |
| `light`  | `{"action": "on"}`                          | `light.turn_on`                             |
| `light`  | `{"action": "on", "brightness_pct": 1-100}` | `light.turn_on` with `brightness_pct`       |
| `light`  | `{"action": "off"}`                         | `light.turn_off`                            |
| `switch` | `{"action": "on"}`                          | `switch.turn_on`                            |
| `switch` | `{"action": "off"}`                         | `switch.turn_off`                           |

Every other kind takes no command. See [Device kinds](devices.md#device-kinds).

### Sender

`sender` records who asked. It is a label, not authentication; see
[security.md](security.md).

| Sender                                                  | Used by                                       |
| ------------------------------------------------------- | --------------------------------------------- |
| `{"kind": "dashboard"}`                                 | Device cards                                  |
| `{"kind": "browser_chat"}`                              | The agent, after approval in the agent panel  |
| `{"kind": "telegram", "id": 123456789, "name": "Ada"}`  | The agent, after approval in Telegram         |

### Outcome

The API records the command as `pending` and forwards it to the controller.
It then reads the device's state every 0.5 seconds, for up to 5 seconds after
the send. It answers `200` with the command record, its `device_kind`, and one
outcome:

| Outcome        | Meaning                                                                |
| -------------- | ---------------------------------------------------------------------- |
| `observed`     | A state read after the send shows the commanded state.                 |
| `not_observed` | Home Assistant accepted the call, but no read within 5 seconds showed it. |
| `refused`      | Home Assistant rejected the call.                                      |
| `unreachable`  | The controller or Home Assistant did not answer.                       |

The command log can also show:

| Outcome       | Meaning                                                              |
| ------------- | -------------------------------------------------------------------- |
| `pending`     | The command is still waiting for an outcome.                         |
| `interrupted` | The API restarted during the command. The device may have switched.  |

Matching rules:

- A reading taken before the send never counts.
- A brightness matches when the reported value is within 2 of
  `percent × 255 / 100`, rounded half up. For 30%, that is `77`, so `75` to
  `79` match.

### Refusals

The API checks these in order and answers the first that applies. It sends and
records nothing.

| Status | Code or `detail`                       | When                                                                               |
| ------ | -------------------------------------- | ---------------------------------------------------------------------------------- |
| 404    | `Unknown entity`                       | The ID is not in the catalog.                                                      |
| 409    | `not_monitored` or `not_supported`     | The device is not a light, plug or switch: `not_monitored` if you do not monitor it, else `not_supported`. |
| 422    | `Entity is unsupported`                | Home Keeper cannot monitor this entity.                                            |
| 409    | `control_off`                          | Control is `off`.                                                                  |
| 409    | `not_monitored`                        | You do not monitor the device.                                                     |
| 409    | `state_unknown`                        | Home Assistant cannot be reached, has no such entity, or reports `unknown`.        |
| 409    | `not_supported`                        | The command has `brightness_pct` for a plug or switch.                             |
| 409    | `unavailable`                          | Home Assistant reports the device `unavailable`.                                   |
| 409    | `already_in_state`                     | The device is already in that state and brightness.                                |
| 409    | `busy`                                 | No send slot within 3 seconds, or less than 0.5 seconds of the 8-second budget left. |

Why `already_in_state` exists: Home Assistant fires no change for a device that
is already in the requested state, so the command could only end as
`not_observed`.

### Concurrency and timeouts

- The API sends at most four commands at a time.
- A waiting command is checked again before it is sent. If the device stopped
  being monitored or control turned off, it gets that refusal.
- Each command has 8 seconds from request to the end of its send. The send gets
  only the time left. A send that runs out of time ends as `unreachable`.
- The controller gives Home Assistant 4 seconds per command. A slower answer
  reads as `unreachable`, even if the device switched.

## Command history

`GET /api/commands` lists recorded commands, newest first: time, device,
`device_kind`, command, sender and outcome.

| Parameter        | Description                       |
| ---------------- | --------------------------------- |
| `start`, `end`   | Optional time window              |
| `limit`          | 1 to 500. Default: 50.            |
| `cursor`         | Opaque paging cursor              |

Commands expire after `HOME_KEEPER_RETENTION_DAYS` (30 by default). See
[Command log retention](history.md#command-log-retention). The device change a
command caused shows in History like any other change.

Commands recorded before plugs could be switched carry `device_kind` `light`.

### In the dashboard

The **History** tab shows **Device commands** above the recorded changes. The
list appears while control is `on` or any command exists.

Each row shows the device, the command (`Turn on`, `Turn on at 30%` or
`Turn off`), the outcome, the sender and the time. Today's rows show the time
only; hover for the date.

| Sender value    | Shown as                     |
| --------------- | ---------------------------- |
| `dashboard`     | `Dashboard`                  |
| `browser_chat`  | `Browser chat`               |
| `telegram`      | Name and ID, such as `Sam (Telegram 12345)` |

| Outcome        | Shown as                    |
| -------------- | --------------------------- |
| `observed`     | `Observed`                  |
| `not_observed` | `Not observed`              |
| `refused`      | `Refused by Home Assistant` |
| `unreachable`  | `Controller unreachable`    |
| `pending`      | `Pending`                   |
| `interrupted`  | `Interrupted`               |

The list shows the newest 20 commands and refreshes with the dashboard.
**Load older commands** adds 20 more and pauses the refresh until you click
**Refresh history**. Commands and recorded changes page separately.

## In the demo

The controller forwards each command to the demo reader. The reader switches
the synthetic device at once and holds it out of the replay sequence for three
10-second steps. A command on a monitored demo light or plug returns
`observed`.

## Limits

- On, off and light brightness only. No color, color temperature or scenes.
- A Home Assistant token cannot be limited to some entities. The controller's
  fixed command set and the API's checks are the only limits on the control
  user.
