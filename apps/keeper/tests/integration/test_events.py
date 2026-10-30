"""History: `/api/events` lists the changes Home Assistant's recorder holds for monitored
devices, read through the reader on each request.

A change is a pair of consecutive recorder rows of one device whose state, availability,
brightness or color differ; its time is Home Assistant's time for the later row.
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support.api import BROWSER, NOW, TOKEN
from support.reader import FakeReader, Payload, history_row

from home_keeper.api import create_app
from home_keeper.models import Entity
from home_keeper.reader import ReaderError

DESK = Entity(
    id="ha:desk",
    device_id="ha:desk-device",
    device_name="Desk lamp",
    name="Desk",
    device_kind="light",
    supported=True,
)
LAMP = Entity(
    id="ha:lamp",
    device_id="ha:lamp-device",
    device_name="Floor lamp",
    name="Lamp",
    device_kind="light",
    supported=True,
)
KETTLE = Entity(
    id="ha:kettle",
    device_id="ha:kettle-device",
    device_name="Kettle plug",
    name="Kettle",
    device_kind="switch",
    supported=True,
)
SOURCES = {DESK.id: "light.desk", LAMP.id: "light.lamp", KETTLE.id: "switch.kettle"}
CHANGE_KEYS = {"id", "entity_id", "source_entity_id", "device_kind", "time", "old", "new"}
T0 = NOW - timedelta(hours=6)
OFF: Payload = {"state": "off"}
UNREACHABLE: list[ReaderError] = ["reader_unreachable", "connection", "authentication", "timeout"]


def minute(n: float) -> datetime:
    return T0 + timedelta(minutes=n)


def time_of(change: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(change["time"])


@dataclass
class House:
    client: TestClient
    reader: FakeReader
    clock: list[datetime]

    def select(self, entity_id: str, *, selected: bool = True) -> None:
        response = self.client.put(
            f"/api/selection/{entity_id}", json={"selected": selected}, headers=BROWSER
        )
        assert response.status_code == 200, response.text

    def record(self, entity_id: str, *rows: tuple[datetime, Mapping[str, object]]) -> None:
        """Home Assistant recorded each (time, state) of the device, in order."""
        source = SOURCES[entity_id]
        state_since: tuple[str, datetime] | None = None
        for at, state in rows:
            # Home Assistant keeps last_changed when only attributes change.
            if state_since is None or state_since[0] != state["state"]:
                state_since = (str(state["state"]), at)
            payload: Payload = {
                **state,
                **({"device_kind": "switch"} if entity_id == KETTLE.id else {}),
            }
            self.reader.record(entity_id, history_row(source, at, payload, state_since[1]))

    def page(self, **params: str | int) -> dict[str, Any]:
        response = self.client.get("/api/events", params=params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert set(body) == {"events", "next_cursor"}, body
        for change in body["events"]:
            assert set(change) == CHANGE_KEYS, change
        return body

    def changes(self, **params: str | int) -> list[dict[str, Any]]:
        return self.page(**params)["events"]

    def every_page(self, **params: str | int) -> list[list[dict[str, Any]]]:
        """Follow `next_cursor` from the newest page to the last."""
        pages = [self.page(**params)]
        while (cursor := pages[-1]["next_cursor"]) is not None:
            assert len(pages) < 50, "the cursor never ends"
            pages.append(self.page(**params, cursor=cursor))
        return [page["events"] for page in pages]


@contextmanager
def house(tmp_path: Path) -> Generator[House]:
    """Home Assistant has the desk lamp, the floor lamp and the kettle; nothing is monitored
    and the recorder holds nothing yet."""
    clock = [NOW]
    reader = FakeReader([DESK, LAMP, KETTLE])
    app = create_app(tmp_path / "history.sqlite", TOKEN, now=lambda: clock[0], reader=reader)
    with TestClient(app) as client:
        yield House(client, reader, clock)


def key(change: dict[str, Any]) -> tuple[datetime, str]:
    return time_of(change), change["entity_id"]


def read_span(home: House) -> tuple[datetime, datetime]:
    """The earliest start and the latest end of the history reads so far."""
    reads = home.reader.history_reads
    return min(start for _ids, start, _end in reads), max(end for _ids, _start, end in reads)


@pytest.mark.parametrize(
    ("before", "after", "old", "new"),
    [
        pytest.param(
            {"state": "on", "brightness": 100},
            {"state": "on", "brightness": 150},
            {"state": "on", "availability": "available", "brightness": 100},
            {"state": "on", "availability": "available", "brightness": 150},
            id="brightness only",
        ),
        pytest.param(
            {"state": "on", "brightness": 150, "color_mode": "xy", "xy_color": (0.3, 0.32)},
            {"state": "on", "brightness": 150, "color_mode": "xy", "xy_color": (0.45, 0.41)},
            {"brightness": 150, "xy_color": [0.3, 0.32]},
            {"brightness": 150, "xy_color": [0.45, 0.41]},
            id="color only",
        ),
        pytest.param(
            OFF,
            {"state": "on", "brightness": 100},
            {"state": "off", "availability": "available"},
            {"state": "on", "availability": "available", "brightness": 100},
            id="on and off",
        ),
        pytest.param(
            {"state": "on", "brightness": 100},
            {"state": "unavailable"},
            {"state": "on", "availability": "available"},
            {"state": "unavailable", "availability": "unavailable"},
            id="becomes unavailable",
        ),
        pytest.param(
            {"state": "unavailable"},
            OFF,
            {"state": "unavailable", "availability": "unavailable"},
            {"state": "off", "availability": "available"},
            id="becomes available",
        ),
    ],
)
def test_each_kind_of_change_is_one_event_with_its_old_and_new_reading(
    tmp_path: Path,
    before: Payload,
    after: Payload,
    old: dict[str, object],
    new: dict[str, object],
) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.record(DESK.id, (minute(0), before), (minute(1), after))

        [change] = home.changes()

        assert time_of(change) == minute(1)
        assert change["id"] == f"{DESK.id}@{change['time']}"
        assert (change["entity_id"], change["source_entity_id"], change["device_kind"]) == (
            DESK.id,
            "light.desk",
            "light",
        )
        assert {field: change["old"].get(field) for field in old} == old
        assert {field: change["new"].get(field) for field in new} == new


def test_a_row_that_repeats_the_one_before_is_no_change(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        bright = {"state": "on", "brightness": 100}
        home.record(
            DESK.id,
            (minute(0), OFF),
            (minute(1), bright),
            # Home Assistant recorded an attribute Home Keeper does not keep.
            (minute(2), bright),
            (minute(3), bright),
            (minute(4), OFF),
        )

        changes = home.changes()

        assert [time_of(change) for change in changes] == [minute(4), minute(1)]
        assert changes[0]["old"]["state"] == "on"
        assert changes[0]["new"]["state"] == "off"


def test_the_state_at_the_window_start_is_never_a_change_by_itself(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.record(DESK.id, (minute(0), OFF), (minute(60), {"state": "on"}))

        # The light turned on before the window; the window opens on its on state.
        assert home.changes(start=minute(90).isoformat(), end=NOW.isoformat()) == []
        # A device with a single recorder row has no change at all.
        home.select(LAMP.id)
        home.record(LAMP.id, (minute(0), {"state": "on"}))
        assert home.changes(entity_id=LAMP.id) == []
        [change] = home.changes(start=minute(30).isoformat(), end=NOW.isoformat())
        assert time_of(change) == minute(60)
        assert change["old"]["state"] == "off"


def test_a_change_at_exactly_start_is_in_that_window_once_with_its_old_reading(
    tmp_path: Path,
) -> None:
    a, b, c = minute(60), minute(120), minute(180)
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.record(
            DESK.id,
            (minute(0), OFF),
            (a, {"state": "on", "brightness": 10}),
            (b, {"state": "on", "brightness": 20}),
            (c, OFF),
        )

        first = home.changes(start=a.isoformat(), end=b.isoformat())
        second = home.changes(start=b.isoformat(), end=c.isoformat())

        assert [time_of(change) for change in first] == [a]
        assert first[0]["old"]["state"] == "off"
        assert [time_of(change) for change in second] == [b]
        assert (second[0]["old"]["brightness"], second[0]["new"]["brightness"]) == (10, 20)
        # The reader is asked from 1 ms before `start`, so the change at `start` has the row
        # before it.
        [_ids, start, end] = home.reader.history_reads[-1]
        assert (start, end) == (b - timedelta(milliseconds=1), c)


def test_120_changes_page_as_50_50_20_newest_first_without_repeats_or_gaps(
    tmp_path: Path,
) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.select(LAMP.id)
        # Both lights change at the same 60 instants: 120 changes, in pairs that share a time.
        for entity_id in (DESK.id, LAMP.id):
            home.record(
                entity_id,
                *((minute(n), {"state": "on"} if n % 2 else OFF) for n in range(61)),
            )
        expected = sorted(
            ((minute(n), entity_id) for n in range(1, 61) for entity_id in (DESK.id, LAMP.id)),
            reverse=True,
        )

        pages = home.every_page()

        assert [len(page) for page in pages] == [50, 50, 20]
        assert [key(change) for page in pages for change in page] == expected
        # Pages of 7 split the pairs that share a time across page boundaries.
        small = home.every_page(limit=7)
        assert [key(change) for page in small for change in page] == expected
        ids = [change["id"] for page in small for change in page]
        assert len(set(ids)) == 120


def test_a_page_reads_history_newest_first_and_never_past_its_cursor(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        recent = [NOW - timedelta(minutes=n) for n in range(10, 0, -1)]
        home.record(
            DESK.id,
            (NOW - timedelta(days=25), OFF),
            (NOW - timedelta(days=20), {"state": "on"}),
            *((at, {"state": "on" if index % 2 else "off"}) for index, at in enumerate(recent)),
        )
        reads = home.reader.history_reads

        reads.clear()
        first = home.page(limit=1)
        # The last day holds enough changes for the page, so the 30-day window is not read.
        assert [start for _ids, start, _end in reads] == [NOW - timedelta(days=1, milliseconds=1)]

        cursor_time = time_of(first["events"][0])
        reads.clear()
        second = home.page(limit=1, cursor=first["next_cursor"])
        assert time_of(second["events"][0]) < cursor_time
        assert reads
        assert all(end <= cursor_time + timedelta(milliseconds=1) for _ids, _start, end in reads)

        # Every change still shows once, the 20-day-old one included.
        pages = home.every_page(limit=4)
        assert [time_of(change) for page in pages for change in page] == [
            *reversed(recent),
            NOW - timedelta(days=20),
        ]


def test_paging_keeps_its_window_while_time_moves_and_new_changes_arrive(
    tmp_path: Path,
) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        oldest = NOW - timedelta(days=30) + timedelta(minutes=30)
        recent = [NOW - timedelta(minutes=n) for n in range(60, 0, -1)]
        home.record(
            DESK.id,
            (NOW - timedelta(days=31), OFF),
            (oldest, {"state": "on"}),
            *((at, {"state": "on" if index % 2 else "off"}) for index, at in enumerate(recent)),
        )
        expected = [*reversed(recent), oldest]

        first = home.page()
        # An hour passes: the default window would now start after `oldest`, and the light
        # changes twice more.
        home.clock[0] = NOW + timedelta(hours=1)
        home.record(
            DESK.id,
            (NOW + timedelta(minutes=10), {"state": "on", "brightness": 1}),
            (NOW + timedelta(minutes=20), OFF),
        )
        home.reader.history_reads.clear()
        second = home.page(cursor=first["next_cursor"])

        assert [time_of(change) for change in first["events"]] == expected[:50]
        assert [time_of(change) for change in second["events"]] == expected[50:]
        assert second["next_cursor"] is None
        # The first page's window, read no later than its last change.
        assert read_span(home) == (
            NOW - timedelta(days=30, milliseconds=1),
            expected[49] + timedelta(milliseconds=1),
        )
        # A new first page shows the new changes.
        newest = home.changes()
        assert [time_of(change) for change in newest[:2]] == [
            NOW + timedelta(minutes=20),
            NOW + timedelta(minutes=10),
        ]


def test_without_start_the_window_is_the_30_days_before_end(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.record(
            DESK.id,
            (NOW - timedelta(days=40), OFF),
            (NOW - timedelta(days=30, hours=1), {"state": "on"}),
            (NOW - timedelta(days=29), OFF),
            (NOW - timedelta(hours=1), {"state": "on"}),
        )

        assert [time_of(change) for change in home.changes()] == [
            NOW - timedelta(hours=1),
            NOW - timedelta(days=29),
        ]
        assert read_span(home) == (NOW - timedelta(days=30, milliseconds=1), NOW)

        end = NOW - timedelta(hours=2)
        assert [time_of(change) for change in home.changes(end=end.isoformat())] == [
            NOW - timedelta(days=29),
            NOW - timedelta(days=30, hours=1),
        ]


def test_start_end_and_entity_filters_narrow_the_changes(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.select(LAMP.id)
        for entity_id in (DESK.id, LAMP.id):
            home.record(
                entity_id,
                *((minute(n), {"state": "on"} if n % 2 else OFF) for n in range(6)),
            )

        window = {"start": minute(2).isoformat(), "end": minute(4).isoformat()}
        assert [key(change) for change in home.changes(**window)] == [
            (minute(3), LAMP.id),
            (minute(3), DESK.id),
            (minute(2), LAMP.id),
            (minute(2), DESK.id),
        ]
        assert [key(change) for change in home.changes(**window, entity_id=DESK.id)] == [
            (minute(3), DESK.id),
            (minute(2), DESK.id),
        ]
        assert {change["entity_id"] for change in home.changes(entity_id=LAMP.id)} == {LAMP.id}


def test_a_switch_change_has_no_light_fields(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(KETTLE.id)
        home.record(KETTLE.id, (minute(0), OFF), (minute(1), {"state": "on"}))

        [change] = home.changes()

        assert (change["device_kind"], change["source_entity_id"]) == ("switch", "switch.kettle")
        assert (change["old"]["state"], change["new"]["state"]) == ("off", "on")
        for reading in (change["old"], change["new"]):
            assert reading["device_kind"] == "switch"
            assert "brightness" not in reading


def test_history_of_a_device_that_is_not_monitored_never_shows(tmp_path: Path) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        for entity_id in (DESK.id, LAMP.id):
            home.record(entity_id, (minute(0), OFF), (minute(1), {"state": "on"}))

        # The floor lamp was never monitored.
        assert {change["entity_id"] for change in home.changes()} == {DESK.id}
        assert home.page(entity_id=LAMP.id) == {"events": [], "next_cursor": None}

        # The desk lamp was monitored, then removed: its history goes with it.
        home.select(DESK.id, selected=False)
        assert home.changes() == []
        assert home.changes(entity_id=DESK.id) == []

        # Monitoring it again shows all the history Home Assistant has, as Home Keeper keeps
        # no copy of its own.
        home.select(DESK.id)
        assert [time_of(change) for change in home.changes()] == [minute(1)]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"cursor": "not-a-cursor"}, id="cursor"),
        pytest.param({"cursor": "Zm9vfGJhcg"}, id="encoded garbage cursor"),
        pytest.param({"limit": 0}, id="limit 0"),
        pytest.param({"limit": 101}, id="limit 101"),
        pytest.param(
            {"start": "0001-01-01T00:00:00Z", "end": "0001-01-02T00:00:00Z"},
            id="start at the calendar's first instant",
        ),
    ],
)
def test_an_invalid_history_request_is_422(tmp_path: Path, params: dict[str, str | int]) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.record(DESK.id, (minute(0), OFF), (minute(1), {"state": "on"}))

        response = home.client.get("/api/events", params=params)

        assert response.status_code == 422, response.text


@pytest.mark.parametrize("error", UNREACHABLE)
def test_history_is_a_503_with_a_safe_error_while_home_assistant_cannot_be_read(
    tmp_path: Path, error: ReaderError
) -> None:
    with house(tmp_path) as home:
        home.select(DESK.id)
        home.reader.error = error

        response = home.client.get("/api/events")

        assert response.status_code == 503, response.text
        body = response.json()
        assert set(body) == {"error", "message"}
        assert body["error"] == error
        assert body["message"]
        assert "http" not in body["message"].lower()
        assert TOKEN not in body["message"]
