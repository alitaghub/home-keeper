"""Measurements: what a monitored power meter or thermometer reports now, and Home Assistant's
long-term statistics for it, through `/api/measurements`.

Each test records buckets in Home Assistant's long-term statistics through a fake reader, then
asks the API. A bucket is one period (5 minutes or an hour); a period with no bucket is a gap.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from support.api import BROWSER, NOW, TOKEN
from support.reader import FakeReader, Payload, history_row, live, measuring
from support.statistics import bucket

from home_keeper.api import create_app
from home_keeper.models import Entity

POWER = Entity(
    id="power-a",
    device_id="device-plug",
    device_name="Desk plug",
    name="Desk plug power",
    device_kind="measurement",
    supported=True,
)
TEMPERATURE = Entity(
    id="temp-a",
    device_id="device-hall",
    device_name="Hall sensor",
    name="Hall temperature",
    device_kind="measurement",
    supported=True,
)
NOTE = Entity(
    id="note-a",
    device_id="device-note",
    device_name="Notes",
    name="Note",
    device_kind="state",
    supported=True,
)
BULB = Entity(
    id="bulb-a",
    device_id="device-bulb",
    device_name="Hue bulb",
    name="Bulb",
    device_kind="light",
    supported=True,
)
SOURCES = {
    POWER.id: "sensor.desk_plug_power",
    TEMPERATURE.id: "sensor.hall_temperature",
    NOTE.id: "sensor.note",
    BULB.id: "light.bulb",
}
SERIES_KEYS = {"entity_id", "unit", "period", "buckets", "min", "mean", "max", "latest"}
BUCKET_KEYS = {"start", "end", "mean", "min", "max"}
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# Every reading a monitored device has now, so each device can be told apart.
READINGS: dict[str, Payload] = {
    POWER.id: measuring(412),
    TEMPERATURE.id: measuring(21.5, "°C"),
    NOTE.id: {"device_kind": "state", "state": "hello"},
    BULB.id: {"state": "on"},
}


@dataclass
class Home:
    client: TestClient
    reader: FakeReader

    def select(self, entity_id: str, *, selected: bool = True) -> None:
        response = self.client.put(
            f"/api/selection/{entity_id}", json={"selected": selected}, headers=BROWSER
        )
        assert response.status_code == 200, response.text

    def measurements_response(
        self, start: datetime, end: datetime, entity_ids: list[str] | None = None
    ) -> Response:
        params: dict[str, str | list[str]] = {"start": start.isoformat(), "end": end.isoformat()}
        if entity_ids is not None:
            params["entity_id"] = entity_ids
        return self.client.get("/api/measurements", params=params)

    def measurements(
        self, start: datetime, end: datetime, entity_ids: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """The report's series by entity ID."""
        response = self.measurements_response(start, end, entity_ids)
        assert response.status_code == 200, response.text
        report: dict[str, Any] = response.json()
        assert set(report) == {"start", "end", "devices"}, report
        assert datetime.fromisoformat(report["start"]) == start
        assert datetime.fromisoformat(report["end"]) == end
        for series in report["devices"]:
            assert set(series) == SERIES_KEYS, series
            assert all(set(entry) == BUCKET_KEYS for entry in series["buckets"]), series
        return {series["entity_id"]: series for series in report["devices"]}


@contextmanager
def home(database: str, monitored: tuple[Entity, ...] = (POWER,)) -> Generator[Home]:
    """Home Assistant has a power meter, a thermometer, a text sensor and a bulb, each
    reporting a reading now; `monitored` are monitored; its statistics hold nothing yet."""
    reader = FakeReader(
        [POWER, TEMPERATURE, NOTE, BULB],
        {entity: live(SOURCES[entity], reading) for entity, reading in READINGS.items()},
    )
    app = create_app(database, TOKEN, now=lambda: NOW, reader=reader)
    with TestClient(app) as client:
        house = Home(client, reader)
        for entity in monitored:
            house.select(entity.id)
        yield house


def starts(series: dict[str, Any]) -> list[datetime]:
    return [datetime.fromisoformat(entry["start"]) for entry in series["buckets"]]


