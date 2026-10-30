"""Usage report: on-time, switch-ons and unknown time per light or switch, from Home
Assistant's history.

Each test records rows in Home Assistant's recorder through a fake reader, then asks
`/api/usage`. A row is evidence from its time until the next row; the row the reader returns
first is Home Assistant's state at the window start.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from support.api import BROWSER, TOKEN
from support.reader import FakeReader, Payload, history_row

from home_keeper.api import create_app
from home_keeper.models import Entity
from home_keeper.reader import ReaderError

HOUR = 3600.0
MS = timedelta(milliseconds=1)
BULB_A = Entity(
    id="bulb-a",
    device_id="device-a",
    device_name="Hue bulb",
    name="Bulb A",
    device_kind="light",
    supported=True,
)
BULB_B = Entity(
    id="bulb-b",
    device_id="device-b",
    device_name="Second bulb",
    name="Bulb B",
    device_kind="light",
    supported=True,
)
PLUG = Entity(
    id="plug-a",
    device_id="device-plug-a",
    device_name="Kettle plug",
    name="Kettle",
    device_kind="switch",
    supported=True,
)
SOURCES = {BULB_A.id: "light.bulb_a", BULB_B.id: "light.bulb_b", PLUG.id: "switch.kettle"}
REPORT_KEYS = {"start", "end", "timezone", "devices"}
UNREACHABLE: list[ReaderError] = ["reader_unreachable", "connection", "authentication", "timeout"]
type Query = str | list[str]
type Row = tuple[datetime, Payload]


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def on(**attributes: object) -> Payload:
    return {"state": "on", **attributes}


OFF: Payload = {"state": "off"}


@dataclass
class Home:
    client: TestClient
    reader: FakeReader
    clock: list[datetime]

    def at(self, when: datetime) -> None:
        self.clock[0] = when

    def select(self, entity_id: str, *, selected: bool = True) -> None:
        response = self.client.put(
            f"/api/selection/{entity_id}", json={"selected": selected}, headers=BROWSER
        )
        assert response.status_code == 200, response.text

    def record(self, entity_id: str, *rows: Row) -> None:
        """Home Assistant's recorder holds each (time, state) of the device, in order."""
        kind: Payload = {"device_kind": "switch"} if entity_id == PLUG.id else {}
        for at, state in rows:
            self.reader.record(entity_id, history_row(SOURCES[entity_id], at, state | kind))

    def usage_response(self, start: datetime, end: datetime, **params: Query) -> Response:
        query: dict[str, Query] = {"start": start.isoformat(), "end": end.isoformat()} | params
        return self.client.get("/api/usage", params=query)

    def usage(self, start: datetime, end: datetime, **params: Query) -> dict[str, Any]:
        response = self.usage_response(start, end, **params)
        assert response.status_code == 200, response.text
        report: dict[str, Any] = response.json()
        assert set(report) == REPORT_KEYS, report
        return report

    def devices(self, start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
        return {device["entity_id"]: device for device in self.usage(start, end)["devices"]}

    def asked(self) -> tuple[list[str], datetime, datetime]:
        """The entity IDs, sorted, and the window of the last history read."""
        entity_ids, start, end = self.reader.history_reads[-1]
        return sorted(entity_ids), start, end


@contextmanager
def home(
    tmp_path: Path,
    devices: tuple[str, ...] = ("bulb-a",),
    *,
    clock: datetime,
    timezone: str = "UTC",
) -> Generator[Home]:
    """Home Assistant has two bulbs and a plug; `devices` are monitored, in that order, and
    the recorder holds nothing yet."""
    time = [clock]
    reader = FakeReader([BULB_A, BULB_B, PLUG])
    app = create_app(
        tmp_path / "usage.sqlite",
        TOKEN,
        now=lambda: time[0],
        timezone=ZoneInfo(timezone),
        reader=reader,
    )
    with TestClient(app) as client:
        house = Home(client, reader, time)
        for device in devices:
            house.select(device)
        yield house


def hours(**seconds: float) -> list[float]:
    """24 local-hour buckets, zero except the named ones (h0..h23)."""
    buckets = [0.0] * 24
    for name, value in seconds.items():
        buckets[int(name.removeprefix("h"))] = value
    return buckets


def interval(start: datetime, end: datetime, reason: str) -> dict[str, Any]:
    return {"start": start, "end": end, "reason": reason}


def unknown(device: dict[str, Any]) -> list[dict[str, Any]]:
    """Unknown intervals with parsed instants, so tests do not depend on offset formatting."""
    return [
        interval(
            datetime.fromisoformat(entry["start"]),
            datetime.fromisoformat(entry["end"]),
            entry["reason"],
        )
        for entry in device["unknown_intervals"]
    ]


def periods(device: dict[str, Any]) -> list[dict[str, Any]]:
    """Known periods with parsed instants, so tests do not depend on offset formatting."""
    return [
        entry
        | {
            "start": datetime.fromisoformat(entry["start"]),
            "end": datetime.fromisoformat(entry["end"]),
        }
        for entry in device["periods"]
    ]


def test_light_on_before_start_counts_from_start_and_only_off_to_on_is_a_switch_on(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 15)) as house:
        house.record(
            "bulb-a",
            (utc(10, 8), on(brightness=200)),
            (utc(10, 11), OFF),
            (utc(10, 12), on(brightness=200)),
            # A brightness change on a light that is already on is not a switch-on.
            (utc(10, 12, 30), on(brightness=100)),
            (utc(10, 13), OFF),
        )

        report = house.usage(utc(10, 10), utc(10, 14))

    assert datetime.fromisoformat(report["start"]) == utc(10, 10)
    assert datetime.fromisoformat(report["end"]) == utc(10, 14)
    assert report["timezone"] == "UTC"
    [light] = report["devices"]
    assert light["entity_id"] == "bulb-a"
    assert light["on_seconds"] == 2 * HOUR
    assert light["unknown_seconds"] == 0
    assert light["switch_ons"] == 1
    assert light["days"] == [{"date": "2026-10-10", "on_seconds": 2 * HOUR, "unknown_seconds": 0}]
    assert light["hours"] == hours(h10=HOUR, h12=HOUR)
    assert light["unknown_intervals"] == []


