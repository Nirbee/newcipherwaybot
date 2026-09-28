import datetime as dt

from src.core.dates import days_left, fmt_date


def test_fmt_date_uses_moscow_time_near_midnight() -> None:
    # 22:30 UTC == 01:30 MSK next day — the cabinet shows 01.10, so must the bot.
    assert fmt_date(dt.datetime(2026, 9, 30, 22, 30, tzinfo=dt.UTC)) == "01.10.2026"
    assert fmt_date(dt.datetime(2026, 9, 30, 20, 59, tzinfo=dt.UTC)) == "30.09.2026"


def test_fmt_date_naive_is_utc_and_default() -> None:
    assert fmt_date(dt.datetime(2026, 9, 30, 22, 30)) == "01.10.2026"
    assert fmt_date(None) == ""
    assert fmt_date(None, "—") == "—"


def test_days_left_rounds_up_like_miniapp() -> None:
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.UTC)
    assert days_left(now + dt.timedelta(days=29, hours=1), now) == 30
    assert days_left(now + dt.timedelta(days=30), now) == 30
    assert days_left(now - dt.timedelta(hours=5), now) == 0
