"""Agent tools over the keeper's public API; failures return "Keeper error: ..." text.

Names from Home Assistant are JSON-quoted so they can never read as instructions.
"""

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from typing import Literal
from urllib.parse import quote

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from pydantic import Field, ValidationError

from home_keeper_agent.keeper import (
    BrowserChatSender,
    Catalog,
    CommandRecord,
    CommandRequest,
    Control,
    DeviceUsage,
    EventsPage,
    Health,
    KeeperClient,
    KeeperError,
    LightOff,
    LightOn,
    LightUsage,
    Monitored,
    Params,
    Reading,
    Selection,
    Sender,
    Usage,
    Wire,
)
from home_keeper_light_command import matches_brightness

EVENT_LIMIT = 100
# A reading's color fields, which a change shows only when one of them changed.
COLOR_FIELDS = (
    "color_mode",
    "color_temp_kelvin",
    "hs_color",
    "xy_color",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
)
COMMAND_TOOLS = ("turn_on", "turn_off")
NOT_ALLOWED = "Not sent: you are not allowed to control lights."
# The keeper's refusal text for a device that is not a light, plug or switch.
NOT_SWITCHABLE = "This device cannot be switched."


def _name(value: str) -> str:
    # json.dumps leaves U+2028/U+2029 raw with ensure_ascii=False; they can break lines.
    quoted = json.dumps(value, ensure_ascii=False)
    return quoted.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def number(value: float) -> str:
    """The keeper's value to the millisecond, without a trailing .0 or float noise."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    # A tiny non-zero value must not read as zero; show it exactly instead.
    return repr(value) if text in ("0", "-0") and value else text


def duration(seconds: float) -> str:
    """Exact seconds, plus whole hours, minutes and seconds from a minute up."""
    exact = f"{number(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    if not minutes:
        return exact
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours}h"] * bool(hours) + [f"{minutes}m"] + [f"{secs}s"] * bool(secs)
    return f"{exact} ({' '.join(parts)})"


def percent(brightness: float) -> int:
    """A 0-255 brightness as a whole percent, rounded half up, so the model does no math."""
    return math.floor(Fraction(brightness) * 100 / 255 + Fraction(1, 2))


def _reading(reading: Reading | None, colors: Sequence[str] = ()) -> str:
    """The state, in Home Assistant's words, and brightness, then each of the `colors`
    fields. The words come from Home Assistant, so they are quoted like names."""
    if reading is None:
        return "state unknown"
    details = [] if reading.brightness is None else [f"brightness {percent(reading.brightness)}%"]
    for field in colors:
        value: object = getattr(reading, field)
        if isinstance(value, tuple):
            value = ", ".join(number(float(part)) for part in value)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType] -- color tuples hold numbers
        details.append(f"{field.replace('_', ' ')} {'none' if value is None else value}")
    label = _name(reading.label)
    return f"{label} ({', '.join(details)})" if details else label


def _change(old: Reading, new: Reading) -> str:
    """Old and new readings; a color change also shows the color mode and what changed."""
    changed = [field for field in COLOR_FIELDS if getattr(old, field) != getattr(new, field)]
    colors = ["color_mode", *(f for f in changed if f != "color_mode")] if changed else []
    return f"{_reading(old, colors)} -> {_reading(new, colors)}"


logger = logging.getLogger(__name__)


def _guard[T](render: Callable[[], T]) -> T | str:
    try:
        return render()
    except KeeperError as error:
        return f"Keeper error: {error}."
    # A tool must never raise into the run; anything else is an unexpected response.
    # Log the traceback so a formatter bug is not mistaken for a keeper failure.
    except Exception:
        logger.exception("Tool failed on an unexpected error")
        return "Keeper error: unexpected response from Home Keeper."


def _kind(device_kind: str | None) -> str:
    return f" kind={device_kind}" if device_kind else ""


def format_monitored(monitored: Monitored) -> str:
    if not monitored.entities:
        return "No devices are monitored."
    lines = [f"{len(monitored.entities)} monitored device(s):"]
    for device in monitored.entities:
        line = f"- {device.id}{_kind(device.device_kind)} name={_name(device.name)}"
        if device.device_name:
            line += f" device={_name(device.device_name)}"
        line += f": {_reading(device.state)}"
        if device.missing:
            line += ", missing from Home Assistant"
        elif not device.supported and device.reason:
            line += f", unsupported: {_name(device.reason)}"
        lines.append(line)
    return "\n".join(lines)


def _device_usage(device: DeviceUsage) -> list[str]:
    line = (
        f"- {device.entity_id}{_kind(device.device_kind)}: on {duration(device.on_seconds)}, "
        f"unknown {duration(device.unknown_seconds)}, switched on {device.switch_ons} time(s)"
    )
    # Plugs and switches have no brightness, so their line never mentions it.
    if isinstance(device, LightUsage):
        brightness = device.average_brightness
        line += ", average brightness while on " + (
            "unknown" if brightness is None else f"{percent(brightness)}%"
        )
    lines = [line]
    lines.extend(
        f"  {day.date}: on {duration(day.on_seconds)}, unknown {duration(day.unknown_seconds)}"
        for day in device.days
    )
    hours = [
        f"{hour:02d}:00 {duration(seconds)}" for hour, seconds in enumerate(device.hours) if seconds
    ]
    lines.append(f"  on-time by hour of day: {', '.join(hours) or 'none'}")
    lines.extend(
        f"  unknown {interval.start} to {interval.end} ({interval.reason})"
        for interval in device.unknown_intervals
    )
    return lines


def format_usage(usage: Usage) -> str:
    lines = [
        f"Usage from {usage.start} to {usage.end}, days and hours in {usage.timezone}, "
        "from Home Assistant's history. Unknown time is neither on nor off: no_history is time "
        "Home Assistant has no history for, unavailable is time it reported the device "
        "unavailable."
    ]
    if not usage.devices:
        lines.append("No monitored lights, plugs or switches match this request.")
    for device in usage.devices:
        lines.extend(_device_usage(device))
    return "\n".join(lines)


def format_events(page: EventsPage) -> str:
    lines = [
        f"{len(page.events)} change(s) from Home Assistant's history, newest first. How far "
        "back it goes depends on Home Assistant's recorder settings:"
    ]
    lines.extend(
        f"- {event.time} {event.entity_id}: {_change(event.old, event.new)}"
        for event in page.events
    )
    if page.next_cursor is not None:
        lines.append(f"More events exist than the {EVENT_LIMIT} shown; narrow the time range.")
    return "\n".join(lines)


def format_health(health: Health) -> str:
    # `healthy` or `unreachable`, as the keeper reports it.
    line = f"Home Assistant is {health.status}, checked {health.checked_at}"
    return f"{line}, error: {health.error}." if health.error else f"{line}."


def format_devices(catalog: Catalog) -> str:
    lines = [f"{len(catalog.entities)} device(s) in Home Assistant:"]
    for device in catalog.entities:
        line = f"- {device.id}{_kind(device.device_kind)} name={_name(device.name)}"
        if device.device_name:
            line += f" device={_name(device.device_name)}"
        if device.supported:
            line += ": can be monitored"
        else:
            line += f": cannot be monitored ({_name(device.reason or 'unsupported')})"
        lines.append(line)
    return "\n".join(lines)


class RunContext(Wire):
    """Who asks in this run and whether they may switch devices.

    Read from the run's `configurable`; the browser chat sets neither, so it gets the defaults.
    """

    sender: Sender = Field(default_factory=BrowserChatSender)
    may_control: bool = True


def run_context(config: RunnableConfig) -> RunContext:
    try:
        return RunContext.model_validate(config.get("configurable", {}))
    except ValidationError:
        # A context that does not parse must not grant control.
        return RunContext(may_control=False)


class CommandCall(Wire):
    entity_id: str
    brightness_percent: int | None = None


def _once[T](fetch: Callable[[], T]) -> Callable[[], T]:
    """Fetch at most once, so later calls reuse the answer or the failure without waiting."""
    results: list[T] = []
    failures: list[KeeperError] = []

    def get() -> T:
        if not results and not failures:
            try:
                results.append(fetch())
            except KeeperError as error:
                failures.append(error)
        if failures:
            raise failures[0]
        return results[0]

    return get


def command_refusals(
    client: KeeperClient, calls: Sequence[tuple[str, Mapping[str, object]]], context: RunContext
) -> list[str | None]:
    """Why each device command must not reach the approval card, or None when it may.

    The keeper refuses the same cases; checking first spares approving a command that
    cannot work. One model response's calls share one read of control and monitored devices.
    """
    control = _once(lambda: client.request("GET", "/api/control", Control))
    monitored = _once(lambda: client.request("GET", "/api/monitored", Monitored))
    return [_command_refusal(name, args, context, control, monitored) for name, args in calls]


def _command_refusal(
    name: str,
    args: Mapping[str, object],
    context: RunContext,
    control: Callable[[], Control],
    monitored: Callable[[], Monitored],
) -> str | None:
    try:
        call = CommandCall.model_validate(args)
    except ValidationError:
        return "Not sent: the arguments are invalid."
    action: Literal["on", "off"] = "on" if name == "turn_on" else "off"
    brightness = call.brightness_percent if action == "on" else None
    entity_id = call.entity_id

    def check() -> str | None:  # noqa: PLR0911 -- one return per ordered check
        if not context.may_control:
            return NOT_ALLOWED
        device = next((device for device in monitored().entities if device.id == entity_id), None)
        # The keeper refuses these too, but only after the user approved it. A device that
        # can never be switched says so before whether control is on.
        if device is not None and device.device_kind not in (None, "light", "switch"):
            return f"Not sent: {NOT_SWITCHABLE}"
        if control().state == "off":
            return "Not sent: light control is off."
        if brightness is not None and brightness not in range(1, 101):
            return "Not sent: brightness must be 1 to 100 percent."
        if device is None:
            return f"Not sent: {entity_id} is not monitored. Offer to start monitoring it first."
        if brightness is not None and device.device_kind == "switch":
            return f"Not sent: {entity_id} is a plug or switch and has no brightness."
        return _state_refusal(entity_id, device.state, action, brightness)

    return _guard(check)


def _state_refusal(
    entity_id: str, reading: Reading | None, action: Literal["on", "off"], brightness: int | None
) -> str | None:
    if reading is not None and reading.state == "unavailable":
        return f"Not sent: {entity_id} is unavailable."
    if reading is None or reading.state not in ("on", "off"):
        return f"Not sent: {entity_id} has no known on/off state."
    # Home Assistant reports no change for these, so the outcome could never be seen.
    if reading.state == action and (
        brightness is None or matches_brightness(reading.brightness, brightness)
    ):
        return f"Not sent: {entity_id} is already {action}."
    return None


def format_outcome(entity_id: str, command: LightOn | LightOff, record: CommandRecord) -> str:
    match record.outcome:
        case "observed":
            return f"{entity_id} is now {command.action} (observed)."
        case "not_observed":
            return f"Sent to {entity_id}, but no change was seen within 5 seconds."
        case "refused":
            return f"Home Assistant refused the command for {entity_id}."
        case "unreachable":
            return f"Cannot reach the light controller; {entity_id} may not have changed."


def keeper_tools(client: KeeperClient) -> list[BaseTool]:
    @tool
    def list_monitored() -> str:
        """List the devices the keeper monitors (lights, plugs, switches and any other
        device), with each one's kind and the state Home Assistant reports now, in its words.
        The first line says whether control is on."""

        def render() -> str:
            monitored = client.request("GET", "/api/monitored", Monitored)
            try:
                control = client.request("GET", "/api/control", Control).state
            except KeeperError:
                control = "unknown"
            return f"Control: {control}\n{format_monitored(monitored)}"

        return _guard(render)

    @tool
    def get_usage(start: str, end: str, entity_ids: list[str] | None = None) -> str:
        """Report on-time, unknown time, switch-ons, and per-day and per-hour on-time for
        monitored lights, plugs and switches, and average brightness for lights. Use this for
        every usage number.

        Args:
            start: Range start, ISO 8601 with a UTC offset, e.g. 2026-10-12T00:00:00+00:00.
            end: Range end, ISO 8601 with a UTC offset. At most 366 days after start.
            entity_ids: Entity IDs such as light.kitchen or switch.kettle; omit for all
                monitored devices.
        """
        params: Params = {"start": start, "end": end}
        if entity_ids:
            params["entity_id"] = entity_ids
        return _guard(
            lambda: format_usage(client.request("GET", "/api/usage", Usage, params=params))
        )

    @tool
    def get_events(start: str, end: str, entity_id: str | None = None) -> str:
        """List changes Home Assistant recorded for monitored devices in a time range, newest
        first, at most 100.

        Args:
            start: Range start, ISO 8601 with a UTC offset.
            end: Range end, ISO 8601 with a UTC offset.
            entity_id: One entity ID such as light.kitchen; omit for every monitored device.
        """
        params: Params = {"start": start, "end": end, "limit": EVENT_LIMIT}
        if entity_id:
            params["entity_id"] = entity_id
        return _guard(
            lambda: format_events(client.request("GET", "/api/events", EventsPage, params=params))
        )

    @tool
    def get_health() -> str:
        """Report whether Home Assistant can be reached right now, and why not."""
        return _guard(lambda: format_health(client.request("GET", "/api/health", Health)))

    @tool
    def list_devices() -> str:
        """List the devices in Home Assistant, with each one's kind and whether it can be
        monitored. Kinds: light; switch, which covers plugs; state, any other device, such as
        a thermostat or a cover, which reports a state but cannot be switched."""
        return _guard(lambda: format_devices(client.request("GET", "/api/catalog", Catalog)))

    def select(entity_id: str, selected: bool) -> str:
        # The ID comes from the model, so it is encoded as one path segment.
        path = f"/api/selection/{quote(entity_id, safe='')}"
        selection = client.request("PUT", path, Selection, body={"selected": selected})
        verb = "Now monitoring" if selected else "Stopped monitoring"
        return f"{verb} {_name(entity_id)}. {len(selection.entity_ids)} device(s) monitored."

    @tool
    def monitor(entity_id: str) -> str:
        """Start recording a device: a light, plug or switch, or any other device. The user
        must approve this; it never changes the device.

        Args:
            entity_id: An entity ID from list_devices that can be monitored, such as
                light.kitchen.
        """
        return _guard(lambda: select(entity_id, selected=True))

    @tool
    def stop_monitoring(entity_id: str) -> str:
        """Stop recording a device: a light, plug or switch, or any other device. The user
        must approve this; it never changes the device.

        Args:
            entity_id: A monitored entity ID, such as light.kitchen.
        """
        return _guard(lambda: select(entity_id, selected=False))

    def send(entity_id: str, command: LightOn | LightOff, config: RunnableConfig) -> str:
        context = run_context(config)
        # The pre-check refused this before the card, but the approver's run decides.
        if not context.may_control:
            return NOT_ALLOWED
        # The ID comes from the model, so it is encoded as one path segment.
        path = f"/api/devices/{quote(entity_id, safe='')}/command"
        request = CommandRequest(command=command, sender=context.sender)
        record = client.request("POST", path, CommandRecord, body=request)
        return format_outcome(entity_id, command, record)

    @tool
    def turn_on(
        entity_id: str, config: RunnableConfig, brightness_percent: int | None = None
    ) -> str:
        """Turn a monitored light, plug or switch on, a light optionally at a brightness. The
        user must approve this. No other kind of device can be switched.

        Args:
            entity_id: A monitored device's ID as list_monitored prints it.
            brightness_percent: Brightness from 1 to 100 percent, for a light only; omit to keep
                the light's own, and always omit it for a plug or switch.
        """
        command = LightOn(brightness_pct=brightness_percent)
        return _guard(lambda: send(entity_id, command, config))

    @tool
    def turn_off(entity_id: str, config: RunnableConfig) -> str:
        """Turn a monitored light, plug or switch off. The user must approve this. No other
        kind of device can be switched.

        Args:
            entity_id: A monitored device's ID as list_monitored prints it.
        """
        return _guard(lambda: send(entity_id, LightOff(), config))

    return [
        list_monitored,
        get_usage,
        get_events,
        get_health,
        list_devices,
        monitor,
        stop_monitoring,
        turn_on,
        turn_off,
    ]
