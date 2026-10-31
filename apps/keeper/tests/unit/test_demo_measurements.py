"""The demo home's power meter: a measurement with a value now and deterministic statistics."""

from datetime import UTC, datetime, timedelta

from home_keeper.models import Bucket
from home_keeper.reader_service import DemoBackend

METER = "sensor.desk_plug_power"


def hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def test_the_demo_catalog_has_a_supported_power_meter_measurement_on_a_device() -> None:
    [meter] = [entity for entity in DemoBackend().catalog() if entity.id == METER]

    assert meter.device_kind == "measurement"
    assert meter.supported
    assert meter.device_id is not None
    assert meter.name == "Desk plug power"


def test_the_demo_power_meter_reads_a_number_of_watts_now() -> None:
    live = DemoBackend().states([METER])[METER]

    assert live is not None
    assert live.source_entity_id == METER
    reading = live.reading.model_dump()
    assert reading["device_kind"] == "measurement"
    assert isinstance(reading["value"], float)
    assert reading["unit"] == "W"
    assert reading["availability"] == "available"


def test_a_day_has_5_minute_buckets_and_a_week_or_a_month_hourly_ones() -> None:
    end = hour_floor(datetime.now(UTC))
    backend = DemoBackend()
    expected = [
        (timedelta(hours=24), timedelta(minutes=5), 200),
        (timedelta(days=7), timedelta(hours=1), 24 * 6),
        (timedelta(days=30), timedelta(hours=1), 24 * 25),
    ]

    for span, length, at_least in expected:
        buckets = backend.statistics([METER], end - span, end)[METER]

        assert len(buckets) >= at_least, (span, len(buckets))
        starts = [datetime.fromisoformat(bucket.start) for bucket in buckets]
        for bucket, start in zip(buckets, starts, strict=True):
            assert end - span <= start < end
            assert datetime.fromisoformat(bucket.end) - start == length
            # Buckets start on the period's own boundaries, as Home Assistant's do.
            assert (start - end) % length == timedelta(0)
            assert bucket.min <= bucket.mean <= bucket.max
        # Oldest first, none twice.
        assert starts == sorted(set(starts))


def test_the_statistics_of_a_bucket_do_not_change_between_reads_or_demo_instances() -> None:
    end = hour_floor(datetime.now(UTC))
    start = end - timedelta(days=7)

    def read(backend: DemoBackend, begin: datetime) -> list[Bucket]:
        return backend.statistics([METER], begin, end)[METER]

    first = DemoBackend()
    whole = read(first, start)
    # The same range again, and another instance of the demo, give the same numbers.
    assert read(first, start) == whole
    assert read(DemoBackend(), start) == whole
    # A narrower range gives the same buckets as the same part of the wider one.
    later = start + timedelta(days=3)
    assert read(first, later) == [
        bucket for bucket in whole if datetime.fromisoformat(bucket.start) >= later
    ]


def test_a_device_that_is_not_a_measurement_or_not_in_the_demo_has_no_statistics() -> None:
    end = hour_floor(datetime.now(UTC))

    result = DemoBackend().statistics(
        ["light.hue_desk", "sensor.nowhere"], end - timedelta(days=7), end
    )

    assert result == {"light.hue_desk": [], "sensor.nowhere": []}
