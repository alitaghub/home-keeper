"""A measurement's numbers over a range, from Home Assistant's statistics.

Periods with no statistics are gaps: they have no bucket and count toward no number.
"""

from collections.abc import Sequence
from statistics import fmean

from home_keeper.models import (
    Bucket,
    Identity,
    MeasurementReading,
    MeasurementSeries,
    Reading,
    StatisticsPeriod,
)


def measurement_series(
    entity_id: Identity,
    period: StatisticsPeriod,
    buckets: Sequence[Bucket],
    current: Reading | None,
) -> MeasurementSeries:
    """The buckets with the lowest minimum, the mean of the bucket means and the highest
    maximum, all None without a bucket. Each bucket is one period, so the means weigh the
    same. The unit and latest value come from the `current` reading."""
    reading = current if isinstance(current, MeasurementReading) else None
    return MeasurementSeries(
        entity_id=entity_id,
        unit=reading.unit if reading else None,
        period=period,
        buckets=list(buckets),
        min=min((bucket.min for bucket in buckets), default=None),
        mean=fmean(bucket.mean for bucket in buckets) if buckets else None,
        max=max((bucket.max for bucket in buckets), default=None),
        latest=reading.value if reading else None,
    )
