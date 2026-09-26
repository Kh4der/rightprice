"""
Tests for the business-day window.

This is the highest-leverage arithmetic in the app. Every Square query is built
from it, so an off-by-one-hour boundary does not raise — it quietly moves
late-night cash sales onto the wrong day and then presents the result to the
owner as a cash shortage.

The DST cases matter because the obvious implementation (take the start, add 24
hours) is correct on 363 days a year and wrong on the other two.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings

from apps.squareapi.client import business_day_for, business_day_window, to_rfc3339

CHICAGO = ZoneInfo("America/Chicago")

# The app's default: a store that closes after midnight, so the day rolls at 4am.
CUTOFF_4AM = override_settings(STORE_TIMEZONE="America/Chicago", BUSINESS_DAY_CUTOFF=dt.time(4, 0))


@CUTOFF_4AM
def test_window_is_the_cutoff_to_cutoff_span():
    start, end = business_day_window(dt.date(2026, 9, 26))

    assert start.astimezone(CHICAGO) == dt.datetime(2026, 9, 26, 4, 0, tzinfo=CHICAGO)
    assert end.astimezone(CHICAGO) == dt.datetime(2026, 9, 27, 4, 0, tzinfo=CHICAGO)
    assert start.tzinfo == dt.UTC and end.tzinfo == dt.UTC


@CUTOFF_4AM
def test_window_is_24h_on_an_ordinary_day():
    start, end = business_day_window(dt.date(2026, 9, 26))
    assert end - start == dt.timedelta(hours=24)


@CUTOFF_4AM
def test_spring_forward_day_is_only_23_hours():
    """
    2026-03-08 is the spring-forward date in America/Chicago, and the 2am jump
    falls inside the business day that began at 04:00 on the 7th. That day is
    genuinely 23 hours long. Adding a fixed 24h in UTC would push the boundary an
    hour past the next cutoff and double-count an hour of sales.
    """
    start, end = business_day_window(dt.date(2026, 3, 7))

    assert end - start == dt.timedelta(hours=23)
    assert start.utcoffset() == dt.timedelta(0)
    # CST (UTC-6) at the start, CDT (UTC-5) at the end.
    assert start.astimezone(CHICAGO).utcoffset() == dt.timedelta(hours=-6)
    assert end.astimezone(CHICAGO).utcoffset() == dt.timedelta(hours=-5)


@CUTOFF_4AM
def test_fall_back_day_is_25_hours():
    """2026-11-01 is the fall-back date; the business day starting 2026-10-31 gains an hour."""
    start, end = business_day_window(dt.date(2026, 10, 31))

    assert end - start == dt.timedelta(hours=25)
    assert start.astimezone(CHICAGO).utcoffset() == dt.timedelta(hours=-5)  # CDT
    assert end.astimezone(CHICAGO).utcoffset() == dt.timedelta(hours=-6)  # CST


@CUTOFF_4AM
def test_consecutive_windows_abut_exactly_with_no_gap_or_overlap():
    """
    The interval is half-open, so one day's end is the next day's start. A gap
    loses transactions; an overlap counts them twice. Checked across a DST
    boundary, where a naive implementation produces one of the two.
    """
    for day in (dt.date(2026, 9, 26), dt.date(2026, 3, 7), dt.date(2026, 10, 31)):
        _, end = business_day_window(day)
        next_start, _ = business_day_window(day + dt.timedelta(days=1))
        assert end == next_start, f"windows do not abut across {day}"


@CUTOFF_4AM
@pytest.mark.parametrize(
    ("local_time", "expected_day"),
    [
        # A sale at 1:30am belongs to the previous day — the one the employee is
        # closing out right now.
        (dt.datetime(2026, 9, 27, 1, 30, tzinfo=CHICAGO), dt.date(2026, 9, 26)),
        # Exactly on the cutoff starts the new day (half-open interval).
        (dt.datetime(2026, 9, 27, 4, 0, tzinfo=CHICAGO), dt.date(2026, 9, 27)),
        # One second before the cutoff is still the old day.
        (dt.datetime(2026, 9, 27, 3, 59, 59, tzinfo=CHICAGO), dt.date(2026, 9, 26)),
        # Normal trading hours.
        (dt.datetime(2026, 9, 26, 18, 0, tzinfo=CHICAGO), dt.date(2026, 9, 26)),
        # Just after midnight, still yesterday's business.
        (dt.datetime(2026, 9, 27, 0, 1, tzinfo=CHICAGO), dt.date(2026, 9, 26)),
    ],
)
def test_business_day_for_an_instant(local_time, expected_day):
    assert business_day_for(local_time) == expected_day


@CUTOFF_4AM
def test_business_day_for_is_consistent_with_the_window():
    """Whatever day an instant maps to, that day's window must contain it."""
    moment = dt.datetime(2026, 9, 27, 1, 30, tzinfo=CHICAGO)
    day = business_day_for(moment)
    start, end = business_day_window(day)
    assert start <= moment.astimezone(dt.UTC) < end


@CUTOFF_4AM
def test_business_day_for_rejects_a_naive_datetime():
    """A naive datetime here means an unknown timezone, which is a silent bug."""
    with pytest.raises(ValueError):
        business_day_for(dt.datetime(2026, 9, 27, 1, 30))


@override_settings(STORE_TIMEZONE="America/Chicago", BUSINESS_DAY_CUTOFF=dt.time(0, 0))
def test_midnight_cutoff_degenerates_to_the_calendar_day():
    """A store closing before midnight can set the cutoff to 00:00 and get calendar days."""
    start, end = business_day_window(dt.date(2026, 9, 26))
    assert start.astimezone(CHICAGO).date() == dt.date(2026, 9, 26)
    assert start.astimezone(CHICAGO).time() == dt.time(0, 0)
    assert end - start == dt.timedelta(hours=24)
    assert business_day_for(dt.datetime(2026, 9, 26, 23, 59, tzinfo=CHICAGO)) == dt.date(
        2026, 9, 26
    )


@CUTOFF_4AM
def test_rfc3339_is_utc_with_a_z_suffix():
    start, _ = business_day_window(dt.date(2026, 9, 26))
    formatted = to_rfc3339(start)
    assert formatted.endswith("Z")
    assert formatted == "2026-09-26T09:00:00Z"  # 04:00 CDT is 09:00 UTC


def test_rfc3339_rejects_a_naive_datetime():
    with pytest.raises(ValueError):
        to_rfc3339(dt.datetime(2026, 9, 26, 9, 0))