def test_a_week_of_hourly_buckets_leaves_gaps_empty_and_gives_the_exact_overall_numbers(
    database: str,
) -> None:
    start, end = NOW - 7 * DAY, NOW
    with home(database) as house:
        house.reader.record_statistics(
            POWER.id,
            bucket(NOW - 6 * HOUR, mean=10, low=5, high=20),
            bucket(NOW - 5 * HOUR, mean=20, low=8, high=40),
            # Nothing for four and three hours ago: Home Assistant had no statistics.
            bucket(NOW - 2 * HOUR, mean=60, low=1, high=90),
            # Older than the range: left out.
            bucket(NOW - 8 * DAY, mean=1000, low=1000, high=1000),
        )

        series = house.measurements(start, end)[POWER.id]

    assert series["period"] == "hour"
    assert series["unit"] == "W"
    # Only the buckets Home Assistant has: a gap is not a zero bucket.
    assert starts(series) == [NOW - 6 * HOUR, NOW - 5 * HOUR, NOW - 2 * HOUR]
    first = series["buckets"][0]
    assert (first["mean"], first["min"], first["max"]) == (10.0, 5.0, 20.0)
    assert datetime.fromisoformat(first["end"]) == NOW - 5 * HOUR
    # Minimum of the minimums, maximum of the maximums and mean of the means: with a zero for
    # each gap hour the mean would be lower.
    assert (series["min"], series["mean"], series["max"]) == (1.0, 30.0, 90.0)
    # The current reading, not the last bucket.
    assert series["latest"] == 412.0
    assert house.reader.statistics_reads == [([POWER.id], start, end)]


@pytest.mark.parametrize(
    ("span", "period"),
    [
        (HOUR, "5minute"),
        (DAY, "5minute"),
        (DAY + timedelta(seconds=1), "hour"),
        (7 * DAY, "hour"),
        (31 * DAY, "hour"),
    ],
)
def test_the_period_is_5_minutes_up_to_24_hours_and_an_hour_above(
    database: str, span: timedelta, period: str
) -> None:
    with home(database) as house:
        series = house.measurements(NOW - span, NOW)[POWER.id]

    assert series["period"] == period


def test_a_range_without_buckets_has_an_empty_chart_and_null_numbers_not_zeros(
    database: str,
) -> None:
    with home(database) as house:
        series = house.measurements(NOW - DAY, NOW)[POWER.id]

    assert series["buckets"] == []
    assert (series["min"], series["mean"], series["max"]) == (None, None, None)
    # The meter still reports now.
    assert (series["latest"], series["unit"]) == (412.0, "W")


@pytest.mark.parametrize(
    "reading",
    [
        pytest.param(measuring(None), id="unavailable"),
        pytest.param(measuring(None, state="unknown"), id="unknown"),
        pytest.param(None, id="Home Assistant has no state"),
    ],
)
def test_latest_is_null_when_the_device_has_no_number_now_but_statistics_stay(
    database: str, reading: dict[str, object] | None
) -> None:
    with home(database) as house:
        house.reader.record_statistics(POWER.id, bucket(NOW - HOUR, mean=5, low=4, high=6))
        if reading is None:
            house.reader.live.pop(POWER.id)
        else:
            house.reader.set(POWER.id, SOURCES[POWER.id], reading)

        series = house.measurements(NOW - DAY, NOW)[POWER.id]

    assert series["latest"] is None
    assert (series["min"], series["mean"], series["max"]) == (4.0, 5.0, 6.0)
    assert len(series["buckets"]) == 1


def test_only_monitored_measurements_are_reported_each_with_its_own_unit(database: str) -> None:
    with home(database, (POWER, TEMPERATURE, NOTE, BULB)) as house:
        house.select(TEMPERATURE.id, selected=False)
        house.reader.record_statistics(POWER.id, bucket(NOW - HOUR, mean=5, low=4, high=6))
        house.reader.record_statistics(TEMPERATURE.id, bucket(NOW - HOUR, mean=21, low=20, high=22))

        everything = house.measurements(NOW - DAY, NOW)
        asked = {entity for ids, _, _ in house.reader.statistics_reads for entity in ids}
        # Naming an unmonitored measurement, a light, a text sensor or an unknown ID leaves it out.
        named = house.measurements(
            NOW - DAY, NOW, [TEMPERATURE.id, BULB.id, NOTE.id, "ghost", POWER.id]
        )

    assert list(everything) == [POWER.id]
    assert list(named) == [POWER.id]
    assert asked == {POWER.id}