def test_usage_reads_home_assistant_history_from_1_ms_before_start_until_now(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-b", "bulb-a"), clock=utc(10, 12)) as house:
        house.usage(utc(10, 10), utc(10, 11))
        assert house.asked() == (["bulb-a", "bulb-b"], utc(10, 10) - MS, utc(10, 11))

        # The window reaches past now: Home Assistant is asked only up to now.
        house.usage(utc(10, 10), utc(10, 14))
        assert house.asked() == (["bulb-a", "bulb-b"], utc(10, 10) - MS, utc(10, 12))


def test_unavailable_and_unknown_states_are_unknown_time_and_never_a_switch_on(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 15)) as house:
        house.record(
            "bulb-a",
            (utc(10, 8), OFF),
            (utc(10, 10), {"state": "unavailable"}),
            (utc(10, 11), on()),
            (utc(10, 12), {"state": "unknown"}),
            (utc(10, 12, 30), OFF),
            (utc(10, 13), on()),
        )

        light = house.devices(utc(10, 10), utc(10, 14))["bulb-a"]

    assert light["on_seconds"] == 2 * HOUR
    assert light["unknown_seconds"] == 1.5 * HOUR
    # Coming back from unavailable is not an observed off-to-on change.
    assert light["switch_ons"] == 1
    assert unknown(light) == [
        interval(utc(10, 10), utc(10, 11), "unavailable"),
        interval(utc(10, 12), utc(10, 12, 30), "unavailable"),
    ]
    assert light["days"] == [
        {"date": "2026-10-10", "on_seconds": 2 * HOUR, "unknown_seconds": 1.5 * HOUR}
    ]
    assert light["hours"] == hours(h11=HOUR, h13=HOUR)


