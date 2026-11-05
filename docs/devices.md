# Devices

This page defines which Home Assistant entities Home Keeper can monitor, how it
classifies them, and what it keeps from each one.

## Monitor a device

1. Open the **Setup** tab.
2. Find the device. **Search devices** filters every section by entity name or
   device name.
3. Click **Monitor**.

The device's current state and all the history Home Assistant has for it appear
at once. **Stop monitoring** removes the device from every tab.

- Devices you add to Home Assistant appear on **Setup** within seconds. No
  restart is needed.
- Selection follows the entity's stable registry ID, so it survives a rename.
  A new registry entry that reuses an old entity ID is not monitored.
- Nothing is monitored automatically. Discovery lists devices; it does not
  approve them.

## Device kinds

Every catalog entry has a `device_kind`. The kind decides what Home Keeper
shows and whether it can switch the device.

| Kind          | Entities                                                                                  | Home tab section    | Usage | Can switch |
| ------------- | ----------------------------------------------------------------------------------------- | ------------------- | ----- | ---------- |
| `light`       | `light.*`                                                                                 | Lights              | Yes   | Yes, with brightness |
| `switch`      | `switch.*`, including smart plugs                                                         | Plugs and switches  | Yes   | Yes        |
| `on_off`      | `fan.*`, `binary_sensor.*`, `siren.*`, `humidifier.*`, `remote.*`                          | Other on/off        | Yes   | No         |
| `measurement` | `sensor.*` with state class `measurement`, such as a power meter or a thermometer         | Measurements        | No    | No         |
| `state`       | Any other device, such as `climate.*`, a cover, a text sensor, or a `number.*` entity     | Other               | No    | No         |

Notes:

- An energy meter (state class `total` or `total_increasing`) is a `state`
  device. Home Keeper does not chart energy totals.
- A device first monitored as `state` takes its newer kind at the next catalog
  read, without a new selection.
- Switching needs the [`control` profile](control.md).

## What can be monitored

| Kind                         | Rule                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------- |
| `light`                      | Enabled and not a group.                                                                     |
| `switch`                     | Enabled, belongs to a device, has no entity category, and is not a group.                    |
| `on_off`, `measurement`, `state` | Enabled, belongs to a device, has no entity category, is not hidden, and is not a group. |

People (`person.*`) and trackers (`device_tracker.*`) are never monitored.

An entity is a **group** when any of these is true:

- It comes from the [`group` integration](https://www.home-assistant.io/integrations/group/).
- Its state has an `entity_id` attribute that lists members.
- An attribute such as `is_hue_group` or `is_deconz_group` is true. A Hue room
  is one example.

Entities that cannot be monitored stay in the collapsed **Show other entities**
section, with a reason:

| Reason shown                                          | Cause                                                     |
| ----------------------------------------------------- | --------------------------------------------------------- |
| Groups and rooms are not recorded                     | A group or room.                                          |
| Disabled in Home Assistant                            | The entity is disabled.                                   |
| Hidden in Home Assistant                              | The entity is hidden.                                     |
| Settings and diagnostic switches are not recorded     | A switch with an entity category, such as a child lock.   |
| Switches without a device are not recorded            | A switch that belongs to no device, such as a helper.     |
| Settings and diagnostic entities are not recorded     | Any other entity with an entity category.                 |
| Presence is not recorded                              | A person or a device tracker.                             |
| Only entities of a device are recorded                | Any other entity without a device, such as a template sensor. |

## Names

Cards use the device name. An `on_off`, `measurement` or `state` entity with its
own entity name adds it, such as "Front door · Door", so two entities of one
device stay distinct.

All names come from Home Assistant and render as text, never as HTML.

## What Home Keeper keeps

The reader drops every attribute not listed here, including account
identifiers and raw logs.

| Kind          | Fields                                                                                              |
| ------------- | --------------------------------------------------------------------------------------------------- |
| `light`       | On/off, availability, brightness, and the color fields below                                        |
| `switch`      | On/off and availability. A plug that reports a brightness never shows one.                          |
| `on_off`      | On/off and availability                                                                             |
| `measurement` | State text, value (a finite number, or null for a state such as `unavailable`), unit (at most 32 characters; a longer unit is dropped) |
| `state`       | State text, at most 255 characters                                                                  |

Light fields and their valid ranges:

| Field                                     | Range                                      |
| ----------------------------------------- | ------------------------------------------ |
| `brightness`                              | Integer, 0 to 255                          |
| `color_mode`                              | Home Assistant's value                     |
| `color_temp_kelvin`                       | Integer, 1000 to 10000                     |
| `hs_color`                                | Two finite numbers, 0..360 and 0..100      |
| `xy_color`                                | Two finite numbers, each 0..1              |
| `rgb_color`, `rgbw_color`, `rgbww_color`  | Three, four or five integers, each 0..255  |

A missing field stays absent. Home Keeper does not derive one color format
from another.

A measurement's unit comes from the catalog. Its current reading and its
history rows carry the same unit. A unit changed in Home Assistant appears
after the next catalog read, within 10 seconds.

## State words

Every reading carries a `label`: Home Assistant's translated word for the
state, such as "Heat" for a thermostat or "Open" for a door sensor that is on.
Without a translation, the label is the raw state.

- Discovery reads Home Assistant's language with `get_config`, then its state
  words with `frontend/get_translations` (category `entity_component`).
- It looks up the device's `device_class` first, then the words its domain uses
  for every device class.
- If Home Assistant refuses the language read, Home Keeper asks for English.
- Both reads share a 2-second budget. If it runs out, discovery still succeeds
  and every label is the raw state.

## Discovery

Discovery reads the entity registry, the device registry and every state
(`GET /api/states`) once. From the states it keeps only:

- which candidates are groups,
- each device's `device_class`,
- each sensor's `state_class` and `unit_of_measurement`.

A candidate with no state yet is not a group. If the state read fails or
returns malformed data, discovery fails rather than guess. The next read tries
again.

## Limits

| Limit                       | Value  | When exceeded                                     |
| --------------------------- | ------ | ------------------------------------------------- |
| Entity registry rows        | 5000   | Discovery fails with `invalid_data`               |
| Device registry rows        | 5000   | Discovery fails with `invalid_data`               |
| Monitored devices           | 500    | `422 Selection is full`; nothing is stored        |
