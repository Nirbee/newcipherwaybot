"""User-facing date rendering.

Datetimes are stored in UTC, but customers read dates in Moscow time — and the web cabinet /
mini-app render them in the browser's local zone. Formatting a UTC value directly with
``strftime`` shows the previous calendar day for anything between 00:00 and 03:00 MSK, so the
bot and the cabinet disagreed on the subscription end date. Always go through these helpers.
"""

from __future__ import annotations

import datetime as dt
import math

MSK = dt.timezone(dt.timedelta(hours=3), "MSK")


def to_msk(value: dt.datetime) -> dt.datetime:
    """Convert to Moscow time; a naive value is assumed to be UTC (how the DB stores it)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(MSK)


def fmt_date(value: dt.datetime | None, default: str = "") -> str:
    """``dd.mm.yyyy`` in Moscow time, or `default` when there is no date."""
    return to_msk(value).strftime("%d.%m.%Y") if value is not None else default


def days_left(value: dt.datetime, now: dt.datetime | None = None) -> int:
    """Whole days until `value`, rounded up (>= 0) — matches the mini-app's ``daysLeft``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    now = now or dt.datetime.now(dt.UTC)
    return max(0, math.ceil((value - now).total_seconds() / 86400))