def test_time_before_the_first_row_is_no_history_and_the_first_row_is_no_switch_on(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(12, 12)) as house:
        # Home Assistant's history of bulb-a starts inside the window.
        house.record("bulb-a", (utc(11, 12), on()))
        house.record("bulb-b", (utc(12, 8), on()))

        report = house.usage(utc(11, 0), utc(12, 12))

    a, b = report["devices"]
    assert a["on_seconds"] == 24 * HOUR
    assert a["unknown_seconds"] == 12 * HOUR
    assert a["switch_ons"] == 0
    assert unknown(a) == [interval(utc(11, 0), utc(11, 12), "no_history")]
    assert a["days"] == [
        {"date": "2026-10-11", "on_seconds": 12 * HOUR, "unknown_seconds": 12 * HOUR},
        {"date": "2026-10-12", "on_seconds": 12 * HOUR, "unknown_seconds": 0},
    ]
    assert b["on_seconds"] == 4 * HOUR
    assert b["unknown_seconds"] == 32 * HOUR
    assert b["switch_ons"] == 0
    assert unknown(b) == [interval(utc(11, 0), utc(12, 8), "no_history")]
    assert b["days"] == [
        {"date": "2026-10-11", "on_seconds": 0, "unknown_seconds": 24 * HOUR},
        {"date": "2026-10-12", "on_seconds": 4 * HOUR, "unknown_seconds": 8 * HOUR},
    ]


def test_a_device_without_history_stays_in_the_report_with_the_whole_window_no_history(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 15)) as house:
        house.record("bulb-a", (utc(10, 8), on()))

        lights = house.devices(utc(10, 10), utc(10, 14))

    assert list(lights) == ["bulb-a", "bulb-b"]
    b = lights["bulb-b"]
    assert b["device_kind"] == "light"
    assert b["on_seconds"] == 0
    assert b["unknown_seconds"] == 4 * HOUR
    assert b["switch_ons"] == 0
    assert b["average_brightness"] is None
    assert b["hours"] == hours()
    assert b["days"] == [{"date": "2026-10-10", "on_seconds": 0, "unknown_seconds": 4 * HOUR}]
    assert unknown(b) == [interval(utc(10, 10), utc(10, 14), "no_history")]


def test_a_light_on_since_long_before_start_is_on_the_whole_window(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(12, 8)) as house:
        # Home Assistant's state at the window start is a row from days earlier.
        house.record("bulb-a", (utc(1, 8), on()))

        light = house.devices(utc(11, 8), utc(12, 8))["bulb-a"]

    assert light["on_seconds"] == 24 * HOUR
    assert light["unknown_seconds"] == 0
    assert light["switch_ons"] == 0


def test_on_time_crossing_local_midnight_splits_by_local_day_and_hour(tmp_path: Path) -> None:
    # Copenhagen is UTC+2 in mid-October: on from 22:30 to 01:15 local time.
    with home(tmp_path, clock=utc(14, 2), timezone="Europe/Copenhagen") as house:
        house.record("bulb-a", (utc(13, 17), OFF), (utc(13, 20, 30), on()), (utc(13, 23, 15), OFF))

        report = house.usage(utc(13, 18), utc(14, 1))

    assert report["timezone"] == "Europe/Copenhagen"
    [light] = report["devices"]
    assert light["on_seconds"] == 2.75 * HOUR
    assert light["unknown_seconds"] == 0
    assert light["switch_ons"] == 1
    assert light["days"] == [
        {"date": "2026-10-13", "on_seconds": 1.5 * HOUR, "unknown_seconds": 0},
        {"date": "2026-10-14", "on_seconds": 1.25 * HOUR, "unknown_seconds": 0},
    ]
    assert light["hours"] == hours(h22=0.5 * HOUR, h23=HOUR, h0=HOUR, h1=0.25 * HOUR)


