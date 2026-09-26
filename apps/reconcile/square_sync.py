"""Read-only Square reconciliation for one store business day.

The vision pipeline records what the paper says.  This module independently
reads Square and compares the two sources.  It deliberately does not contain
any Square write call, and it refuses to choose one drawer when a business day
contains more than one.

Money stays in integer cents from the SDK boundary through persistence.
``None`` means absent; zero is ordinary data.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.capture.models import DocumentType
from apps.squareapi.client import business_day_window, get_client, to_rfc3339

from .models import DailyReconciliation, ReconciliationStatus

SQUARE_CHECK_SOURCE = "square_api"


@dataclass(frozen=True)
class DailySquareSyncResult:
    """The persisted outcome of one read-only Square pull."""

    reconciliation_id: str
    status: str
    square_values: dict[str, Any]
    comparisons: tuple[dict[str, Any], ...]
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ComparisonSpec:
    name: str
    paper_document: str
    paper_field: str
    square_value: int | None
    detail: str


def sync_daily_reconciliation(
    reconciliation: DailyReconciliation,
    client: object | None = None,
    *,
    on_persist: Callable[[DailyReconciliation, DailySquareSyncResult], None] | None = None,
) -> DailySquareSyncResult:
    """Pull Square's day data, persist a snapshot, and compare it to paper.

    ``client`` exists for tests and explicit dependency injection.  When it is
    omitted the configured Square SDK client is used.  The function only calls
    list/get endpoints.
    """

    location_id = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    start, end = business_day_window(reconciliation.submission.business_day)
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "location_id": location_id,
        "business_day": reconciliation.submission.business_day.isoformat(),
        "window": {
            "begin_time": to_rfc3339(start),
            "end_time": to_rfc3339(end),
        },
        "drawer": {
            "status": "not_loaded",
            "shift_count": 0,
            "shifts": [],
        },
        "payments": {
            "status": "not_loaded",
            "returned_count": 0,
            "completed_count": 0,
            "totals_by_source_cents": {},
            "total_cents": None,
        },
    }
    problems: list[str] = []

    if not location_id:
        problems.append("Square location is not configured.")
        snapshot["drawer"]["status"] = "not_configured"
        snapshot["payments"]["status"] = "not_configured"
        return _persist_result(
            reconciliation,
            snapshot,
            (),
            problems,
            on_persist=on_persist,
        )

    try:
        square_client = client or get_client()
    except Exception as exc:  # configuration/client construction is an incomplete sync
        message = _safe_error(exc)
        problems.append(f"Square client could not be created: {message}")
        snapshot["drawer"].update({"status": "error", "error": message})
        snapshot["payments"].update({"status": "error", "error": message})
        return _persist_result(
            reconciliation,
            snapshot,
            (),
            problems,
            on_persist=on_persist,
        )

    shifts, drawer_problems = _load_drawers(
        square_client,
        location_id=location_id,
        begin_time=snapshot["window"]["begin_time"],
        end_time=snapshot["window"]["end_time"],
    )
    snapshot["drawer"] = shifts
    problems.extend(drawer_problems)

    payments, payment_problems = _load_payments(
        square_client,
        location_id=location_id,
        begin_time=snapshot["window"]["begin_time"],
        end_time=snapshot["window"]["end_time"],
    )
    snapshot["payments"] = payments
    problems.extend(payment_problems)

    specs = _comparison_specs(snapshot)
    comparisons = (
        *(_compare_to_paper(reconciliation.paper_values, spec) for spec in specs),
        _compare_unexplained_cash(reconciliation, snapshot),
        _compare_closing_team_member(reconciliation, snapshot),
    )
    for comparison in comparisons:
        if not comparison["available"]:
            problems.append(str(comparison["detail"]))

    return _persist_result(
        reconciliation,
        snapshot,
        comparisons,
        problems,
        on_persist=on_persist,
    )


def _load_drawers(
    client: object,
    *,
    location_id: str,
    begin_time: str,
    end_time: str,
) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    result: dict[str, Any] = {
        "status": "ok",
        "shift_count": 0,
        "shifts": [],
    }
    try:
        summaries = list(
            client.cash_drawers.shifts.list(
                location_id=location_id,
                begin_time=begin_time,
                end_time=end_time,
            )
        )
    except Exception as exc:
        message = _safe_error(exc)
        result.update({"status": "error", "error": message})
        return result, [f"Square cash drawers could not be loaded: {message}"]

    result["shift_count"] = len(summaries)
    if not summaries:
        result["status"] = "missing"
        return result, ["Square returned no cash drawer shift for this business day."]
    if len(summaries) > 1:
        result["status"] = "ambiguous"
        problems.append(
            f"Square returned {len(summaries)} cash drawer shifts; no drawer was chosen."
        )

    shifts: list[dict[str, Any]] = []
    for summary in summaries:
        shift_id = _string_or_none(getattr(summary, "id", None))
        if shift_id is None:
            shifts.append({"id": None, "load_error": "Shift summary has no ID."})
            problems.append("A Square cash drawer summary has no shift ID.")
            continue
        try:
            response = client.cash_drawers.shifts.get(shift_id, location_id=location_id)
            shift = getattr(response, "cash_drawer_shift", None)
            if shift is None:
                raise ValueError("Square returned no cash drawer shift detail.")
            shifts.append(_shift_snapshot(shift))
        except Exception as exc:
            message = _safe_error(exc)
            shifts.append({"id": shift_id, "load_error": message})
            problems.append(f"Square cash drawer {shift_id} could not be loaded: {message}")
    result["shifts"] = shifts

    if any("load_error" in shift for shift in shifts):
        result["status"] = "error"
    elif len(shifts) == 1 and shifts[0].get("state") != "CLOSED":
        result["status"] = "open"
        problems.append("The Square cash drawer shift is not closed.")
    return result, problems


def _load_payments(
    client: object,
    *,
    location_id: str,
    begin_time: str,
    end_time: str,
) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {
        "status": "ok",
        "returned_count": 0,
        "completed_count": 0,
        "totals_by_source_cents": {},
        "total_cents": None,
    }
    try:
        payments = list(
            client.payments.list(
                location_id=location_id,
                begin_time=begin_time,
                end_time=end_time,
            )
        )
    except Exception as exc:
        message = _safe_error(exc)
        result.update({"status": "error", "error": message})
        return result, [f"Square payments could not be loaded: {message}"]

    totals: dict[str, int] = {}
    completed_count = 0
    problems: list[str] = []
    for payment in payments:
        if getattr(payment, "status", None) != "COMPLETED":
            continue
        completed_count += 1
        source = _string_or_none(getattr(payment, "source_type", None))
        amount = _money_cents(getattr(payment, "total_money", None))
        payment_id = _string_or_none(getattr(payment, "id", None)) or "(unknown)"
        if source is None:
            problems.append(f"Completed Square payment {payment_id} has no source type.")
            continue
        if amount is None:
            problems.append(f"Completed Square payment {payment_id} has no total amount.")
            continue
        totals[source] = totals.get(source, 0) + amount

    # A successful empty response is a real zero-sales result.  Keep explicit
    # CASH and CARD zeroes so no later code mistakes absence for zero.
    totals.setdefault("CASH", 0)
    totals.setdefault("CARD", 0)
    result.update(
        {
            "status": "incomplete" if problems else "ok",
            "returned_count": len(payments),
            "completed_count": completed_count,
            "totals_by_source_cents": dict(sorted(totals.items())),
            "total_cents": sum(totals.values()) if not problems else None,
        }
    )
    return result, problems


def _shift_snapshot(shift: object) -> dict[str, Any]:
    return {
        "id": _string_or_none(getattr(shift, "id", None)),
        "state": _string_or_none(getattr(shift, "state", None)),
        "opened_at": _json_scalar(getattr(shift, "opened_at", None)),
        "ended_at": _json_scalar(getattr(shift, "ended_at", None)),
        "closed_at": _json_scalar(getattr(shift, "closed_at", None)),
        "opened_cash_cents": _money_cents(getattr(shift, "opened_cash_money", None)),
        "cash_payment_cents": _money_cents(getattr(shift, "cash_payment_money", None)),
        "cash_refunds_cents": _money_cents(getattr(shift, "cash_refunds_money", None)),
        "cash_paid_in_cents": _money_cents(getattr(shift, "cash_paid_in_money", None)),
        "cash_paid_out_cents": _money_cents(getattr(shift, "cash_paid_out_money", None)),
        "expected_cash_cents": _money_cents(getattr(shift, "expected_cash_money", None)),
        "closed_cash_cents": _money_cents(getattr(shift, "closed_cash_money", None)),
        "opening_team_member_id": _string_or_none(getattr(shift, "opening_team_member_id", None)),
        "closing_team_member_id": _string_or_none(getattr(shift, "closing_team_member_id", None)),
    }


def _comparison_specs(snapshot: dict[str, Any]) -> tuple[_ComparisonSpec, ...]:
    drawer = snapshot["drawer"]
    unique_shift = (
        drawer["shifts"][0]
        if drawer.get("status") in {"ok", "open"} and len(drawer.get("shifts", [])) == 1
        else {}
    )
    paid_in = unique_shift.get("cash_paid_in_cents")
    paid_out = unique_shift.get("cash_paid_out_cents")
    # Square returns paid-out and cash-refund event totals as negative Money.
    # The photographed drawer uses a signed paid-in/out net but shows cash
    # refunds as a positive magnitude (and its arithmetic subtracts them).
    paid_in_out = paid_in + paid_out if paid_in is not None and paid_out is not None else None
    square_refunds = unique_shift.get("cash_refunds_cents")
    paper_refund_magnitude = -square_refunds if square_refunds is not None else None

    payment_values = snapshot["payments"]
    totals = (
        payment_values.get("totals_by_source_cents", {})
        if payment_values.get("status") == "ok"
        else {}
    )
    return (
        _ComparisonSpec(
            "square.drawer.starting_cash",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "starting_cash_cents",
            unique_shift.get("opened_cash_cents"),
            "Starting cash must be present on both the drawer photo and Square shift.",
        ),
        _ComparisonSpec(
            "square.drawer.paid_in_out",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "paid_in_out_cents",
            paid_in_out,
            "Paid in/out requires both Square paid-in and paid-out amounts.",
        ),
        _ComparisonSpec(
            "square.drawer.cash_sales",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "cash_sales_cents",
            unique_shift.get("cash_payment_cents"),
            "Cash sales must be present on both the drawer photo and Square shift.",
        ),
        _ComparisonSpec(
            "square.drawer.cash_refunds",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "cash_refunds_cents",
            paper_refund_magnitude,
            "Cash refunds must be present on both sources; Square's signed refund is compared to the paper's positive magnitude.",
        ),
        _ComparisonSpec(
            "square.drawer.expected_cash",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "expected_in_drawer_cents",
            unique_shift.get("expected_cash_cents"),
            "Expected cash must be present on both the drawer photo and Square shift.",
        ),
        _ComparisonSpec(
            "square.drawer.counted_cash",
            DocumentType.SQUARE_DRAWER_SCREEN,
            "counted_cash_cents",
            unique_shift.get("closed_cash_cents"),
            "Counted cash requires an ended drawer photo and a closed Square shift.",
        ),
        _ComparisonSpec(
            "square.payments.cash",
            DocumentType.SQUARE_SALES_REPORT,
            "cash_cents",
            totals.get("CASH"),
            "Cash collected must be present on the sales report and Square payments.",
        ),
        _ComparisonSpec(
            "square.payments.card",
            DocumentType.SQUARE_SALES_REPORT,
            "card_cents",
            totals.get("CARD"),
            "Card collected must be present on the sales report and Square payments.",
        ),
        _ComparisonSpec(
            "square.payments.total_collected",
            DocumentType.SQUARE_SALES_REPORT,
            "total_collected_cents",
            payment_values.get("total_cents") if payment_values.get("status") == "ok" else None,
            "Total collected must be present on the sales report and Square payments.",
        ),
    )


def _compare_to_paper(
    paper_values: object,
    spec: _ComparisonSpec,
) -> dict[str, Any]:
    paper = _paper_cents(paper_values, spec.paper_document, spec.paper_field)
    square = spec.square_value
    tolerance = max(0, int(getattr(settings, "CASH_VARIANCE_TOLERANCE_CENTS", 0)))
    available = paper is not None and square is not None
    delta = square - paper if available else None
    passed = abs(delta) <= tolerance if delta is not None else None
    missing_sources: list[str] = []
    if paper is None:
        missing_sources.append("paper")
    if square is None:
        missing_sources.append("Square")
    detail = spec.detail
    if missing_sources:
        detail = f"{detail} Missing: {', '.join(missing_sources)}."
    return {
        "source": SQUARE_CHECK_SOURCE,
        "kind": "money",
        "name": spec.name,
        "required": True,
        "available": available,
        "passed": passed,
        "paper_cents": paper,
        "square_cents": square,
        "delta_cents": delta,
        "tolerance_cents": tolerance,
        "detail": detail,
    }


def _compare_unexplained_cash(
    reconciliation: DailyReconciliation,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Check that counted cash is explained by Square plus lottery activity."""

    shift = _unique_shift(snapshot)
    counted = reconciliation.counted_cash_cents
    expected = shift.get("expected_cash_cents") if shift else None
    explained = reconciliation.explained_cash_cents
    target = expected + explained if expected is not None and explained is not None else None
    available = counted is not None and target is not None
    unexplained = counted - target if available else None
    tolerance = max(0, int(getattr(settings, "CASH_VARIANCE_TOLERANCE_CENTS", 0)))
    missing_sources: list[str] = []
    if counted is None:
        missing_sources.append("photo counted cash")
    if expected is None:
        missing_sources.append("Square expected cash")
    if explained is None:
        missing_sources.append("lottery explanation")
    detail = (
        "Counted cash must equal Square expected cash plus the configured lottery cash explanation."
    )
    if missing_sources:
        detail = f"{detail} Missing: {', '.join(missing_sources)}."
    return {
        "source": SQUARE_CHECK_SOURCE,
        "kind": "money",
        "name": "square.drawer.unexplained_variance",
        "required": True,
        "available": available,
        "passed": abs(unexplained) <= tolerance if unexplained is not None else None,
        "paper_cents": counted,
        "square_cents": target,
        "delta_cents": unexplained,
        "tolerance_cents": tolerance,
        "detail": detail,
    }


