"""Long-term statistics buckets for tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from home_keeper import models


def bucket(
    start: datetime, mean: float, low: float, high: float, *, hours: float = 1
) -> models.Bucket:
    """One long-term statistics bucket of `hours` from `start`; a bucket's times are ISO text."""
    return models.Bucket(
        start=start.isoformat(),
        end=(start + timedelta(hours=hours)).isoformat(),
        mean=mean,
        min=low,
        max=high,
    )