def test_switch_usage_splits_local_days_and_hours_keeps_unavailable_time_unknown_and_has_no_brightness(  # noqa: E501 -- the name states the behavior
    tmp_path: Path,
) -> None:
    # Copenhagen is UTC+2 in mid-October. Local times: on 23:30, unavailable 00:15-01:00,
    # off 01:45.
    with home(
        tmp_path, ("bulb-a", "plug-a"), clock=utc(14, 1), timezone="Europe/Copenhagen"
    ) as house:
        house.record("bulb-a", (utc(13, 17), on(brightness=100)))
        house.record(
            "plug-a",
            (utc(13, 17), OFF),
            (utc(13, 21, 30), on()),
            (utc(13, 22, 15), {"state": "unavailable"}),
            # On before the unavailable stretch and after it: no switch-on is inferred.
            (utc(13, 23), on()),
            (utc(13, 23, 45), OFF),
        )

        report = house.usage(utc(13, 21), utc(14, 0))

    devices = {device["entity_id"]: device for device in report["devices"]}
    assert list(devices) == ["bulb-a", "plug-a"], report
    plug, bulb = devices["plug-a"], devices["bulb-a"]
    assert plug["device_kind"] == "switch"
    assert "average_brightness" not in plug
    assert plug["on_seconds"] == 1.5 * HOUR
    assert plug["unknown_seconds"] == 0.75 * HOUR
    assert plug["switch_ons"] == 1
    assert plug["days"] == [
        {"date": "2026-10-13", "on_seconds": 0.5 * HOUR, "unknown_seconds": 0},
        {"date": "2026-10-14", "on_seconds": HOUR, "unknown_seconds": 0.75 * HOUR},
    ]
    assert plug["hours"] == hours(h23=0.5 * HOUR, h0=0.25 * HOUR, h1=0.75 * HOUR)
    assert unknown(plug) == [interval(utc(13, 22, 15), utc(13, 23), "unavailable")]
    # A light in the same report keeps every light field.
    assert bulb["device_kind"] == "light"
    assert bulb["on_seconds"] == 3 * HOUR
    assert bulb["unknown_seconds"] == 0
    assert bulb["average_brightness"] == 100
    assert bulb["hours"] == hours(h23=HOUR, h0=HOUR, h1=HOUR)


def test_local_day_with_daylight_saving_end_has_25_hours_and_a_doubled_hour(
    tmp_path: Path,
) -> None:
    # On 2026-10-25 Copenhagen falls back from 03:00 CEST to 02:00 CET: 02:00-03:00 runs twice.
    zone = ZoneInfo("Europe/Copenhagen")
    day_start = datetime(2026, 10, 25, tzinfo=zone)
    day_end = datetime(2026, 10, 26, tzinfo=zone)
    with home(tmp_path, clock=utc(26, 0), timezone="Europe/Copenhagen") as house:
        house.record("bulb-a", (utc(24, 21), on()))

        [light] = house.usage(day_start, day_end)["devices"]

    assert light["on_seconds"] == 25 * HOUR
    assert light["days"] == [{"date": "2026-10-25", "on_seconds": 25 * HOUR, "unknown_seconds": 0}]
    assert light["hours"] == [2 * HOUR if hour == 2 else HOUR for hour in range(24)]


def test_quarter_hour_offset_zone_splits_hours_and_days_at_local_boundaries(
    tmp_path: Path,
) -> None:
    # Kathmandu is UTC+05:45: 18:00-18:30 UTC is 23:45-00:15 local time.
    with home(tmp_path, clock=utc(13, 20), timezone="Asia/Kathmandu") as house:
        house.record("bulb-a", (utc(13, 17), OFF), (utc(13, 18), on()), (utc(13, 18, 30), OFF))

        light = house.devices(utc(13, 17), utc(13, 19))["bulb-a"]

    assert light["on_seconds"] == 1800
    assert light["unknown_seconds"] == 0
    assert light["hours"] == hours(h23=900, h0=900)
    assert light["days"] == [
        {"date": "2026-10-13", "on_seconds": 900, "unknown_seconds": 0},
        {"date": "2026-10-14", "on_seconds": 900, "unknown_seconds": 0},
    ]


