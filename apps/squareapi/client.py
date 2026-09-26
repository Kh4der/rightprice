"""
Square client construction and the business-day window.

Everything here is built against the shapes verified in
docs/square-sdk-reference.md by introspecting the installed SDK, not against
documentation — several published field names do not match the SDK.

Two things in this module are load-bearing:

* ``get_client()`` is the only place a Square client is built, so the sandbox /
  production switch and the token exist in exactly one place.
* ``business_day_window()`` is the only place a business day becomes a pair of
  timestamps. Every Square query goes through it. A store closing at 2am has no
  calendar business day, and a wrong window silently files late-night sales under
  the wrong date — the kind of bug that looks like a cash shortage.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from square import Square
from square.environment import SquareEnvironment

# Square accepts RFC 3339. Python's isoformat() on an aware datetime produces a
# compatible string, but with a "+00:00" offset rather than "Z"; both are legal.
RFC3339 = "%Y-%m-%dT%H:%M:%S%z"


@lru_cache(maxsize=1)
def get_client() -> Square:
    """
    The single Square client for this process.

    Cached because constructing one builds an httpx client and a connection pool;
    a Celery worker handling many documents should not build one per task.
    """
    token = settings.SQUARE_ACCESS_TOKEN
    if not token:
        raise ImproperlyConfigured(
            "SQUARE_ACCESS_TOKEN is not set. Get a sandbox token from "
            "squareup.com/dashboard/apps -> your app -> Sandbox."
        )

    env_name = (settings.SQUARE_ENVIRONMENT or "sandbox").lower()
    if env_name == "production":
        environment = SquareEnvironment.PRODUCTION
    elif env_name == "sandbox":
        environment = SquareEnvironment.SANDBOX
    else:
        raise ImproperlyConfigured(
            f"SQUARE_ENVIRONMENT must be 'sandbox' or 'production', got {env_name!r}."
        )

    return Square(token=token, environment=environment, timeout=30.0)


def store_timezone() -> ZoneInfo:
    return ZoneInfo(settings.STORE_TIMEZONE)


def business_day_window(business_day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """
    The UTC half-open interval ``[start, end)`` covering one store business day.

    A business day runs from the cutoff on that calendar date to the cutoff on
    the next. With ``BUSINESS_DAY_CUTOFF = 04:00`` in ``America/Chicago``, the
    business day 2026-09-26 covers local 2026-09-26 04:00 up to (not including)
    2026-09-27 04:00 — so a sale rung at 01:30 on the 27th belongs to the 26th,
    which is what the employee closing the drawer believes.

    The interval is half-open deliberately: a closed interval would let a
    transaction exactly on the boundary fall into two business days and be
    counted twice.

    DST is handled by constructing local times and converting, rather than by
    adding 24 hours. On the spring-forward date the window is genuinely 23 hours
    long and on fall-back genuinely 25; adding a fixed timedelta in UTC would
    silently shift the boundary by an hour on those two days a year.
    """
    tz = store_timezone()
    cutoff = settings.BUSINESS_DAY_CUTOFF

    start_local = dt.datetime.combine(business_day, cutoff, tzinfo=tz)
    end_local = dt.datetime.combine(business_day + dt.timedelta(days=1), cutoff, tzinfo=tz)

    return (
        start_local.astimezone(dt.UTC),
        end_local.astimezone(dt.UTC),
    )


def business_day_for(moment: dt.datetime) -> dt.date:
    """
    Which business day an instant belongs to.

    Used when stamping a submission: an employee uploading at 01:15 is closing out
    yesterday, and the app must agree with them without being asked.
    """
    if moment.tzinfo is None:
        raise ValueError("business_day_for() requires an aware datetime.")

    local = moment.astimezone(store_timezone())
    if local.time() < settings.BUSINESS_DAY_CUTOFF:
        return local.date() - dt.timedelta(days=1)
    return local.date()


def to_rfc3339(moment: dt.datetime) -> str:
    """Format for a Square query parameter."""
    if moment.tzinfo is None:
        raise ValueError("to_rfc3339() requires an aware datetime.")
    return moment.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
