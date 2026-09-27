"""Deterministic sample records stored only in the visitor's session."""

from __future__ import annotations

import datetime as dt

from django.utils import timezone

from apps.squareapi.client import business_day_for

SESSION_KEY = "rightprice_demo_workspace"
DEMO_VERSION = 1
DRAWER_FLOAT_CENTS = 26_500


def _latest_completed_week(today: dt.date) -> dt.date:
    return today - dt.timedelta(days=today.weekday() + 7)


def default_state() -> dict:
    today = business_day_for(timezone.now())
    monday = _latest_completed_week(today)
    cash_specs = [
        ("mon", 0, 88_500, 62_000, "Counted once; exact match"),
        ("tue", 1, 75_235, 43_235, "Saved short so the owner can recount"),
        ("wed", 2, 91_000, None, "Waiting for the owner to count this pouch"),
        ("thu", 3, 80_500, 54_000, "Corrected after a second count"),
        ("fri", 4, 103_750, None, "Waiting for the owner to count this pouch"),
    ]
    cash_days = []
    for key, offset, register_cents, owner_counted_cents, explanation in cash_specs:
        business_day = monday + dt.timedelta(days=offset)
        expected_cents = register_cents - DRAWER_FLOAT_CENTS
        history = []
        if key == "mon":
            history.append(
                _history_entry(
                    business_day,
                    expected_cents,
                    expected_cents,
                    "Counted once",
                )
            )
        elif key == "tue":
            history.append(
                _history_entry(
                    business_day,
                    owner_counted_cents,
                    expected_cents,
                    "First count saved; pouch is $55 short",
                )
            )
        elif key == "thu":
            first_count = expected_cents + 1_000
            history.append(
                _history_entry(
                    business_day,
                    first_count,
                    expected_cents,
                    "First count included the next day's $10",
                    hour=10,
                )
            )
            history.append(
                _history_entry(
                    business_day,
                    expected_cents,
                    expected_cents,
                    "Recounted the correct pouch",
                    hour=10,
                    minute=8,
                    correction_of_cents=first_count,
                )
            )
        cash_days.append(
            {
                "id": key,
                "business_day": business_day.isoformat(),
                "employee_name": "Jordan (demo employee)",
                "register_cents": register_cents,
                "drawer_float_cents": DRAWER_FLOAT_CENTS,
                "expected_cents": expected_cents,
                "counted_cents": owner_counted_cents,
                "explanation": explanation,
                "history": history,
            }
        )

    return {
        "version": DEMO_VERSION,
        "week_start": monday.isoformat(),
        "cash_days": cash_days,
        "payouts": [
            {
                "id": "payout-1",
                "business_day": (monday + dt.timedelta(days=2)).isoformat(),
                "employee_name": "Jordan",
                "amount_cents": 12_500,
                "ticket_reference": "DEMO-88421",
                "status": "waiting",
            },
            {
                "id": "payout-2",
                "business_day": (monday + dt.timedelta(days=0)).isoformat(),
                "employee_name": "Maya",
                "amount_cents": 5_000,
                "ticket_reference": "DEMO-77210",
                "status": "reimbursed",
            },
        ],
        "inventory": {
            "invoice_number": "DEMO-INV-1048",
            "invoice_date": (monday + dt.timedelta(days=4)).isoformat(),
            "vendor": "Sample Beverage Distributor",
            "lines": [
                {
                    "id": "line-1",
                    "description": "Tito's Handmade Vodka 1 L",
                    "invoice_units": 12,
                    "square_item": "Tito's Vodka · 1 L",
                    "square_count": 18,
                    "projected_count": 30,
                    "unit_cost_cents": 2_099,
                    "square_cost_cents": 1_999,
                    "status": "matched",
                },
                {
                    "id": "line-2",
                    "description": "Jack Daniel's Old No. 7 750 mL",
                    "invoice_units": 6,
                    "square_item": "Jack Daniel's · 750 mL",
                    "square_count": 11,
                    "projected_count": 17,
                    "unit_cost_cents": 1_875,
                    "square_cost_cents": 1_825,
                    "status": "matched",
                },
                {
                    "id": "line-3",
                    "description": "Casamigos Reposado 750 mL",
                    "invoice_units": 6,
                    "square_item": "Casamigos Reposado · 750 mL",
                    "square_count": 4,
                    "projected_count": 10,
                    "unit_cost_cents": 4_050,
                    "square_cost_cents": 3_750,
                    "status": "suggested",
                },
            ],
        },
        "employees": [
            {"name": "Jordan", "code": "DEMO01", "today": "Daily close"},
            {"name": "Maya", "code": "DEMO02", "today": "Lottery payout"},
        ],
    }


def _history_entry(
    business_day: dt.date,
    counted_cents: int,
    expected_cents: int,
    note: str,
    *,
    hour: int = 9,
    minute: int = 30,
    correction_of_cents: int | None = None,
) -> dict:
    recorded_at = timezone.make_aware(
        dt.datetime.combine(business_day + dt.timedelta(days=6), dt.time(hour, minute))
    )
    return {
        "created_at": recorded_at.isoformat(),
        "counted_cents": counted_cents,
        "expected_cents": expected_cents,
        "variance_cents": counted_cents - expected_cents,
        "note": note,
        "entered_by_name": "Demo Owner",
        "correction_of_cents": correction_of_cents,
    }


def get_state(request) -> dict:
    state = request.session.get(SESSION_KEY)
    if not isinstance(state, dict) or state.get("version") != DEMO_VERSION:
        state = default_state()
        request.session[SESSION_KEY] = state
    return state


def save_state(request, state: dict) -> None:
    request.session[SESSION_KEY] = state
    request.session.modified = True


def reset_state(request) -> dict:
    state = default_state()
    save_state(request, state)
    return state