def test_average_brightness_is_weighted_by_on_time_and_null_when_never_known(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 15)) as house:
        house.record(
            "bulb-a",
            (utc(10, 8), on(brightness=200)),
            (utc(10, 11, 30), on(brightness=50)),
            (utc(10, 12), OFF),
        )
        house.record("bulb-b", (utc(10, 8), on()))

        lights = house.devices(utc(10, 10), utc(10, 14))

    # 1.5 h at 200 and 0.5 h at 50; off time does not dilute the average.
    assert lights["bulb-a"]["average_brightness"] == 162.5
    assert lights["bulb-a"]["switch_ons"] == 0
    assert lights["bulb-b"]["on_seconds"] == 4 * HOUR
    assert lights["bulb-b"]["average_brightness"] is None


def test_devices_are_filtered_to_selected_ids_in_order_and_reversed_range_is_rejected(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-b", "bulb-a"), clock=utc(10, 12)) as house:
        start, end = utc(10, 9), utc(10, 11)

        def ids(**params: Query) -> list[str]:
            return [device["entity_id"] for device in house.usage(start, end, **params)["devices"]]

        assert ids() == ["bulb-a", "bulb-b"]
        assert ids(entity_id=["bulb-b"]) == ["bulb-b"]
        house.select("bulb-b", selected=False)
        assert ids() == ["bulb-a"]
        # Home Assistant is never asked for a device that is not monitored.
        assert "bulb-b" not in house.asked()[0]
        assert ids(entity_id=["bulb-b", "room", "missing", "bulb-a"]) == ["bulb-a"]
        assert ids(entity_id=["bulb-b"]) == []
        assert house.usage_response(end, start).status_code == 422
        assert house.client.get("/api/usage", params={"end": end.isoformat()}).status_code == 422


def test_range_longer_than_366_days_is_rejected(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        end = utc(10, 8)
        assert house.usage_response(end - timedelta(days=366), end).status_code == 200
        too_long = house.usage_response(end - timedelta(days=366, seconds=1), end)
        assert too_long.status_code == 422


def test_time_after_now_is_never_on_or_off(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 12)) as house:
        house.record("bulb-a", (utc(10, 9), on()))

        light = house.devices(utc(10, 10), utc(10, 14))["bulb-a"]

    assert light["on_seconds"] == 2 * HOUR
    assert light["unknown_seconds"] == 2 * HOUR
    assert unknown(light) == [interval(utc(10, 12), utc(10, 14), "no_history")]
    assert light["hours"] == hours(h10=HOUR, h11=HOUR)


def test_switch_on_at_a_range_edge_belongs_to_the_range_that_starts_there(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 12)) as house:
        house.record("bulb-a", (utc(10, 8), OFF), (utc(10, 10), on()))

        before = house.devices(utc(10, 9), utc(10, 10))["bulb-a"]
        after = house.devices(utc(10, 10), utc(10, 11))["bulb-a"]

    assert before["switch_ons"] == 0
    assert before["on_seconds"] == 0
    assert before["unknown_seconds"] == 0
    # Counted once in the window that starts at the switch-on, not twice.
    assert after["switch_ons"] == 1
    assert after["on_seconds"] == HOUR
    assert after["unknown_seconds"] == 0


