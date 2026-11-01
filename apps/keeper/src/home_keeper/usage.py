"""How long selected lights, plugs and switches were on, from Home Assistant's history. Other
devices have no on-time.

Time the history cannot vouch for is unknown, never on or off.
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from home_keeper.models import (
    DayUsage,
    DeviceUsage,
    LightPeriod,
    LightReading,
    LightUsage,
    SwitchableKind,
    SwitchPeriod,
    SwitchUsage,
    UnknownInterval,
    UnknownReason,
)
from home_keeper.reader import HistoryRow


@dataclass(frozen=True)
class Evidence:
    """A state the device had from `at` until the next evidence."""

    at: datetime
    # A light's or switch's state; any other text counts as unknown.
    state: str
    brightness: int | None


@dataclass(frozen=True)
class Known:
    on: bool
    brightness: int | None


def row_evidence(row: HistoryRow) -> Evidence:
    reading = row.reading
    brightness = reading.brightness if isinstance(reading, LightReading) else None
    return Evidence(row.updated_at, reading.state, brightness)


def local_days(
    start: datetime, end: datetime, timezone: ZoneInfo
) -> Iterator[tuple[date, datetime, datetime]]:
    """Split [start, end) at local midnights."""
    while start < end:
        day = start.astimezone(timezone).date()
        midnights = (
            datetime.combine(day + timedelta(days=1), time(fold=fold), tzinfo=timezone).astimezone(
                UTC
            )
            for fold in (0, 1)
        )
        # Where clocks fall back over midnight, the next day starts at its later occurrence.
        stop = min(end, min((moment for moment in midnights if moment > start), default=end))
        yield day, start, stop
        start = stop


def local_hours(start: datetime, end: datetime, timezone: ZoneInfo) -> Iterator[tuple[int, float]]:
    """Seconds per local hour of [start, end), a span within one local day."""
    # With one UTC offset all day, local hours are whole hours apart in UTC. Around an offset
    # change, step in quarter hours: every current offset is a whole number of quarters.
    last = (end - timedelta(microseconds=1)).astimezone(timezone)
    step = 60 if start.astimezone(timezone).utcoffset() == last.utcoffset() else 15
    while start < end:
        local = start.astimezone(timezone)
        into = timedelta(
            minutes=local.minute % step, seconds=local.second, microseconds=local.microsecond
        )
        stop = min(end, start - into + timedelta(minutes=step))
        yield local.hour, (stop - start).total_seconds()
        start = stop


def local_dates(start: datetime, end: datetime, timezone: ZoneInfo) -> list[date]:
    first = start.astimezone(timezone).date()
    # The range is half-open: a range ending at local midnight does not touch the next day.
    last = (end - timedelta(microseconds=1)).astimezone(timezone).date() if end > start else first
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def device_usage(
    entity_id: str,
    device_kind: SwitchableKind,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    timezone: ZoneInfo,
    rows: Sequence[HistoryRow],
) -> DeviceUsage:
    """Usage of one device from its Home Assistant history rows, oldest first. The first row
    may be the state at `start`.

    Plugs and switches get the same numbers as lights, without brightness."""
    evidence = sorted((row_evidence(row) for row in rows), key=lambda item: item.at)
    moments = [item.at for item in evidence]

    def status(moment: datetime) -> Known | UnknownReason:
        index = bisect_right(moments, moment) - 1
        if moment >= now or index < 0:
            return "no_history"
        item = evidence[index]
        if item.state not in ("on", "off"):
            return "unavailable"
        return Known(item.state == "on", item.brightness)

    boundaries = sorted(
        {start, end} | {moment for moment in (now, *moments) if start < moment < end}
    )
    days = {day: [0.0, 0.0] for day in local_dates(start, end, timezone)}
    hours = [0.0] * 24
    on_seconds = unknown_seconds = brightness_seconds = weighted_brightness = 0.0
    unknown_intervals: list[UnknownInterval] = []
    periods: list[LightPeriod] = []
    for segment_start, segment_end in pairwise(boundaries):
        current = status(segment_start)
        seconds = (segment_end - segment_start).total_seconds()
        if isinstance(current, Known):
            state = "on" if current.on else "off"
            brightness = current.brightness if current.on else None
            last = periods[-1] if periods else None
            # A row that repeats the same state continues the period.
            if (
                last
                and last.end == segment_start.isoformat()
                and (last.state, last.brightness) == (state, brightness)
            ):
                last.end = segment_end.isoformat()
            else:
                periods.append(
                    LightPeriod(
                        start=segment_start.isoformat(),
                        end=segment_end.isoformat(),
                        state=state,
                        brightness=brightness,
                    )
                )
            if not current.on:
                continue
            on_seconds += seconds
            if current.brightness is not None:
                brightness_seconds += seconds
                weighted_brightness += current.brightness * seconds
            for day, day_start, day_end in local_days(segment_start, segment_end, timezone):
                days[day][0] += (day_end - day_start).total_seconds()
                for hour, part in local_hours(day_start, day_end, timezone):
                    hours[hour] += part
            continue
        unknown_seconds += seconds
        for day, day_start, day_end in local_days(segment_start, segment_end, timezone):
            days[day][1] += (day_end - day_start).total_seconds()
        previous = unknown_intervals[-1] if unknown_intervals else None
        if previous and previous.reason == current and previous.end == segment_start.isoformat():
            previous.end = segment_end.isoformat()
        else:
            unknown_intervals.append(
                UnknownInterval(
                    start=segment_start.isoformat(), end=segment_end.isoformat(), reason=current
                )
            )

    # Only recorded off-to-on changes count; none is inferred at the range start. A change at
    # exactly `start` counts here and not in the window that ends there.
    switch_ons = sum(
        1
        for old, new in pairwise(evidence)
        if start <= new.at < end and old.state == "off" and new.state == "on"
    )
    day_usage = [
        DayUsage(date=day.isoformat(), on_seconds=on, unknown_seconds=unknown)
        for day, (on, unknown) in days.items()
    ]
    if device_kind == "switch":
        return SwitchUsage(
            device_kind="switch",
            entity_id=entity_id,
            on_seconds=on_seconds,
            unknown_seconds=unknown_seconds,
            switch_ons=switch_ons,
            days=day_usage,
            hours=hours,
            unknown_intervals=unknown_intervals,
            periods=[
                SwitchPeriod(start=period.start, end=period.end, state=period.state)
                for period in periods
            ],
        )
    return LightUsage(
        device_kind="light",
        entity_id=entity_id,
        on_seconds=on_seconds,
        unknown_seconds=unknown_seconds,
        switch_ons=switch_ons,
        average_brightness=weighted_brightness / brightness_seconds if brightness_seconds else None,
        days=day_usage,
        hours=hours,
        unknown_intervals=unknown_intervals,
        periods=periods,
    )
