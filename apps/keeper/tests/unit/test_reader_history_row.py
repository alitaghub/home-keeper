"""A history row from the reader service carries timezone-aware times, or is invalid."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from home_keeper.reader import HistoryRow

AT = "2026-10-12T09:00:00+02:00"


def row(last_changed: str, last_updated: str) -> dict[str, object]:
    return {
        "source_entity_id": "light.desk",
        "reading": {
            "device_kind": "light",
            "state": "on",
            "availability": "available",
            "last_changed": last_changed,
            "last_updated": last_updated,
        },
    }


def test_a_row_is_dated_by_its_last_updated_time_in_utc() -> None:
    parsed = HistoryRow.model_validate(row(AT, "2026-10-12T09:30:00+02:00"))
    assert parsed.updated_at == datetime(2026, 10, 12, 7, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("last_changed", "last_updated"),
    [
        pytest.param(AT, "not a time", id="malformed last_updated"),
        pytest.param("yesterday", AT, id="malformed last_changed"),
        pytest.param(AT, "2026-10-12T09:00:00", id="last_updated without a timezone"),
        pytest.param("2026-10-12T09:00:00", AT, id="last_changed without a timezone"),
    ],
)
def test_a_row_with_a_malformed_or_naive_time_is_invalid(
    last_changed: str, last_updated: str
) -> None:
    with pytest.raises(ValidationError):
        HistoryRow.model_validate(row(last_changed, last_updated))
