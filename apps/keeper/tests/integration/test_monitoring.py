"""The selection and monitored devices through `Monitoring`, with a real store and readings
recorded as the collector would."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from home_keeper.models import DeviceObservation, Entity, MonitoredEntity
from home_keeper.monitoring import Monitoring, UnknownEntity, UnsupportedEntity
from home_keeper.recorder import Recorder, Rejected
from home_keeper.store import Store

NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
RECONCILIATION_SECONDS = 60
# A confirmation older than two reconciliation intervals is stale.
FRESH_FOR = timedelta(seconds=2 * RECONCILIATION_SECONDS)

CATALOG = [
    Entity(id="bulb-a", name="Desk", device_kind="light", supported=True),
    Entity(id="plug-a", name="Kettle", device_kind="switch", supported=True),
    # A catalog entry without a kind; only its readings name one.
    Entity(id="kindless-plug", name="Kindless plug", supported=True),
    Entity(id="room", name="Living room", supported=False, reason="Groups are not recorded"),
]

observation_of: TypeAdapter[DeviceObservation] = TypeAdapter(DeviceObservation)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Home:
    store: Store
    clock: Clock
    monitoring: Monitoring

    def select(self, entity_id: str, *, selected: bool = True) -> int:
        result = self.monitoring.select(entity_id, selected=selected)
        assert not isinstance(result, UnknownEntity | UnsupportedEntity), result
        return result.revision

    def snapshot(self, entity_id: str, kind: str = "light", state: str = "off") -> None:
        """Record a snapshot as the collector would, received now."""
        payload = observation_of.validate_python(
            {
                "entity_id": entity_id,
                "source_entity_id": f"{kind}.{entity_id.replace('-', '_')}",
                "selection_revision": self.monitoring.selection().revision,
                "source_id": "demo",
                "received_at": self.clock.now,
                "last_changed": "2020-01-01T00:00:00Z",
                "last_updated": "2020-01-01T00:00:00Z",
                "availability": "available",
                "state": state,
            }
            | ({"device_kind": "switch"} if kind == "switch" else {})
        )
        result = Recorder(self.store, "demo", self.clock).observe(payload)
        assert not isinstance(result, Rejected), result

    def leave_discovery(self, entity_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("DELETE FROM catalog WHERE id=?", (entity_id,))

    def monitored(self, *, collector_healthy: bool = True) -> dict[str, MonitoredEntity]:
        devices = self.monitoring.devices(collector_healthy=collector_healthy).entities
        return {device.id: device for device in devices}


@pytest.fixture
def home(tmp_path: Path) -> Home:
    """The test catalog with nothing monitored."""
    clock = Clock()
    store = Store(tmp_path / "monitoring.sqlite")
    with store.transaction() as db:
        # Written by `create_app` at startup; the selection read needs it.
        db.execute("INSERT INTO metadata VALUES('refresh_revision', '0')")
        db.executemany(
            "INSERT INTO catalog VALUES(?, ?)",
            [(entity.id, entity.model_dump_json()) for entity in CATALOG],
        )
    return Home(store, clock, Monitoring(store, now=clock, reconciliation_seconds=60))


# Selection


def test_each_change_bumps_the_revision_and_a_repeat_changes_nothing(home: Home) -> None:
    assert home.monitoring.selection().revision == 0
    assert home.select("bulb-a") == 1
    assert home.select("bulb-a") == 1
    assert home.select("plug-a") == 2
    assert home.monitoring.selection().entity_ids == ["bulb-a", "plug-a"]
    assert home.select("bulb-a", selected=False) == 3
    assert home.select("bulb-a", selected=False) == 3
    assert home.monitoring.selection().entity_ids == ["plug-a"]


def test_selecting_again_waits_for_a_new_confirmation(home: Home) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a")
    assert home.monitored()["bulb-a"].freshness == "fresh"

    home.select("bulb-a", selected=False)
    home.select("bulb-a")

    again = home.monitored()["bulb-a"]
    assert (again.freshness, again.baseline, again.confirmed_at) == ("pending", None, None)


def test_unknown_or_unsupported_devices_cannot_be_selected(home: Home) -> None:
    assert home.monitoring.select("missing", selected=True) == UnknownEntity()
    assert home.monitoring.select("missing", selected=False) == UnknownEntity()
    assert home.monitoring.select("room", selected=True) == UnsupportedEntity()
    assert home.monitoring.selection().revision == 0


def test_a_device_that_left_discovery_can_still_be_stopped(home: Home) -> None:
    home.select("bulb-a")
    home.leave_discovery("bulb-a")
    assert home.monitoring.select("bulb-a", selected=True) == UnknownEntity()
    assert home.select("bulb-a", selected=False) == 2


# Admission


def test_only_the_current_revision_of_a_monitored_supported_device_is_admitted(
    home: Home,
) -> None:
    revision = home.select("bulb-a")
    with home.store.transaction("DEFERRED") as db:
        assert Monitoring.admits(db, "bulb-a", revision)
        assert not Monitoring.admits(db, "bulb-a", revision - 1)
        assert not Monitoring.admits(db, "plug-a", revision)

    home.leave_discovery("bulb-a")
    with home.store.transaction("DEFERRED") as db:
        assert not Monitoring.admits(db, "bulb-a", revision)


# Device kind


def test_discovery_names_the_kind_before_any_reading(home: Home) -> None:
    home.select("plug-a")
    with home.store.transaction("DEFERRED") as db:
        device = Monitoring.device(db, "plug-a")
    assert (device.kind, device.monitored, device.reading) == ("switch", True, None)


def test_the_newest_reading_names_the_kind_when_discovery_does_not(home: Home) -> None:
    home.select("kindless-plug")
    with home.store.transaction("DEFERRED") as db:
        assert Monitoring.device(db, "kindless-plug").kind is None
    home.snapshot("kindless-plug", "switch")
    with home.store.transaction("DEFERRED") as db:
        assert Monitoring.device(db, "kindless-plug").kind == "switch"


def test_a_device_that_left_discovery_keeps_the_kind_it_was_selected_as(home: Home) -> None:
    home.select("plug-a")
    home.leave_discovery("plug-a")
    with home.store.transaction("DEFERRED") as db:
        device = Monitoring.device(db, "plug-a")
    assert (device.entity, device.kind) == (None, "switch")
    missing = home.monitored()["plug-a"]
    assert (missing.device_kind, missing.supported) == ("switch", False)
    assert missing.reason == "Entity missing from latest discovery"


def test_once_discovery_drops_a_device_its_newest_reading_outranks_its_selected_kind(
    home: Home,
) -> None:
    home.select("bulb-a")
    # Home Assistant now reports the device as a switch, then discovery drops it.
    home.snapshot("bulb-a", "switch")
    home.leave_discovery("bulb-a")
    with home.store.transaction("DEFERRED") as db:
        assert Monitoring.device(db, "bulb-a").kind == "switch"


# Freshness


def test_a_monitored_device_is_pending_until_its_first_confirmation(home: Home) -> None:
    home.select("bulb-a")
    pending = home.monitored()["bulb-a"]
    assert (pending.freshness, pending.baseline) == ("pending", None)
    home.snapshot("bulb-a", state="on")
    fresh = home.monitored()["bulb-a"]
    assert fresh.freshness == "fresh"
    assert fresh.baseline is not None
    assert fresh.baseline.state == "on"
    assert fresh.confirmed_at == NOW.isoformat()


@pytest.mark.parametrize(
    ("age", "freshness"),
    [(FRESH_FOR, "fresh"), (FRESH_FOR + timedelta(seconds=1), "stale")],
)
def test_a_confirmation_stays_fresh_for_two_reconciliation_intervals(
    home: Home, age: timedelta, freshness: str
) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a")
    home.clock.now = NOW + age
    assert home.monitored()["bulb-a"].freshness == freshness


def test_a_confirmation_from_the_future_is_stale(home: Home) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a")
    home.clock.now = NOW - timedelta(seconds=1)
    assert home.monitored()["bulb-a"].freshness == "stale"


def test_an_unhealthy_collector_makes_every_confirmed_device_stale(home: Home) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a")
    assert home.monitored(collector_healthy=False)["bulb-a"].freshness == "stale"


def test_an_open_gap_makes_the_device_stale(home: Home) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a")
    with home.store.transaction() as db:
        Store.interrupt(db, "demo", NOW.isoformat(), "connection", "bulb-a")
    assert home.monitored()["bulb-a"].freshness == "stale"


def test_a_device_that_left_discovery_is_stale_but_keeps_its_reading(home: Home) -> None:
    home.select("bulb-a")
    home.snapshot("bulb-a", state="on")
    home.leave_discovery("bulb-a")
    missing = home.monitored()["bulb-a"]
    assert missing.freshness == "stale"
    assert missing.baseline is not None
    assert missing.baseline.state == "on"
