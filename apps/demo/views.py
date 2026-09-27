from __future__ import annotations

import datetime as dt
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from apps.accounts.management.commands.seed_demo_account import DEMO_LOGIN_CODE
from apps.accounts.models import User
from apps.reconcile.forms import DailyCashCountForm
from apps.squareapi.client import business_day_for

from .state import get_state, reset_state, save_state


def demo_owner_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_owner or not request.user.is_demo:
            raise PermissionDenied("This page belongs to the isolated practice workspace.")
        return view(request, *args, **kwargs)

    return wrapped


@require_POST
def start_demo(request):
    user = User.objects.filter(
        login_code=DEMO_LOGIN_CODE,
        is_demo=True,
        is_active=True,
    ).first()
    if user is None:
        messages.error(request, "The practice account is being prepared. Try again shortly.")
        return redirect("accounts:login")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    reset_state(request)
    messages.success(request, "Practice mode is ready. These numbers are examples only.")
    return redirect("demo:dashboard")


@demo_owner_required
def dashboard(request):
    state = get_state(request)
    cash_days = state["cash_days"]
    waiting_cash = sum(day["counted_cents"] is None for day in cash_days)
    issue_cash = sum(
        day["counted_cents"] is not None
        and day["counted_cents"] != day["expected_cents"]
        for day in cash_days
    )
    waiting_payouts = sum(item["status"] == "waiting" for item in state["payouts"])
    unresolved_inventory = sum(
        line["status"] != "matched" for line in state["inventory"]["lines"]
    )
    return render(
        request,
        "demo/dashboard.html",
        {
            "state": state,
            "waiting_cash": waiting_cash,
            "issue_cash": issue_cash,
            "waiting_payouts": waiting_payouts,
            "unresolved_inventory": unresolved_inventory,
        },
    )


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _cash_rows(state: dict, week_start: dt.date) -> list[dict]:
    rows = []
    for day in state["cash_days"]:
        business_day = dt.date.fromisoformat(day["business_day"])
        if _week_start(business_day) != week_start:
            continue
        counted_cents = day["counted_cents"]
        expected_cents = day["expected_cents"]
        variance_cents = None if counted_cents is None else counted_cents - expected_cents
        if counted_cents is None:
            state_name, state_label, state_class = "waiting", "Needs your count", "provisional"
        elif variance_cents == 0:
            state_name, state_label, state_class = "matched", "Matches", "approved"
        elif variance_cents < 0:
            state_name, state_label, state_class = (
                "issue",
                f"Short ${abs(variance_cents) / 100:,.2f}",
                "mismatch",
            )
        else:
            state_name, state_label, state_class = (
                "issue",
                f"Over ${variance_cents / 100:,.2f}",
                "mismatch",
            )
        history = []
        for item in day["history"]:
            history.append(
                {
                    **item,
                    "created_at": parse_datetime(item["created_at"]),
                }
            )
        explanation = day["explanation"]
        if len(history) > 1:
            explanation = (
                "Corrected after another count"
                if variance_cents == 0
                else "Correction saved; count this pouch again"
            )
        rows.append(
            {
                **day,
                "business_day": business_day,
                "counted_cents": counted_cents,
                "variance_cents": variance_cents,
                "state": state_name,
                "state_label": state_label,
                "state_class": state_class,
                "explanation": explanation,
                "amount_initial": (
                    f"{Decimal(counted_cents) / Decimal(100):.2f}"
                    if counted_cents is not None
                    else ""
                ),
                "history": history,
                "is_corrected": len(history) > 1,
                "form_action": reverse("demo:daily-cash-count", args=[day["id"]]),
            }
        )
    return rows


