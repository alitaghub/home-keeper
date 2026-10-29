"""Usage report: on-time, switch-ons and unknown time per light or switch, from recorded evidence.

Each test drives the app's clock forward and records snapshots, change events and coverage gaps
through the internal ingest endpoints at the moment the collector would send them, so seeded
evidence passes the same validation as live data.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from httpx import Response
from support.api import AUTH, CATALOG, CATALOG_ENTITIES, TOKEN, Payload, select

from home_keeper.api import create_app

HOUR = 3600.0
PLUG: Payload = {
    "id": "plug-a",
    "device_id": "device-plug-a",
    "device_name": "Kettle plug",
    "name": "Kettle",
    "device_kind": "switch",
    "supported": True,
    "reason": None,
}
type Query = str | list[str]


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@dataclass
class Home:
    client: TestClient
    clock: list[datetime]
    revision: int

    def at(self, when: datetime) -> None:
        assert when >= self.clock[0], "the collector's clock only moves forward"
        self.clock[0] = when

    def post(self, path: str, payload: Payload) -> None:
        response = self.client.post(path, json=payload, headers=AUTH)
        assert response.is_success, response.text

    def snapshot(self, entity: str, state: str, **attributes: object) -> None:
        now = self.clock[0].isoformat()
        self.post(
            "/internal/baselines",
            {
                "entity_id": entity,
                "selection_revision": self.revision,
                "source_id": "demo",
                "state": state,
                "availability": "available",
                "received_at": now,
                "last_changed": now,
                "last_updated": now,
                "last_reported": now,
                **attributes,
            },
        )

    def change(
        self, entity: str, old: Payload, new: Payload, event_time: datetime | None = None
    ) -> None:
        happened = event_time or self.clock[0]
        self.post(
            "/internal/events",
            {
                "id": f"{entity}-{int(happened.timestamp())}",
                "entity_id": entity,
                "source_id": "demo",
                "selection_revision": self.revision,
                "event_time": happened.isoformat(),
                "received_at": self.clock[0].isoformat(),
                "old": old,
                "new": new,
            },
        )

    def interrupt(self, entity: str | None = None) -> None:
        payload: Payload = {
            "source_id": "demo",
            "status": "interrupted",
            "received_at": self.clock[0].isoformat(),
        }
        self.post("/internal/coverage", payload | ({"entity_id": entity} if entity else {}))

    def recover(self, entity: str | None = None) -> None:
        payload: Payload = {
            "source_id": "demo",
            "status": "recovered",
            "received_at": self.clock[0].isoformat(),
            "selection_revision": self.revision,
        }
        self.post("/internal/coverage", payload | ({"entity_id": entity} if entity else {}))

    def usage_response(self, start: datetime, end: datetime, **params: Query) -> Response:
        query: dict[str, Query] = {"start": start.isoformat(), "end": end.isoformat()} | params
        return self.client.get("/api/usage", params=query)

    def usage(self, start: datetime, end: datetime, **params: Query) -> dict[str, Any]:
        response = self.usage_response(start, end, **params)
        assert response.status_code == 200, response.text
        report: dict[str, Any] = response.json()
        return report

    def devices(self, start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
        return {device["entity_id"]: device for device in self.usage(start, end)["devices"]}


@contextmanager
def home(
    tmp_path: Path,
    lights: tuple[str, ...] = ("bulb-a",),
    *,
    clock: datetime,
    timezone: str = "UTC",
    retention_days: int = 30,
) -> Generator[Home]:
    time = [clock]
    app = create_app(
        tmp_path / "usage.sqlite",
        TOKEN,
        now=lambda: time[0],
        timezone=ZoneInfo(timezone),
        retention_days=retention_days,
    )
    with TestClient(app) as client:
        catalog = CATALOG | {"entities": [*CATALOG_ENTITIES, PLUG]}
        assert client.post("/internal/catalog", json=catalog, headers=AUTH).is_success
        revision = 0
        for light in lights:
            revision = select(client, light)
        yield Home(client, time, revision)


def hours(**seconds: float) -> list[float]:
    """24 local-hour buckets, zero except the named ones (h0..h23)."""
    buckets = [0.0] * 24
    for name, value in seconds.items():
        buckets[int(name.removeprefix("h"))] = value
    return buckets


def interval(start: datetime, end: datetime, reason: str) -> dict[str, Any]:
    return {"start": start, "end": end, "reason": reason}


def unknown(light: dict[str, Any]) -> list[dict[str, Any]]:
    """Unknown intervals with parsed instants, so tests do not depend on offset formatting."""
    return [
        interval(
            datetime.fromisoformat(entry["start"]),
            datetime.fromisoformat(entry["end"]),
            entry["reason"],
        )
        for entry in light["unknown_intervals"]
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


def test_light_on_before_start_counts_from_start_and_only_observed_off_to_on_is_a_switch_on(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on", brightness=200)
        house.at(utc(10, 11))
        house.change("bulb-a", {"state": "on", "brightness": 200}, {"state": "off"})
        house.at(utc(10, 12))
        house.change("bulb-a", {"state": "off"}, {"state": "on", "brightness": 200})
        house.at(utc(10, 12, 30))
        # A brightness change on a light that is already on is not a switch-on.
        house.change(
            "bulb-a", {"state": "on", "brightness": 200}, {"state": "on", "brightness": 100}
        )
        house.at(utc(10, 13))
        house.change("bulb-a", {"state": "on", "brightness": 100}, {"state": "off"})
        house.at(utc(10, 15))

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


def test_gap_scoped_to_one_light_is_unknown_for_it_only_and_no_switch_on_is_inferred_across_it(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "off")
        house.snapshot("bulb-b", "on")
        house.at(utc(10, 10, 30))
        house.change("bulb-a", {"state": "off"}, {"state": "on"})
        house.at(utc(10, 10, 45))
        house.change("bulb-a", {"state": "on"}, {"state": "off"})
        house.at(utc(10, 11))
        # Last confirmation of bulb-a is 10:45, so its unknown time starts there.
        house.interrupt("bulb-a")
        house.at(utc(10, 12))
        # Off before the gap and on after it: the switch-on in between was never observed.
        house.snapshot("bulb-a", "on")
        house.recover("bulb-a")
        house.at(utc(10, 13))
        house.change("bulb-a", {"state": "on"}, {"state": "off"})
        house.at(utc(10, 15))

        lights = house.devices(utc(10, 10), utc(10, 14))

    assert list(lights) == ["bulb-a", "bulb-b"]
    a, b = lights["bulb-a"], lights["bulb-b"]
    assert a["on_seconds"] == 0.25 * HOUR + HOUR
    assert a["unknown_seconds"] == 1.25 * HOUR
    assert a["switch_ons"] == 1
    assert a["hours"] == hours(h10=0.25 * HOUR, h12=HOUR)
    assert unknown(a) == [interval(utc(10, 10, 45), utc(10, 12), "gap")]
    assert b["on_seconds"] == 4 * HOUR
    assert b["unknown_seconds"] == 0
    assert b["switch_ons"] == 0
    assert b["unknown_intervals"] == []


def test_open_whole_home_gap_is_unknown_from_last_confirmation_through_end(tmp_path: Path) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on")
        house.snapshot("bulb-b", "off")
        house.at(utc(10, 10, 30))
        house.snapshot("bulb-a", "on")
        house.snapshot("bulb-b", "off")
        house.at(utc(10, 11))
        house.interrupt()
        house.at(utc(10, 12, 30))

        lights = house.devices(utc(10, 10), utc(10, 12))

    a, b = lights["bulb-a"], lights["bulb-b"]
    assert a["on_seconds"] == 0.5 * HOUR
    assert a["unknown_seconds"] == 1.5 * HOUR
    assert unknown(a) == [interval(utc(10, 10, 30), utc(10, 12), "gap")]
    assert a["days"] == [
        {"date": "2026-10-10", "on_seconds": 0.5 * HOUR, "unknown_seconds": 1.5 * HOUR}
    ]
    assert b["on_seconds"] == 0
    assert b["unknown_seconds"] == 1.5 * HOUR
    assert unknown(b) == [interval(utc(10, 10, 30), utc(10, 12), "gap")]


def test_recovered_gap_stays_unknown_until_a_snapshot_or_event_gives_state(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on")
        house.snapshot("bulb-b", "on")
        house.at(utc(10, 10, 30))
        house.snapshot("bulb-a", "on")
        house.snapshot("bulb-b", "on")
        house.at(utc(10, 11))
        house.interrupt()
        house.at(utc(10, 11, 30))
        # Recovery without fresh state: the pre-gap "on" must not carry over.
        house.recover()
        house.at(utc(10, 12, 30))
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 13))
        house.change("bulb-b", {"state": "on"}, {"state": "off"})
        house.at(utc(10, 15))

        lights = house.devices(utc(10, 10), utc(10, 14))

    a, b = lights["bulb-a"], lights["bulb-b"]
    assert a["on_seconds"] == 0.5 * HOUR + 1.5 * HOUR
    assert a["unknown_seconds"] == 2 * HOUR
    # Time after recovery until fresh state is still the gap's doing, so it merges into one.
    assert unknown(a) == [interval(utc(10, 10, 30), utc(10, 12, 30), "gap")]
    assert a["switch_ons"] == 0
    assert b["on_seconds"] == 0.5 * HOUR
    assert b["unknown_seconds"] == 2.5 * HOUR
    assert unknown(b) == [interval(utc(10, 10, 30), utc(10, 13), "gap")]


def test_snapshot_is_placed_at_receipt_so_a_late_snapshot_cannot_rewrite_earlier_time(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        # Home Assistant reports when the state last changed, which can be long before receipt.
        house.snapshot(
            "bulb-a", "on", last_changed="2020-01-01T00:00:00Z", last_updated="2020-01-01T00:00:00Z"
        )
        house.at(utc(10, 10))
        house.change("bulb-a", {"state": "on"}, {"state": "off"})
        house.at(utc(10, 11))
        house.interrupt("bulb-a")
        before = house.devices(utc(10, 9), utc(10, 11))["bulb-a"]
        house.at(utc(10, 12))
        # The recovery snapshot claims an old change time; it still only speaks from 12:00.
        house.snapshot(
            "bulb-a",
            "off",
            last_changed="2021-01-01T00:00:00Z",
            last_updated="2021-01-01T00:00:00Z",
        )
        house.recover("bulb-a")
        house.at(utc(10, 13))

        after = house.devices(utc(10, 9), utc(10, 11))["bulb-a"]
        whole = house.devices(utc(10, 9), utc(10, 13))["bulb-a"]

    assert before["on_seconds"] == HOUR
    assert unknown(before) == [interval(utc(10, 10), utc(10, 11), "gap")]
    assert after == before
    assert whole["on_seconds"] == HOUR
    assert whole["unknown_seconds"] == 2 * HOUR
    assert unknown(whole) == [interval(utc(10, 10), utc(10, 12), "gap")]


def test_unavailable_and_unknown_states_are_unknown_time_and_never_a_switch_on(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "off")
        for when, old, new in (
            (utc(10, 10), "off", "unavailable"),
            (utc(10, 11), "unavailable", "on"),
            (utc(10, 12), "on", "unknown"),
            (utc(10, 12, 30), "unknown", "off"),
            (utc(10, 13), "off", "on"),
        ):
            house.at(when)
            house.change("bulb-a", {"state": old}, {"state": new})
        house.at(utc(10, 15))

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


def test_gap_with_empty_entity_ids_makes_every_light_unknown(tmp_path: Path) -> None:
    # A gap recorded before anything was selected lists no lights, which means all of them.
    with home(tmp_path, (), clock=utc(10, 7)) as house:
        house.interrupt()
        [gap] = house.client.get("/api/coverage").json()["gaps"]
        assert gap["entity_ids"] == []
        select(house.client, "bulb-a")
        house.revision = select(house.client, "bulb-b")
        house.at(utc(10, 8))
        house.snapshot("bulb-a", "on")
        house.snapshot("bulb-b", "on")
        house.at(utc(10, 11))

        lights = house.devices(utc(10, 9), utc(10, 11))

    for light in lights.values():
        assert light["on_seconds"] == 0
        assert light["unknown_seconds"] == 2 * HOUR
        assert unknown(light) == [interval(utc(10, 9), utc(10, 11), "gap")]
    assert list(lights) == ["bulb-a", "bulb-b"]


def test_time_before_earliest_evidence_or_retention_is_unknown_without_evidence(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(12, 6), retention_days=1) as house:
        # Delivered late but retained: the event is old evidence, yet older than retention.
        house.change("bulb-a", {"state": "off"}, {"state": "on"}, event_time=utc(10, 20))
        house.at(utc(12, 8))
        house.snapshot("bulb-b", "on")
        house.at(utc(12, 12))

        report = house.usage(utc(11, 0), utc(12, 12))

    assert datetime.fromisoformat(report["retained_since"]) == utc(11, 12)
    a, b = report["devices"]
    assert a["on_seconds"] == 24 * HOUR
    assert a["unknown_seconds"] == 12 * HOUR
    assert a["switch_ons"] == 0
    assert unknown(a) == [interval(utc(11, 0), utc(11, 12), "no_evidence")]
    assert a["days"] == [
        {"date": "2026-10-11", "on_seconds": 12 * HOUR, "unknown_seconds": 12 * HOUR},
        {"date": "2026-10-12", "on_seconds": 12 * HOUR, "unknown_seconds": 0},
    ]
    assert b["on_seconds"] == 4 * HOUR
    assert b["unknown_seconds"] == 32 * HOUR
    assert unknown(b) == [interval(utc(11, 0), utc(12, 8), "no_evidence")]
    assert b["days"] == [
        {"date": "2026-10-11", "on_seconds": 0, "unknown_seconds": 24 * HOUR},
        {"date": "2026-10-12", "on_seconds": 4 * HOUR, "unknown_seconds": 8 * HOUR},
    ]


def test_on_time_crossing_local_midnight_splits_by_local_day_and_hour(tmp_path: Path) -> None:
    # Copenhagen is UTC+2 in mid-October: on from 22:30 to 01:15 local time.
    with home(tmp_path, clock=utc(13, 17), timezone="Europe/Copenhagen") as house:
        house.snapshot("bulb-a", "off")
        house.at(utc(13, 20, 30))
        house.change("bulb-a", {"state": "off"}, {"state": "on"})
        house.at(utc(13, 23, 15))
        house.change("bulb-a", {"state": "on"}, {"state": "off"})
        house.at(utc(14, 2))

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


def test_switch_usage_splits_local_days_and_hours_keeps_gaps_unknown_and_has_no_brightness(
    tmp_path: Path,
) -> None:
    # Copenhagen is UTC+2 in mid-October. Local times: on 23:30, gap 00:15-01:00, off 01:45.
    switch: Payload = {"device_kind": "switch"}
    with home(
        tmp_path, ("bulb-a", "plug-a"), clock=utc(13, 17), timezone="Europe/Copenhagen"
    ) as house:
        house.snapshot("bulb-a", "on", brightness=100)
        house.snapshot("plug-a", "off", **switch)
        house.at(utc(13, 21, 30))
        house.change("plug-a", {"state": "off"} | switch, {"state": "on"} | switch)
        house.at(utc(13, 22, 15))
        house.snapshot("plug-a", "on", **switch)
        house.at(utc(13, 22, 30))
        # Last confirmation of the plug is 22:15, so its unknown time starts there.
        house.interrupt("plug-a")
        house.at(utc(13, 23))
        # On before the gap and after it: no switch-on is inferred across it.
        house.snapshot("plug-a", "on", **switch)
        house.recover("plug-a")
        house.at(utc(13, 23, 45))
        house.change("plug-a", {"state": "on"} | switch, {"state": "off"} | switch)
        house.at(utc(14, 1))

        report = house.usage(utc(13, 21), utc(14, 0))

    devices = {device["entity_id"]: device for device in report.get("devices", [])}
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
    assert unknown(plug) == [interval(utc(13, 22, 15), utc(13, 23), "gap")]
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
    with home(tmp_path, clock=utc(24, 21), timezone="Europe/Copenhagen") as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(26, 0))

        [light] = house.usage(day_start, day_end)["devices"]

    assert light["on_seconds"] == 25 * HOUR
    assert light["days"] == [{"date": "2026-10-25", "on_seconds": 25 * HOUR, "unknown_seconds": 0}]
    assert light["hours"] == [2 * HOUR if hour == 2 else HOUR for hour in range(24)]


def test_average_brightness_is_weighted_by_on_time_and_null_when_never_known(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-a", "bulb-b"), clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on", brightness=200)
        house.snapshot("bulb-b", "on")
        house.at(utc(10, 11, 30))
        house.change(
            "bulb-a", {"state": "on", "brightness": 200}, {"state": "on", "brightness": 50}
        )
        house.at(utc(10, 12))
        house.change("bulb-a", {"state": "on", "brightness": 50}, {"state": "off"})
        house.at(utc(10, 15))

        lights = house.devices(utc(10, 10), utc(10, 14))

    # 1.5 h at 200 and 0.5 h at 50; off time does not dilute the average.
    assert lights["bulb-a"]["average_brightness"] == 162.5
    assert lights["bulb-a"]["switch_ons"] == 0
    assert lights["bulb-b"]["on_seconds"] == 4 * HOUR
    assert lights["bulb-b"]["average_brightness"] is None


def test_lights_are_filtered_to_selected_ids_in_order_and_reversed_range_is_rejected(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("bulb-b", "bulb-a"), clock=utc(10, 8)) as house:
        house.at(utc(10, 12))
        start, end = utc(10, 9), utc(10, 11)

        def ids(**params: Query) -> list[str]:
            return [light["entity_id"] for light in house.usage(start, end, **params)["devices"]]

        assert ids() == ["bulb-a", "bulb-b"]
        assert ids(entity_id=["bulb-b"]) == ["bulb-b"]
        select(house.client, "bulb-b", selected=False)
        assert ids() == ["bulb-a"]
        assert ids(entity_id=["bulb-b", "room", "missing", "bulb-a"]) == ["bulb-a"]
        assert ids(entity_id=["bulb-b"]) == []
        assert house.usage_response(end, start).status_code == 422
        assert house.client.get("/api/usage", params={"end": end.isoformat()}).status_code == 422


def test_snapshot_read_before_a_newer_event_does_not_override_it(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 9)) as house:
        house.snapshot("bulb-a", "off")
        house.at(utc(10, 10, 1))
        house.change("bulb-a", {"state": "off"}, {"state": "on"})
        house.at(utc(10, 10, 2))
        # This read started at 10:00, before the 10:01 change, and arrived after it.
        house.snapshot(
            "bulb-a",
            "off",
            collection_started_at=utc(10, 10).isoformat(),
            last_changed=utc(10, 9).isoformat(),
            last_updated=utc(10, 9).isoformat(),
            last_reported=utc(10, 10).isoformat(),
        )
        house.at(utc(10, 12))

        light = house.devices(utc(10, 9), utc(10, 11))["bulb-a"]

    assert light["on_seconds"] == 59 * 60
    assert light["unknown_seconds"] == 0


def test_silent_collector_is_a_gap_without_first_asking_for_health(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 8, 30))
        house.post(
            "/internal/health",
            {"source_id": "demo", "status": "healthy", "received_at": house.clock[0].isoformat()},
        )
        house.at(utc(10, 11, 30))

        first = house.devices(utc(10, 8), utc(10, 11, 30))["bulb-a"]
        assert house.client.get("/api/health").json()["status"] == "stale"
        after_health = house.devices(utc(10, 8), utc(10, 11, 30))["bulb-a"]

    assert first["on_seconds"] == 0.5 * HOUR
    assert unknown(first) == [interval(utc(10, 8, 30), utc(10, 11, 30), "gap")]
    assert first == after_health


def test_range_longer_than_366_days_is_rejected(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        end = utc(10, 8)
        assert house.usage_response(end - timedelta(days=366), end).status_code == 200
        too_long = house.usage_response(end - timedelta(days=366, seconds=1), end)
        assert too_long.status_code == 422


def test_time_after_now_is_unknown_without_evidence(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 9)) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 12))

        light = house.devices(utc(10, 10), utc(10, 14))["bulb-a"]

    assert light["on_seconds"] == 2 * HOUR
    assert light["unknown_seconds"] == 2 * HOUR
    assert unknown(light) == [interval(utc(10, 12), utc(10, 14), "no_evidence")]
    assert light["hours"] == hours(h10=HOUR, h11=HOUR)


def test_steady_light_stays_known_after_its_only_snapshot_leaves_retention(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8), retention_days=1) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(12, 8))

        report = house.usage(utc(11, 8), utc(12, 8))

    assert datetime.fromisoformat(report["retained_since"]) == utc(11, 8)
    [light] = report["devices"]
    assert light["on_seconds"] == 24 * HOUR
    assert light["unknown_seconds"] == 0


def test_switch_on_at_a_range_edge_belongs_to_the_range_that_starts_there(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "off")
        house.at(utc(10, 10))
        house.change("bulb-a", {"state": "off"}, {"state": "on"})
        house.at(utc(10, 12))

        before = house.devices(utc(10, 9), utc(10, 10))["bulb-a"]
        after = house.devices(utc(10, 10), utc(10, 11))["bulb-a"]

    assert before["switch_ons"] == 0
    assert before["on_seconds"] == 0
    assert after["switch_ons"] == 1
    assert after["on_seconds"] == HOUR


def test_quarter_hour_offset_zone_splits_hours_and_days_at_local_boundaries(
    tmp_path: Path,
) -> None:
    # Kathmandu is UTC+05:45: 18:00-18:30 UTC is 23:45-00:15 local time.
    with home(tmp_path, clock=utc(13, 17), timezone="Asia/Kathmandu") as house:
        house.snapshot("bulb-a", "off")
        house.at(utc(13, 18))
        house.change("bulb-a", {"state": "off"}, {"state": "on"})
        house.at(utc(13, 18, 30))
        house.change("bulb-a", {"state": "on"}, {"state": "off"})
        house.at(utc(13, 20))

        light = house.devices(utc(13, 17), utc(13, 19))["bulb-a"]

    assert light["on_seconds"] == 1800
    assert light["hours"] == hours(h23=900, h0=900)
    assert light["days"] == [
        {"date": "2026-10-13", "on_seconds": 900, "unknown_seconds": 0},
        {"date": "2026-10-14", "on_seconds": 900, "unknown_seconds": 0},
    ]


def test_evidence_from_one_recovery_does_not_resolve_a_touching_later_gap(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 9)) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 10))
        house.interrupt("bulb-a")
        house.at(utc(10, 11))
        house.snapshot("bulb-a", "on")
        house.recover("bulb-a")
        house.at(utc(10, 12))
        # Starts at the 11:00 recovery snapshot, the last confirmation of bulb-a.
        house.interrupt("bulb-a")
        house.at(utc(10, 13))
        house.recover("bulb-a")
        house.at(utc(10, 15))

        light = house.devices(utc(10, 9), utc(10, 14))["bulb-a"]

    assert light["on_seconds"] == 0
    assert light["unknown_seconds"] == 5 * HOUR
    assert unknown(light) == [interval(utc(10, 9), utc(10, 14), "gap")]


def test_open_gap_wins_over_retention_and_future_unknown_reasons(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8), retention_days=1) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 9))
        house.interrupt("bulb-a")
        house.at(utc(12, 12))

        report = house.usage(utc(10, 8), utc(12, 14))

    assert datetime.fromisoformat(report["retained_since"]) == utc(11, 12)
    [light] = report["devices"]
    assert light["on_seconds"] == 0
    assert light["unknown_seconds"] == 54 * HOUR
    assert unknown(light) == [interval(utc(10, 8), utc(12, 14), "gap")]


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


def test_expired_recovered_gap_does_not_turn_unknown_time_into_on_time(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8), retention_days=1) as house:
        house.snapshot("bulb-a", "on")
        house.at(utc(10, 9))
        house.interrupt()
        house.at(utc(10, 9, 30))
        # Recovery without fresh state: the pre-gap "on" must not carry over.
        house.recover()
        house.at(utc(10, 10))
        before_expiry = house.usage(utc(10, 10), utc(10, 11))["devices"][0]
        house.at(utc(12, 8))

        report = house.usage(utc(11, 8), utc(12, 8))

    assert before_expiry["on_seconds"] == 0
    [light] = report["devices"]
    assert light["on_seconds"] == 0, light
    assert light["unknown_seconds"] == 24 * HOUR


def test_a_selected_plug_stays_a_switch_after_discovery_drops_it_before_any_reading(
    tmp_path: Path,
) -> None:
    with home(tmp_path, ("plug-a",), clock=utc(13, 12)) as house:
        catalog = CATALOG | {"entities": CATALOG_ENTITIES}
        house.post("/internal/catalog", catalog)
        house.at(utc(13, 14))

        monitored = house.client.get("/api/monitored").json()["entities"]
        plug = house.devices(utc(13, 12), utc(13, 14))["plug-a"]

    assert [e["device_kind"] for e in monitored if e["id"] == "plug-a"] == ["switch"]
    assert plug["device_kind"] == "switch"
    assert "average_brightness" not in plug


def test_switch_readings_name_the_kind_after_a_kindless_entry_is_reselected(
    tmp_path: Path,
) -> None:
    switch: Payload = {"device_kind": "switch"}
    # bulb-b's catalog entry names no kind; only its readings say it is a switch.
    with home(tmp_path, ("bulb-b",), clock=utc(13, 12)) as house:
        house.snapshot("bulb-b", "on", **switch)
        house.at(utc(13, 13))
        # Reselecting clears the current state until the next reading arrives.
        select(house.client, "bulb-b", selected=False)
        house.revision = select(house.client, "bulb-b")
        house.at(utc(13, 14))

        device = house.devices(utc(13, 12), utc(13, 14))["bulb-b"]

    assert device["device_kind"] == "switch"
    assert "average_brightness" not in device


def test_periods_and_unknown_intervals_tile_the_range_and_equal_stretches_merge(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on", brightness=200)
        house.at(utc(10, 11))
        house.change("bulb-a", {"state": "on", "brightness": 200}, {"state": "off"})
        house.at(utc(10, 11, 30))
        # A snapshot that confirms the state does not split the period.
        house.snapshot("bulb-a", "off")
        house.at(utc(10, 12))
        house.change("bulb-a", {"state": "off"}, {"state": "on", "brightness": 200})
        house.at(utc(10, 12, 30))
        house.change(
            "bulb-a", {"state": "on", "brightness": 200}, {"state": "on", "brightness": 100}
        )
        house.at(utc(10, 13))
        house.change("bulb-a", {"state": "on", "brightness": 100}, {"state": "unavailable"})
        house.at(utc(10, 13, 30))
        house.change("bulb-a", {"state": "unavailable"}, {"state": "off"})
        house.at(utc(10, 15))

        [light] = house.usage(utc(10, 10), utc(10, 14))["devices"]

    def on(start: datetime, end: datetime, brightness: int) -> dict[str, object]:
        return {"start": start, "end": end, "state": "on", "brightness": brightness}

    def off(start: datetime, end: datetime) -> dict[str, object]:
        return {"start": start, "end": end, "state": "off", "brightness": None}

    assert periods(light) == [
        on(utc(10, 10), utc(10, 11), 200),
        off(utc(10, 11), utc(10, 12)),
        on(utc(10, 12), utc(10, 12, 30), 200),
        on(utc(10, 12, 30), utc(10, 13), 100),
        off(utc(10, 13, 30), utc(10, 14)),
    ]
    assert unknown(light) == [interval(utc(10, 13), utc(10, 13, 30), "unavailable")]


def test_switch_periods_carry_no_brightness_and_leave_gaps_to_unknown_intervals(
    tmp_path: Path,
) -> None:
    switch: Payload = {"device_kind": "switch"}
    with home(tmp_path, ("plug-a",), clock=utc(10, 8)) as house:
        house.snapshot("plug-a", "on", **switch)
        house.at(utc(10, 11))
        # The gap starts at this last confirmation.
        house.snapshot("plug-a", "on", **switch)
        house.interrupt("plug-a")
        house.at(utc(10, 12))
        house.snapshot("plug-a", "off", **switch)
        house.recover("plug-a")
        house.at(utc(10, 15))

        [plug] = house.usage(utc(10, 10), utc(10, 14))["devices"]

    assert periods(plug) == [
        {"start": utc(10, 10), "end": utc(10, 11), "state": "on"},
        {"start": utc(10, 12), "end": utc(10, 14), "state": "off"},
    ]
    assert unknown(plug) == [interval(utc(10, 11), utc(10, 12), "gap")]


def test_equal_states_on_both_sides_of_a_gap_stay_two_periods(tmp_path: Path) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        house.snapshot("bulb-a", "on", brightness=200)
        house.at(utc(10, 11))
        # The gap starts at this last confirmation.
        house.snapshot("bulb-a", "on", brightness=200)
        house.interrupt("bulb-a")
        house.at(utc(10, 12))
        house.snapshot("bulb-a", "on", brightness=200)
        house.recover("bulb-a")
        house.at(utc(10, 15))

        [light] = house.usage(utc(10, 10), utc(10, 14))["devices"]

    assert periods(light) == [
        {"start": utc(10, 10), "end": utc(10, 11), "state": "on", "brightness": 200},
        {"start": utc(10, 12), "end": utc(10, 14), "state": "on", "brightness": 200},
    ]
    assert unknown(light) == [interval(utc(10, 11), utc(10, 12), "gap")]


def test_time_before_a_devices_first_record_is_no_evidence_even_after_a_gap(
    tmp_path: Path,
) -> None:
    with home(tmp_path, clock=utc(10, 8)) as house:
        # Collection stops before bulb-a has any record, then recovers.
        house.interrupt()
        house.at(utc(10, 9))
        house.recover()
        house.at(utc(10, 10))
        house.snapshot("bulb-a", "off")
        house.at(utc(10, 12))

        [light] = house.usage(utc(10, 8), utc(10, 12))["devices"]

    assert unknown(light) == [
        interval(utc(10, 8), utc(10, 9), "gap"),
        interval(utc(10, 9), utc(10, 10), "no_evidence"),
    ]
