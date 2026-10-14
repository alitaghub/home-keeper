"""Agent tools over the observer's public API; failures return "Observer error: ..." text.

Names from Home Assistant are JSON-quoted so they can never read as instructions.
"""

import json
from collections.abc import Callable
from urllib.parse import quote

from langchain_core.tools import BaseTool, tool

from home_observer_agent.observer import (
    Catalog,
    CoverageReport,
    DiscoveryRefresh,
    EventsPage,
    Health,
    LightUsage,
    Monitored,
    ObserverClient,
    ObserverError,
    Params,
    Reading,
    Selection,
    Usage,
)

EVENT_LIMIT = 100


def _name(value: str) -> str:
    # json.dumps leaves U+2028/U+2029 raw with ensure_ascii=False; they can break lines.
    quoted = json.dumps(value, ensure_ascii=False)
    return quoted.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def number(value: float) -> str:
    """The observer's value to the millisecond, without a trailing .0 or float noise."""
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


def _reading(reading: Reading | None) -> str:
    if reading is None:
        return "no state"
    if reading.brightness is None:
        return reading.state
    return f"{reading.state} (brightness {reading.brightness}/255)"


def _guard(render: Callable[[], str]) -> str:
    try:
        return render()
    except ObserverError as error:
        return f"Observer error: {error}."
    # A tool must never raise into the run; anything else is an unexpected response.
    except Exception:
        return "Observer error: unexpected response from the observer."


def format_lights(monitored: Monitored) -> str:
    if not monitored.entities:
        return "No lights are monitored."
    lines = [f"{len(monitored.entities)} monitored light(s):"]
    for light in monitored.entities:
        line = f"- {light.id} name={_name(light.name)}: {_reading(light.baseline)}"
        line += f", data {light.freshness}, last confirmed {light.confirmed_at or 'never'}"
        if not light.supported and light.reason:
            line += f", unsupported: {_name(light.reason)}"
        lines.append(line)
    return "\n".join(lines)


def _light_usage(light: LightUsage) -> list[str]:
    brightness = (
        "unknown" if light.average_brightness is None else f"{number(light.average_brightness)}/255"
    )
    lines = [
        f"- {light.entity_id}: on {duration(light.on_seconds)}, "
        f"unknown {duration(light.unknown_seconds)}, switched on {light.switch_ons} time(s), "
        f"average brightness while on {brightness}"
    ]
    lines.extend(
        f"  {day.date}: on {duration(day.on_seconds)}, unknown {duration(day.unknown_seconds)}"
        for day in light.days
    )
    hours = [
        f"{hour:02d}:00 {duration(seconds)}" for hour, seconds in enumerate(light.hours) if seconds
    ]
    lines.append(f"  on-time by hour of day: {', '.join(hours) or 'none'}")
    lines.extend(
        f"  unknown {interval.start} to {interval.end} ({interval.reason})"
        for interval in light.unknown_intervals
    )
    return lines


def format_usage(usage: Usage) -> str:
    lines = [
        f"Usage from {usage.start} to {usage.end}, days and hours in {usage.timezone}, "
        f"data retained since {usage.retained_since}. Unknown time is neither on nor off."
    ]
    if not usage.lights:
        lines.append("No monitored lights match this request.")
    for light in usage.lights:
        lines.extend(_light_usage(light))
    return "\n".join(lines)


def format_events(page: EventsPage) -> str:
    lines = [f"{len(page.events)} event(s), newest first, retained since {page.retained_since}:"]
    lines.extend(
        f"- {event.event_time} {event.entity_id}: {_reading(event.old)} -> {_reading(event.new)}"
        for event in page.events
    )
    if page.next_cursor is not None:
        lines.append(f"More events exist than the {EVENT_LIMIT} shown; narrow the time range.")
    return "\n".join(lines)


def format_coverage(report: CoverageReport) -> str:
    lines = [
        f"Collection is {report.state}, last confirmed {report.last_confirmed_at or 'never'}, "
        f"data retained since {report.retained_since}.",
        f"{len(report.gaps)} gap(s) where recorded data is unknown:"
        if report.gaps
        else "No gaps recorded.",
    ]
    for gap in report.gaps:
        start = gap.last_confirmed_at or gap.detected_at
        end = gap.recovered_at or "now (still open)"
        scope = ", ".join(gap.entity_ids) or "all lights"
        note = ", start uncertain" if gap.uncertain_start else ""
        lines.append(f"- {scope}: unknown from {start} to {end} ({gap.reason}{note})")
    return "\n".join(lines)


