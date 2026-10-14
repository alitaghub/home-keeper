"""How long selected lights were on, from stored evidence only.

Time the evidence cannot vouch for is unknown, never on or off.
"""

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import accumulate, pairwise
from zoneinfo import ZoneInfo

from home_observer.models import (
    BaselineRecord,
    CoverageGap,
    DayUsage,
    EventReading,
    EventRecord,
    LightStateValue,
    LightUsage,
    UnknownInterval,
    UnknownReason,
)
from home_observer.store import SnapshotRead, event_outranks_snapshot


@dataclass(frozen=True)
class Evidence:
    """A state the light had from `at` until the next evidence."""

    at: datetime
    # At equal times a recorded change wins over a snapshot.
    rank: int
    state: LightStateValue
    brightness: int | None
    received_at: datetime
    # Snapshots only: Home Assistant's source time and the HTTP read, for the ingestion rule.
    read: tuple[datetime, SnapshotRead] | None


@dataclass(frozen=True)
class Known:
    on: bool
    brightness: int | None


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def reading_evidence(reading: BaselineRecord | EventReading) -> Evidence:
    received = instant(reading.received_at)
    if isinstance(reading, EventReading):
        return Evidence(
            instant(reading.event_time), 1, reading.state, reading.brightness, received, None
        )
    # A snapshot proves the state when it was read, so a later snapshot never rewrites an
    # earlier window, whatever source time Home Assistant gave it.
    started = instant(reading.collection_started_at) if reading.collection_started_at else received
    source = instant(reading.last_updated) if reading.last_updated else received
    read = (source, SnapshotRead(started, received))
    return Evidence(received, 0, reading.state, reading.brightness, received, read)


def event_evidence(event: EventRecord) -> Evidence:
    # A change without a new state means Home Assistant dropped the entity.
    return Evidence(
        instant(event.event_time),
        1,
        event.new.state if event.new else "unknown",
        event.new.brightness if event.new else None,
        instant(event.received_at),
        None,
    )


def timeline(evidence: Sequence[Evidence]) -> list[Evidence]:
    """Order evidence by placement, dropping snapshots ingestion would not let replace an event."""
    kept: list[Evidence] = []
    for item in sorted(evidence, key=lambda entry: (entry.at, entry.rank)):
        previous = kept[-1] if kept else None
        if (
            item.read is not None
            and previous is not None
            and previous.read is None
            and event_outranks_snapshot(previous.received_at, previous.at, *item.read)
        ):
            continue
        kept.append(item)
    return kept


def light_gaps(
    entity_id: str, gaps: Sequence[CoverageGap], end: datetime
) -> list[tuple[datetime, datetime]]:
    """Gap windows for one light, from last confirmation to recovery (or `end` while open)."""
    windows = (
        (
            instant(gap.last_confirmed_at or gap.detected_at),
            instant(gap.recovered_at) if gap.recovered_at else end,
        )
        for gap in gaps
        # An empty scope means the whole collection stopped, as /api/coverage reads it.
        if not gap.entity_ids or entity_id in gap.entity_ids
    )
    return [(gap_start, gap_end) for gap_start, gap_end in windows if gap_start < gap_end]


def merged(windows: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """The time covered by any of the windows, as sorted, non-overlapping windows."""
    covered: list[tuple[datetime, datetime]] = []
    for gap_start, gap_end in sorted(windows):
        if covered and gap_start <= covered[-1][1]:
            covered[-1] = (covered[-1][0], max(covered[-1][1], gap_end))
        else:
            covered.append((gap_start, gap_end))
    return covered


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


def light_usage(
    entity_id: str,
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    timezone: ZoneInfo,
    retained_since: datetime,
    readings: Sequence[BaselineRecord | EventReading],
    events: Sequence[EventRecord],
    gaps: Sequence[CoverageGap],
) -> LightUsage:
    own_events = [event for event in events if event.entity_id == entity_id]
    evidence = timeline(
        [reading_evidence(reading) for reading in readings if reading.entity_id == entity_id]
        + [event_evidence(event) for event in own_events]
    )
    keys = [(item.at, item.rank) for item in evidence]
    own_gaps = light_gaps(entity_id, gaps, end)
    windows = merged(own_gaps)
    gap_starts = [gap_start for gap_start, _ in windows]
    # Each gap needs evidence placed after its own start, so a recovery that resolved one gap
    # cannot resolve a later one it touches. By end time: the latest start of any ended gap.
    by_end = sorted(own_gaps, key=lambda window: window[1])
    gap_ends = [gap_end for _, gap_end in by_end]
    latest_starts = list(accumulate((gap_start for gap_start, _ in by_end), max))

    def status(moment: datetime) -> Known | UnknownReason:
        # A gap wins over the retention cutoff and the future: collection was known to be down.
        covering = bisect_right(gap_starts, moment) - 1
        if covering >= 0 and moment < windows[covering][1]:
            return "gap"
        if moment < retained_since or moment >= now:
            return "no_evidence"
        ended = bisect_right(gap_ends, moment) - 1
        resumed = latest_starts[ended] if ended >= 0 else None
        index = bisect_right(keys, (moment, 1)) - 1
        # After a gap, only evidence placed after it began says what the light did.
        if index < 0 or (resumed is not None and evidence[index].at <= resumed):
            return "no_evidence" if resumed is None else "gap"
        item = evidence[index]
        if item.state not in ("on", "off"):
            return "unavailable"
        return Known(item.state == "on", item.brightness)

    boundaries = sorted(
        {start, end}
        | {
            moment
            for moment in (
                retained_since,
                now,
                *(item.at for item in evidence),
                *(edge for window in windows for edge in window),
            )
            if start < moment < end
        }
    )
    days = {day: [0.0, 0.0] for day in local_dates(start, end, timezone)}
    hours = [0.0] * 24
    on_seconds = unknown_seconds = brightness_seconds = weighted_brightness = 0.0
    unknown_intervals: list[UnknownInterval] = []
    for segment_start, segment_end in pairwise(boundaries):
        current = status(segment_start)
        seconds = (segment_end - segment_start).total_seconds()
        if isinstance(current, Known):
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

    return LightUsage(
        entity_id=entity_id,
        on_seconds=on_seconds,
        unknown_seconds=unknown_seconds,
        # Only observed off-to-on changes count; none are inferred at the range start or a gap.
        switch_ons=sum(
            1
            for event in own_events
            if start <= instant(event.event_time) < end
            and event.old is not None
            and event.new is not None
            and event.old.state == "off"
            and event.new.state == "on"
        ),
        average_brightness=weighted_brightness / brightness_seconds if brightness_seconds else None,
        days=[
            DayUsage(date=day.isoformat(), on_seconds=on, unknown_seconds=unknown)
            for day, (on, unknown) in days.items()
        ],
        hours=hours,
        unknown_intervals=unknown_intervals,
    )