@demo_owner_required
def daily_cash(request):
    state = get_state(request)
    default_week = dt.date.fromisoformat(state["week_start"])
    sample_week_end = default_week + dt.timedelta(days=6)
    selected_date = parse_date((request.GET.get("date") or "").strip()) or default_week
    if not default_week <= selected_date <= sample_week_end:
        selected_date = default_week
    week_start = default_week
    week_end = sample_week_end
    rows = _cash_rows(state, week_start)
    counted_rows = [row for row in rows if row["counted_cents"] is not None]
    expected_total = sum(row["expected_cents"] for row in rows)
    counted_expected = sum(row["expected_cents"] for row in counted_rows)
    counted_total = sum(row["counted_cents"] for row in counted_rows)
    today = business_day_for(timezone.now())
    return render(
        request,
        "demo/daily_cash.html",
        {
            "rows": rows,
            "selected_date": selected_date,
            "sample_week_start": default_week,
            "sample_week_end": sample_week_end,
            "week_start": week_start,
            "week_end": week_end,
            "today": today,
            "current_week": _week_start(today),
            "daily_cash_count": len(rows),
            "counted_count": len(counted_rows),
            "expected_total": expected_total,
            "counted_difference": counted_total - counted_expected,
            "issue_count": sum(row["state"] == "issue" for row in rows),
        },
    )


@demo_owner_required
@require_POST
def record_daily_cash(request, day_id):
    state = get_state(request)
    day = next((item for item in state["cash_days"] if item["id"] == day_id), None)
    if day is None:
        raise PermissionDenied("That practice day does not exist.")
    form = DailyCashCountForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Type a dollar amount such as 620.00, then save again.")
    else:
        counted_cents = int(form.cleaned_data["amount"] * Decimal(100))
        previous_cents = day["counted_cents"]
        expected_cents = day["expected_cents"]
        day["counted_cents"] = counted_cents
        day["history"].append(
            {
                "created_at": timezone.now().isoformat(),
                "counted_cents": counted_cents,
                "expected_cents": expected_cents,
                "variance_cents": counted_cents - expected_cents,
                "note": form.cleaned_data["note"].strip() or "Practice count",
                "entered_by_name": "Demo Owner",
                "correction_of_cents": previous_cents,
            }
        )
        save_state(request, state)
        difference = counted_cents - expected_cents
        if difference == 0:
            messages.success(request, "Saved. This day's pouch matches exactly.")
        else:
            direction = "short" if difference < 0 else "over"
            messages.warning(
                request,
                f"Saved as {direction} ${abs(difference) / 100:,.2f}. Count again and correct it if needed.",
            )
    return redirect(
        f"{reverse('demo:daily-cash')}?date={day['business_day']}#demo-cash-{day_id}"
    )


@demo_owner_required
def payouts(request):
    state = get_state(request)
    rows = [
        {**item, "business_day": dt.date.fromisoformat(item["business_day"])}
        for item in state["payouts"]
    ]
    return render(request, "demo/payouts.html", {"payouts": rows})


@demo_owner_required
@require_POST
def reimburse_payout(request, payout_id):
    state = get_state(request)
    payout = next((item for item in state["payouts"] if item["id"] == payout_id), None)
    if payout is None:
        raise PermissionDenied("That practice payout does not exist.")
    payout["status"] = "reimbursed"
    save_state(request, state)
    messages.success(request, "Practice payout marked reimbursed. No real money moved.")
    return redirect("demo:payouts")


@demo_owner_required
def inventory(request):
    state = get_state(request)
    inventory_state = state["inventory"]
    lines = []
    for line in inventory_state["lines"]:
        baseline = line["square_cost_cents"]
        change_percent = round((line["unit_cost_cents"] - baseline) * 100 / baseline, 1)
        lines.append({**line, "cost_change_percent": change_percent})
    return render(
        request,
        "demo/inventory.html",
        {
            "invoice": {
                **inventory_state,
                "invoice_date": dt.date.fromisoformat(inventory_state["invoice_date"]),
            },
            "lines": lines,
            "unresolved_count": sum(line["status"] != "matched" for line in lines),
        },
    )


@demo_owner_required
@require_POST
def match_inventory_line(request, line_id):
    state = get_state(request)
    line = next(
        (item for item in state["inventory"]["lines"] if item["id"] == line_id),
        None,
    )
    if line is None:
        raise PermissionDenied("That practice invoice line does not exist.")
    line["status"] = "matched"
    save_state(request, state)
    messages.success(request, "Practice match saved. Square was not contacted.")
    return redirect("demo:inventory")


@demo_owner_required
@require_POST
def reset_demo(request):
    reset_state(request)
    messages.success(request, "Practice data reset to the starting examples.")
    return redirect("demo:dashboard")