def format_health(health: Health) -> str:
    if health.status == "pending":
        return "The collector has not reported yet."
    line = f"The collector is {health.status}, last report {health.received_at}"
    return f"{line}, error: {health.error}." if health.error else f"{line}."


def format_devices(catalog: Catalog) -> str:
    lines = [
        f"{len(catalog.entities)} discovered device(s), discovery {catalog.freshness}, "
        f"refreshed {catalog.refreshed_at or 'never'}:"
    ]
    for device in catalog.entities:
        line = f"- {device.id} name={_name(device.name)}"
        if device.device_name:
            line += f" device={_name(device.device_name)}"
        if device.supported:
            line += ": can be monitored"
        else:
            line += f": cannot be monitored ({_name(device.reason or 'unsupported')})"
        lines.append(line)
    return "\n".join(lines)


def observer_tools(client: ObserverClient) -> list[BaseTool]:
    @tool
    def list_lights() -> str:
        """List the lights the observer records, with their latest state and freshness."""
        return _guard(lambda: format_lights(client.request("GET", "/api/monitored", Monitored)))

    @tool
    def get_usage(start: str, end: str, entity_ids: list[str] | None = None) -> str:
        """Report on-time, unknown time, switch-ons, per-day and per-hour on-time, and average
        brightness for monitored lights. Use this for every usage number.

        Args:
            start: Range start, ISO 8601 with a UTC offset, e.g. 2026-10-12T00:00:00+00:00.
            end: Range end, ISO 8601 with a UTC offset. At most 366 days after start.
            entity_ids: Light entity IDs such as light.kitchen; omit for all monitored lights.
        """
        params: Params = {"start": start, "end": end}
        if entity_ids:
            params["entity_id"] = entity_ids
        return _guard(
            lambda: format_usage(client.request("GET", "/api/usage", Usage, params=params))
        )

    @tool
    def get_events(start: str, end: str, entity_id: str | None = None) -> str:
        """List recorded light changes in a time range, newest first, at most 100.

        Args:
            start: Range start, ISO 8601 with a UTC offset.
            end: Range end, ISO 8601 with a UTC offset.
            entity_id: One light entity ID such as light.kitchen; omit for all lights.
        """
        params: Params = {"start": start, "end": end, "limit": EVENT_LIMIT}
        if entity_id:
            params["entity_id"] = entity_id
        return _guard(
            lambda: format_events(client.request("GET", "/api/events", EventsPage, params=params))
        )

    @tool
    def get_coverage() -> str:
        """Report collection gaps: periods when the observer could not record, so data is
        unknown."""
        return _guard(
            lambda: format_coverage(client.request("GET", "/api/coverage", CoverageReport))
        )

    @tool
    def get_health() -> str:
        """Report whether the collector is healthy and when Home Assistant last answered."""
        return _guard(lambda: format_health(client.request("GET", "/api/health", Health)))

    @tool
    def list_devices() -> str:
        """List the devices the observer discovered in Home Assistant and whether each can be
        monitored."""
        return _guard(lambda: format_devices(client.request("GET", "/api/catalog", Catalog)))

    def select(entity_id: str, selected: bool) -> str:
        # The ID comes from the model, so it is encoded as one path segment.
        path = f"/api/selection/{quote(entity_id, safe='')}"
        selection = client.request("PUT", path, Selection, body={"selected": selected})
        verb = "Now monitoring" if selected else "Stopped monitoring"
        return f"{verb} {_name(entity_id)}. {len(selection.entity_ids)} light(s) monitored."

    @tool
    def refresh_discovery() -> str:
        """Ask the collector to rediscover devices in Home Assistant, for example after a new
        lamp was added. Records nothing by itself."""

        def render() -> str:
            client.request("POST", "/api/discovery/refresh", DiscoveryRefresh)
            return "Discovery refresh requested. Call list_devices shortly to see the result."

        return _guard(render)

    @tool
    def monitor_light(entity_id: str) -> str:
        """Start recording a light. The user must approve this; it never changes the light.

        Args:
            entity_id: A light entity ID from list_devices, such as light.kitchen.
        """
        return _guard(lambda: select(entity_id, selected=True))

    @tool
    def stop_monitoring(entity_id: str) -> str:
        """Stop recording a light. The user must approve this; it never changes the light.

        Args:
            entity_id: A monitored light entity ID, such as light.kitchen.
        """
        return _guard(lambda: select(entity_id, selected=False))

    return [
        list_lights,
        get_usage,
        get_events,
        get_coverage,
        get_health,
        list_devices,
        refresh_discovery,
        monitor_light,
        stop_monitoring,
    ]