def test_every_monitored_measurement_is_reported_and_entity_id_narrows_the_report(
    database: str,
) -> None:
    with home(database, (POWER, TEMPERATURE)) as house:
        house.reader.record_statistics(TEMPERATURE.id, bucket(NOW - HOUR, mean=21, low=20, high=22))

        everything = house.measurements(NOW - DAY, NOW)
        only_temperature = house.measurements(NOW - DAY, NOW, [TEMPERATURE.id])

    assert sorted(everything) == [POWER.id, TEMPERATURE.id]
    assert everything[TEMPERATURE.id]["unit"] == "°C"
    assert everything[TEMPERATURE.id]["latest"] == 21.5
    assert list(only_temperature) == [TEMPERATURE.id]


def test_a_range_of_31_days_is_read_and_a_longer_or_reversed_one_is_a_422_that_reads_nothing(
    database: str,
) -> None:
    with home(database) as house:
        assert house.measurements_response(NOW - 31 * DAY, NOW).status_code == 200
        reads = len(house.reader.statistics_reads)

        too_long = house.measurements_response(NOW - 31 * DAY - timedelta(seconds=1), NOW)
        reversed_range = house.measurements_response(NOW, NOW - HOUR)
        too_many = house.measurements_response(NOW - DAY, NOW, [f"id-{n}" for n in range(501)])
        missing = house.client.get("/api/measurements")

        assert too_long.status_code == 422
        assert reversed_range.status_code == 422
        assert too_many.status_code == 422
        assert missing.status_code == 422
        assert len(house.reader.statistics_reads) == reads


def test_measurements_are_a_503_with_a_safe_code_while_statistics_cannot_be_read(
    database: str,
) -> None:
    with home(database) as house:
        house.reader.statistics_error = "timeout"

        response = house.measurements_response(NOW - DAY, NOW)

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"] == "timeout"
    assert TOKEN not in response.text


def test_usage_leaves_measurements_out_and_never_reads_their_history(database: str) -> None:
    with home(database, (POWER, BULB)) as house:
        house.reader.record(
            BULB.id, history_row(SOURCES[BULB.id], NOW - HOUR, {"state": "on", "brightness": 10})
        )
        window = {"start": (NOW - DAY).isoformat(), "end": NOW.isoformat()}

        everything = house.client.get("/api/usage", params=window)
        named = house.client.get("/api/usage", params=[*window.items(), ("entity_id", POWER.id)])

    assert [device["entity_id"] for device in everything.json()["devices"]] == [BULB.id]
    assert named.json()["devices"] == []
    assert all(POWER.id not in ids for ids, _, _ in house.reader.history_reads)


def test_a_state_device_the_catalog_now_calls_a_measurement_becomes_one_without_selecting_again(
    database: str,
) -> None:
    sensor = POWER.model_copy(update={"device_kind": "state"})
    with home(database, ()) as house:
        house.reader.entities = [sensor, NOTE]
        house.reader.set(POWER.id, SOURCES[POWER.id], {"device_kind": "state", "state": "412"})
        house.select(POWER.id)
        monitored = house.client.get("/api/monitored").json()["entities"][0]
        assert monitored["device_kind"] == "state"
        assert house.measurements(NOW - DAY, NOW) == {}

        # The catalog now sorts it as a measurement; nobody selects it again.
        house.reader.entities = [POWER, NOTE]
        house.reader.set(POWER.id, SOURCES[POWER.id], measuring(412))
        house.reader.record_statistics(POWER.id, bucket(NOW - HOUR, mean=400, low=390, high=410))

        monitored = house.client.get("/api/monitored").json()["entities"][0]
        series = house.measurements(NOW - DAY, NOW)

        assert monitored["device_kind"] == "measurement"
        assert monitored["state"]["device_kind"] == "measurement"
        assert (monitored["state"]["value"], monitored["state"]["unit"]) == (412.0, "W")
        assert list(series) == [POWER.id]
        assert series[POWER.id]["mean"] == 400.0

        # The kind kept for when Home Assistant cannot be read is the new one.
        house.reader.error = "connection"
        kept = house.client.get("/api/monitored").json()["entities"][0]
        assert kept["device_kind"] == "measurement"
