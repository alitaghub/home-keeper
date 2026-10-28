# Home Keeper

Home Keeper looks after a home through Home Assistant. It records what devices do,
and, when control is on, switches monitored devices with a fixed command set.

## Language

### Recording

**Device**:
A single light, plug or switch that Home Assistant exposes as one entity.
_Avoid_: entity (for the thing in the home), light (when plugs and switches count too)

**Device kind**:
What a device is: a light, which can also have a brightness, or a switch, which is only on or off. A plug is a switch.
_Avoid_: domain, type

**Monitored device**:
A device the user chose to record. Only a monitored device can receive a command.
_Avoid_: selected entity, tracked device

**Reading**:
One recorded state of a device, from an event or a snapshot, with the time Home Keeper received it.
_Avoid_: sample, state update

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
A request to turn one device on or off. A light can also be turned on at a brightness.
_Avoid_: action, instruction, light command

**Sender**:
Who asked for a command: the dashboard, the browser chat, or a Telegram user. A label for the record, not authentication.
_Avoid_: actor, user

**Refusal**:
Home Keeper declining a command before sending it, for a fixed reason such as the device not being monitored. Nothing reaches the controller.
_Avoid_: rejection, denied command

**Outcome**:
What became of a sent command: pending, observed, not observed, refused (Home Assistant rejected the call), unreachable, or interrupted. A refusal is not an outcome; the command was never sent.
_Avoid_: result, status

**Evidence**:
A reading, received after the send, that shows the state a command asked for. Evidence makes the outcome observed.
_Avoid_: observation (the collector's snapshot of a device), confirmation, ack