def _compare_closing_team_member(
    reconciliation: DailyReconciliation,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Bind the single closed Square drawer to the employee who submitted it."""

    shift = _unique_shift(snapshot)
    employee_team_member_id = _string_or_none(
        reconciliation.submission.submitted_by.square_team_member_id
    )
    square_team_member_id = _string_or_none(shift.get("closing_team_member_id")) if shift else None
    available = employee_team_member_id is not None and square_team_member_id is not None
    missing_sources: list[str] = []
    if employee_team_member_id is None:
        missing_sources.append("employee Square team member mapping")
    if square_team_member_id is None:
        missing_sources.append("Square closing team member")
    detail = "The Square drawer closer must be the employee who submitted this day."
    if missing_sources:
        detail = f"{detail} Missing: {', '.join(missing_sources)}."
    return {
        "source": SQUARE_CHECK_SOURCE,
        "kind": "identity",
        "name": "square.drawer.closing_team_member",
        "required": True,
        "available": available,
        "passed": (employee_team_member_id == square_team_member_id if available else None),
        "paper_value": employee_team_member_id,
        "square_value": square_team_member_id,
        # Keep the common shape used by the reconciliation template and older
        # API consumers while identifying this as a non-money comparison.
        "paper_cents": None,
        "square_cents": None,
        "delta_cents": None,
        "tolerance_cents": None,
        "detail": detail,
    }


def _paper_cents(paper_values: object, document_type: str, field: str) -> int | None:
    if not isinstance(paper_values, dict):
        return None
    document = paper_values.get(document_type)
    if not isinstance(document, dict):
        return None
    value: Any = document.get(field)
    if isinstance(value, dict):
        # Current extraction evidence uses explicit present/legible booleans.
        # ``presence`` remains understood for already-stored v1 drafts.
        if value.get("present") is False or value.get("legible") is False:
            return None
        presence = value.get("presence")
        if presence is not None and presence != "PRESENT":
            return None
        value = value.get("value")
    return _cents_or_none(value)


def _persist_result(
    reconciliation: DailyReconciliation,
    snapshot: dict[str, Any],
    comparisons: tuple[dict[str, Any], ...],
    problems: list[str],
    *,
    on_persist: Callable[[DailyReconciliation, DailySquareSyncResult], None] | None = None,
) -> DailySquareSyncResult:
    snapshot = _json_safe(snapshot)
    comparisons = tuple(_json_safe(item) for item in comparisons)
    sync_time = timezone.now()

    with transaction.atomic():
        locked = DailyReconciliation.objects.select_for_update().get(pk=reconciliation.pk)
        existing_checks = locked.check_results if isinstance(locked.check_results, list) else []
        paper_checks = [
            check
            for check in existing_checks
            if not (isinstance(check, dict) and check.get("source") == SQUARE_CHECK_SOURCE)
        ]
        hard_paper_failure = any(
            isinstance(check, dict)
            and check.get("passed") is False
            and check.get("severity", "hard") == "hard"
            for check in paper_checks
        )
        unavailable = any(not comparison["available"] for comparison in comparisons)
        square_mismatch = any(comparison["passed"] is False for comparison in comparisons)

        if square_mismatch or hard_paper_failure:
            computed_status = ReconciliationStatus.MISMATCH
        elif problems or unavailable or not comparisons or locked.missing_evidence:
            computed_status = ReconciliationStatus.INCOMPLETE
        else:
            computed_status = ReconciliationStatus.MATCHED

        # Owner approval is a final human decision.  A later refresh may add a
        # newer snapshot, but must not silently reopen an approved day.
        persisted_status = (
            ReconciliationStatus.APPROVED
            if locked.status == ReconciliationStatus.APPROVED
            else computed_status
        )
        unique_shift = _unique_shift(snapshot)
        expected_cash = unique_shift.get("expected_cash_cents") if unique_shift else None
        unexplained = None
        if (
            locked.counted_cash_cents is not None
            and expected_cash is not None
            and locked.explained_cash_cents is not None
        ):
            unexplained = locked.counted_cash_cents - expected_cash - locked.explained_cash_cents

        locked.status = persisted_status
        locked.square_values = snapshot
        locked.check_results = [*paper_checks, *comparisons]
        locked.square_expected_cash_cents = expected_cash
        locked.unexplained_variance_cents = unexplained
        locked.square_synced_at = sync_time
        locked.save(
            update_fields=[
                "status",
                "square_values",
                "check_results",
                "square_expected_cash_cents",
                "unexplained_variance_cents",
                "square_synced_at",
                "updated_at",
            ]
        )

        result = DailySquareSyncResult(
            reconciliation_id=str(locked.pk),
            status=persisted_status,
            square_values=snapshot,
            comparisons=comparisons,
            problems=tuple(dict.fromkeys(problems)),
        )
        if on_persist is not None:
            # The network work is already complete.  This hook lets the web
            # layer append its audit event in the same short transaction as
            # the snapshot, so neither record can commit alone.
            on_persist(locked, result)

    reconciliation.status = persisted_status
    reconciliation.square_values = snapshot
    reconciliation.check_results = [*paper_checks, *comparisons]
    reconciliation.square_expected_cash_cents = expected_cash
    reconciliation.unexplained_variance_cents = unexplained
    reconciliation.square_synced_at = sync_time
    return result


def _unique_shift(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    drawer = snapshot.get("drawer", {})
    shifts = drawer.get("shifts", [])
    if drawer.get("status") == "ok" and len(shifts) == 1:
        return shifts[0]
    return None


def _money_cents(money: object) -> int | None:
    if money is None:
        return None
    return _cents_or_none(getattr(money, "amount", None))


def _cents_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _json_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:500]
