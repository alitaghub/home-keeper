# Home Keeper

Home Keeper looks after a home through Home Assistant. It shows what devices report now
and what Home Assistant recorded them doing, and, when control is on, switches monitored
devices with a fixed command set.

## Language

### Seeing

**Device**:
A single thing in the home that Home Assistant exposes as one entity of a device: a light, a plug or switch, another on/off device such as a fan or a door sensor, or any other device such as a thermostat or a cover. Groups, rooms, people and settings are not devices.
_Avoid_: entity (for the thing in the home), light (when other devices count too)

**Device kind**:
What a device is, which decides its shape: a light, which is on or off and can also have a brightness and color; a switch, which is only on or off (a plug is a switch); `on_off`, another device that is only on or off, such as a fan, a binary sensor, a siren, a humidifier or a remote, which has usage in Home Assistant's words for its on and off but cannot be switched; or `state`, any other device, which has only a state text in Home Assistant's words and cannot be switched.
_Avoid_: domain, type

**Monitored device**:
A device the user chose to watch. History, usage and the agent cover only monitored devices, and only a monitored device can receive a command.
_Avoid_: selected entity, tracked device

**Selection**:
The set of monitored devices. It is the only filter: monitoring a device again shows all the history Home Assistant has for it.
_Avoid_: watch list, tracked devices

**Reader**:
The process that reads Home Assistant for the API: the device list, current states and history. It holds the Home Assistant owner token, keeps no state and never changes a device.
_Avoid_: poller, importer

**History**:
The past states of a device, as Home Assistant's recorder keeps them. Home Keeper reads it on demand and keeps no copy; how far back it goes is a Home Assistant setting.
_Avoid_: recording, log, timeline (for the data)

**Reading**:
One state of a device as Home Assistant reports it, now or in its history, with Home Assistant's times and its label.
_Avoid_: sample, state update

**Label**:
Home Assistant's translated word for a reading's state, such as "Heat", or "Open" for a door sensor that is on, else the raw state.
_Avoid_: display state, friendly state

### Control

**Control**:
Whether Home Keeper may switch devices right now: on while the controller's heartbeat is fresh, off otherwise.
_Avoid_: light control (it switches plugs and switches too), control mode, write access

**Controller**:
The separate, opt-in process that holds the only Home Assistant credential able to switch devices.
_Avoid_: actuator, light service

**Heartbeat**:
The controller's periodic statement that it can switch devices.
_Avoid_: ping, keepalive

**Command**:
A request to turn one light, plug or switch on or off. A light can also be turned on at a brightness. No other device kind takes a command.
_Avoid_: action, instruction, light command

**Sender**:
Who asked for a command: the dashboard, the browser chat (the agent, approved in the dashboard's agent panel), or a Telegram user. A label for the record, not authentication.
_Avoid_: actor, user

**Refusal**:
Home Keeper declining a command before sending it, for a fixed reason such as the device not being monitored. Nothing reaches the controller.
_Avoid_: rejection, denied command

**Outcome**:
What became of a sent command: pending, observed, not observed, refused (Home Assistant rejected the call), unreachable, or interrupted. A refusal is not an outcome; the command was never sent.
_Avoid_: result, status

**Evidence**:
A reading, received after the send, that shows the state a command asked for. Evidence makes the outcome observed.
_Avoid_: observation, confirmation, ack