def test_range_at_the_edge_of_representable_time_is_rejected(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        # Normalizing +01:00 to UTC moves this start before year 1.
        response = house.client.get(
            "/api/usage",
            params={"start": "0001-01-01T00:00:00+01:00", "end": "0001-01-01T00:00:00Z"},
        )
        assert response.status_code == 422
        # The last local day would end after the largest date Python can represent.
        response = house.client.get(
            "/api/usage",
            params={"start": "9999-12-31T00:00:00Z", "end": "9999-12-31T12:00:00Z"},
        )
        assert response.status_code == 422


def test_a_selected_plug_stays_a_switch_after_home_assistant_drops_it(tmp_path: Path) -> None:
    with home(tmp_path, ("plug-a",), clock=utc(13, 14)) as house:
        house.reader.drop("plug-a")

        plug = house.devices(utc(13, 12), utc(13, 14))["plug-a"]

    assert plug["device_kind"] == "switch"
    assert "average_brightness" not in plug
    assert unknown(plug) == [interval(utc(13, 12), utc(13, 14), "no_history")]


@pytest.mark.parametrize("error", UNREACHABLE)
def test_usage_is_a_503_with_a_safe_error_while_home_assistant_cannot_be_read(
    tmp_path: Path, error: ReaderError
) -> None:
    with home(tmp_path, clock=utc(10, 12)) as house:
        house.reader.error = error

        response = house.usage_response(utc(10, 10), utc(10, 11))

    assert response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == error
    assert body["message"]
    assert "http" not in body["message"].lower()
    assert TOKEN not in body["message"]


def test_periods_and_unknown_intervals_tile_the_range_and_equal_stretches_merge(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 15)) as house:
        house.record(
            "bulb-a",
            (utc(10, 8), on(brightness=200)),
            (utc(10, 11), OFF),
            # A row that repeats the state does not split the period.
            (utc(10, 11, 30), OFF),
            (utc(10, 12), on(brightness=200)),
            (utc(10, 12, 30), on(brightness=100)),
            (utc(10, 13), {"state": "unavailable"}),
            (utc(10, 13, 30), OFF),
        )

        [light] = house.usage(utc(10, 10), utc(10, 14))["devices"]

    def lit(start: datetime, end: datetime, brightness: int) -> dict[str, object]:
        return {"start": start, "end": end, "state": "on", "brightness": brightness}

    def dark(start: datetime, end: datetime) -> dict[str, object]:
        return {"start": start, "end": end, "state": "off", "brightness": None}

    assert periods(light) == [
        lit(utc(10, 10), utc(10, 11), 200),
        dark(utc(10, 11), utc(10, 12)),
        lit(utc(10, 12), utc(10, 12, 30), 200),
        lit(utc(10, 12, 30), utc(10, 13), 100),
        dark(utc(10, 13, 30), utc(10, 14)),
    ]
    assert unknown(light) == [interval(utc(10, 13), utc(10, 13, 30), "unavailable")]


def test_switch_periods_carry_no_brightness_and_leave_unknown_time_to_unknown_intervals(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("plug-a",), clock=utc(10, 15)) as house:
        house.record(
            "plug-a",
            (utc(10, 8), on()),
            (utc(10, 11), {"state": "unavailable"}),
            (utc(10, 12), OFF),
        )

        [plug] = house.usage(utc(10, 9), utc(10, 14))["devices"]

    assert periods(plug) == [
        {"start": utc(10, 9), "end": utc(10, 11), "state": "on"},
        {"start": utc(10, 12), "end": utc(10, 14), "state": "off"},
    ]
    assert unknown(plug) == [interval(utc(10, 11), utc(10, 12), "unavailable")]


def test_equal_states_on_both_sides_of_unavailable_time_stay_two_periods(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 15)) as house:
        house.record(
            "bulb-a",
            (utc(10, 8), on(brightness=200)),
            (utc(10, 11), {"state": "unavailable"}),
            (utc(10, 12), on(brightness=200)),
        )

        [light] = house.usage(utc(10, 10), utc(10, 14))["devices"]

    assert periods(light) == [
        {"start": utc(10, 10), "end": utc(10, 11), "state": "on", "brightness": 200},
        {"start": utc(10, 12), "end": utc(10, 14), "state": "on", "brightness": 200},
    ]
    assert unknown(light) == [interval(utc(10, 11), utc(10, 12), "unavailable")]


def test_time_without_history_is_in_no_period(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 12)) as house:
        house.record("bulb-a", (utc(10, 10), OFF))

        [light] = house.usage(utc(10, 8), utc(10, 14))["devices"]

    assert periods(light) == [
        {"start": utc(10, 10), "end": utc(10, 12), "state": "off", "brightness": None}
    ]
    assert unknown(light) == [
        interval(utc(10, 8), utc(10, 10), "no_history"),
        interval(utc(10, 12), utc(10, 14), "no_history"),
    ]
