"""Idempotent materialization of extracted evidence into workflow records.

This module is intentionally a post-processing hook.  Vision extraction owns
the evidence in ``Document.extracted_data``; these records are convenient,
reviewable projections for the inventory, payout and reconciliation workflows.
Reviewed or already-posted inventory data is never overwritten by a rerun.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.capture.models import DocumentStatus, DocumentType, Submission, SubmissionKind


def materialize_submission(submission: Submission) -> None:
    """Populate the one downstream record appropriate to ``submission.kind``."""

    if submission.kind == SubmissionKind.INVENTORY:
        _materialize_delivery(submission)
    elif submission.kind == SubmissionKind.PAYOUT:
        _materialize_payout(submission)
    elif submission.kind == SubmissionKind.DAILY_REPORT:
        _materialize_daily_reconciliation(submission)


def _usable_documents(submission: Submission):
    return submission.documents.filter(
        status__in=[
            DocumentStatus.EXTRACTED,
            DocumentStatus.REVIEWED,
            DocumentStatus.NEEDS_REVIEW,
        ]
    ).order_by("created_at")


def _document_result(document) -> dict[str, Any] | None:
    payload = document.extracted_data
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def _value(payload: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    if isinstance(current, dict) and "value" in current:
        return current.get("value")
    return current if current is not None else default


@transaction.atomic
def _materialize_delivery(submission: Submission) -> None:
    from apps.inventory.models import (
        Delivery,
        DeliveryLine,
        DeliveryStatus,
        LineMatchStatus,
        Vendor,
    )
    from apps.inventory.packs import is_non_stock_line

    documents = list(
        _usable_documents(submission).filter(detected_type=DocumentType.DELIVERY_INVOICE)
    )
    results = [result for document in documents if (result := _document_result(document))]
    if not results:
        return

    delivery, created = Delivery.objects.select_for_update().get_or_create(
        submission=submission,
        defaults={"status": DeliveryStatus.EXTRACTING},
    )
    # A rerun after a push must remain historical evidence, never a rewrite of
    # stock which already moved in Square.
    if delivery.status in {
        DeliveryStatus.PUSHING,
        DeliveryStatus.PUSHED,
        DeliveryStatus.PUSHED_WITH_DRIFT,
        DeliveryStatus.PUSHED_UNVERIFIED,
    } or bool(delivery.square_batch_keys):
        return

    vendor_name = _first_text(results, "vendor_name") or delivery.vendor_name_raw
    delivery.vendor_name_raw = vendor_name
    delivery.vendor = (
        Vendor.objects.filter(name__iexact=vendor_name, active=True).first()
        if vendor_name
        else None
    )
    delivery.invoice_number = (_first_text(results, "invoice_number") or delivery.invoice_number)[
        :100
    ]
    delivery.invoice_date = _first_date(results, "invoice_date") or delivery.invoice_date
    invoice_total_cents = _last_int(results, "invoice_total_cents")
    if invoice_total_cents is not None:
        delivery.invoice_total_cents = invoice_total_cents
    delivery.status = DeliveryStatus.NEEDS_REVIEW
    delivery.save(
        update_fields=[
            "vendor_name_raw",
            "vendor",
            "invoice_number",
            "invoice_date",
            "invoice_total_cents",
            "status",
            "updated_at",
        ]
    )

    # Existing rows may contain owner corrections or catalogue matches.  Only
    # seed lines into a new/empty delivery; never overwrite reviewed values.
    if not created and delivery.lines.exists():
        return

    raw_lines: list[Any] = []
    for result in results:
        result_lines = result.get("lines")
        if isinstance(result_lines, list):
            raw_lines.extend(result_lines)
    lines: list[DeliveryLine] = []
    for fallback_position, raw_line in enumerate(raw_lines, start=1):
        if not isinstance(raw_line, dict):
            continue
        description = str(_value(raw_line, "description", "") or "").strip()
        position = _positive_int(_value(raw_line, "line_number")) or fallback_position
        non_stock = is_non_stock_line(description)
        lines.append(
            DeliveryLine(
                delivery=delivery,
                position=position,
                vendor_sku=str(_value(raw_line, "vendor_sku", "") or "")[:100],
                upc="".join(
                    character
                    for character in str(_value(raw_line, "upc", "") or "")
                    if character.isdigit()
                )[:14],
                description=description[:300],
                pack_text=str(_value(raw_line, "pack_text", "") or "")[:80],
                cases=_decimal_or_none(_value(raw_line, "cases")),
                received_units=_decimal_or_none(_value(raw_line, "stated_units")),
                unit_cost_cents=_int_or_none(_value(raw_line, "unit_cost_cents")),
                line_total_cents=_int_or_none(_value(raw_line, "line_total_cents")),
                included=not non_stock,
                match_status=(LineMatchStatus.EXCLUDED if non_stock else LineMatchStatus.UNMATCHED),
                review_note="Non-stock charge detected from invoice description"
                if non_stock
                else "",
            )
        )
    # A duplicate printed line number must not violate the database constraint.
    # Preserve visual order and use a deterministic position when duplicates occur.
    seen: set[int] = set()
    for fallback_position, line in enumerate(lines, start=1):
        if line.position in seen:
            line.position = fallback_position
            while line.position in seen:
                line.position += 1
        seen.add(line.position)
    DeliveryLine.objects.bulk_create(lines)

    # Inventory owns catalogue matching, pack interpretation and readiness.
    # Calling its public normalizer here keeps that policy out of extraction.
    from apps.inventory.services import refresh_delivery_readiness

    refresh_delivery_readiness(delivery)


@transaction.atomic
def _materialize_payout(submission: Submission) -> None:
    from apps.reconcile.models import PayoutRecord, PayoutStatus

    document = (
        _usable_documents(submission).filter(detected_type=DocumentType.LOTTERY_PAYOUT).first()
    )
    if document is None or (result := _document_result(document)) is None:
        return

    payout, _ = PayoutRecord.objects.select_for_update().get_or_create(submission=submission)
    if payout.status != PayoutStatus.PENDING:
        return
    amount_cents = _int_or_none(_value(result, "amount_cents"))
    game_name = str(_value(result, "game_name", "") or "").strip()
    ticket_reference = str(_value(result, "ticket_reference", "") or "").strip()
    validation_reference = str(_value(result, "validation_reference", "") or "").strip()
    paid_at = _aware_datetime(_value(result, "payout_date"), _value(result, "payout_time"))
    payout.extracted_amount_cents = amount_cents
    if payout.amount_confirmed_at is None:
        if (
            payout.employee_reported_amount_cents is not None
            and amount_cents is not None
            and payout.employee_reported_amount_cents != amount_cents
        ):
            # Independent readings disagree. Preserve both and require the
            # owner to choose the reviewed ledger amount.
            payout.amount_cents = None
        elif amount_cents is not None:
            payout.amount_cents = amount_cents
        else:
            payout.amount_cents = payout.employee_reported_amount_cents
    if game_name:
        payout.game_name = game_name[:120]
    if ticket_reference:
        payout.ticket_reference = ticket_reference[:120]
    if validation_reference:
        payout.validation_reference = validation_reference[:120]
    if paid_at is not None:
        payout.paid_at = paid_at
    payout.save(
        update_fields=[
            "amount_cents",
            "extracted_amount_cents",
            "game_name",
            "ticket_reference",
            "validation_reference",
            "paid_at",
            "updated_at",
        ]
    )


@transaction.atomic
def _materialize_daily_reconciliation(submission: Submission) -> None:
    from apps.reconcile.models import (
        DailyReconciliation,
        ReconciliationStatus,
        cash_collection_evidence_hash,
    )

    # Follow the same lock order as owner approval and physical collection so
    # a background re-read cannot leave a decision bound to older evidence.
    submission = Submission.objects.select_for_update().get(pk=submission.pk)
    documents = list(_usable_documents(submission))
    by_type = {document.detected_type: document for document in documents}
    required = {
        DocumentType.SQUARE_SALES_REPORT,
        DocumentType.SQUARE_DRAWER_SCREEN,
        DocumentType.LOTTERY_DAILY_SALES,
        DocumentType.LOTTERY_TICKET_BALANCE,
    }
    missing = sorted(required - set(by_type))
    paper_values: dict[str, Any] = {}
    combined_checks: list[dict[str, Any]] = []
    for document_type, document in by_type.items():
        result = _document_result(document)
        if result is not None:
            paper_values[document_type] = result
            if document_type in {
                DocumentType.SQUARE_SALES_REPORT,
                DocumentType.LOTTERY_DAILY_SALES,
                DocumentType.LOTTERY_TICKET_BALANCE,
            }:
                report_date = _date(_value(result, "report_date"))
                combined_checks.append(
                    {
                        "source": "workflow",
                        "name": f"business_day:{document_type}",
                        "passed": report_date == submission.business_day,
                        "severity": "hard",
                        "expected_date": submission.business_day.isoformat(),
                        "actual_date": report_date.isoformat() if report_date else None,
                        "detail": (
                            "The printed report date must match the business day selected "
                            "for this submission."
                        ),
                    }
                )
        for check in document.check_results if isinstance(document.check_results, list) else []:
            if isinstance(check, dict):
                combined_checks.append(
                    {"document_id": str(document.pk), "document_type": document_type, **check}
                )

    reconciliation, _ = DailyReconciliation.objects.select_for_update().get_or_create(
        submission=submission
    )
    if reconciliation.status == ReconciliationStatus.APPROVED:
        return

    drawer = paper_values.get(DocumentType.SQUARE_DRAWER_SCREEN, {})
    sales_report = paper_values.get(DocumentType.SQUARE_SALES_REPORT, {})
    lottery_daily = paper_values.get(DocumentType.LOTTERY_DAILY_SALES, {})
    ticket_balance = paper_values.get(DocumentType.LOTTERY_TICKET_BALANCE, {})
    counted = _int_or_none(_value(drawer, "counted_cash_cents"))
    expected = _int_or_none(_value(drawer, "expected_in_drawer_cents"))
    lottery_sales = _int_or_none(_value(ticket_balance, "shift_total_cents"))
    lottery_payouts = _int_or_none(_value(lottery_daily, "pays.amount_cents"))

    report_cash = _int_or_none(_value(sales_report, "cash_cents"))
    drawer_cash_sales = _int_or_none(_value(drawer, "cash_sales_cents"))
    if report_cash is not None and drawer_cash_sales is not None:
        combined_checks.append(
            {
                "name": "sales_report.cash = drawer.cash_sales",
                "passed": report_cash == drawer_cash_sales,
                "severity": "hard",
                "expected_cents": report_cash,
                "actual_cents": drawer_cash_sales,
                "delta_cents": drawer_cash_sales - report_cash,
                "detail": "The two Square photos must describe the same day and drawer.",
            }
        )

    explained: int | None = None
    unexplained: int | None = None
    if lottery_sales is not None and lottery_payouts is not None:
        explained = 0 if settings.LOTTERY_RINGS_THROUGH_POS else lottery_sales - lottery_payouts
    if counted is not None and expected is not None and explained is not None:
        unexplained = counted - expected - explained

    collection_hash = cash_collection_evidence_hash(
        submission_id=submission.pk,
        business_day=submission.business_day,
        counted_cash_cents=counted,
        paper_expected_cash_cents=expected,
    )
    collection_is_stale = (
        reconciliation.owner_collected_cents is not None
        and reconciliation.collection_evidence_hash != collection_hash
    )
    if collection_is_stale:
        # The prior physical count was reviewed against different paper
        # evidence.  Make the owner record it again instead of carrying a
        # stale acknowledgement into approval.
        reconciliation.drawer_float_cents = None
        reconciliation.expected_collection_cents = None
        reconciliation.owner_collected_cents = None
        reconciliation.collection_variance_cents = None
        reconciliation.collection_note = ""
        reconciliation.collection_evidence_hash = ""
        reconciliation.collection_recorded_at = None
        reconciliation.collection_recorded_by = None

    hard_failure = any(
        check.get("passed") is False and check.get("severity", "hard") == "hard"
        for check in combined_checks
    )
    if missing:
        status = ReconciliationStatus.INCOMPLETE
    elif hard_failure:
        status = ReconciliationStatus.MISMATCH
    else:
        # Paper is internally consistent, but the Square API pull is a separate
        # source of truth and must still complete before this can be MATCHED.
        status = ReconciliationStatus.PROVISIONAL

    reconciliation.status = status
    reconciliation.paper_values = paper_values
    reconciliation.check_results = combined_checks
    reconciliation.missing_evidence = missing
    reconciliation.counted_cash_cents = counted
    reconciliation.paper_expected_cash_cents = expected
    reconciliation.lottery_sales_cents = lottery_sales
    reconciliation.lottery_payouts_cents = lottery_payouts
    reconciliation.explained_cash_cents = explained
    reconciliation.unexplained_variance_cents = unexplained
    reconciliation.save(
        update_fields=[
            "status",
            "paper_values",
            "check_results",
            "missing_evidence",
            "counted_cash_cents",
            "paper_expected_cash_cents",
            "lottery_sales_cents",
            "lottery_payouts_cents",
            "explained_cash_cents",
            "unexplained_variance_cents",
            "drawer_float_cents",
            "expected_collection_cents",
            "owner_collected_cents",
            "collection_variance_cents",
            "collection_note",
            "collection_evidence_hash",
            "collection_recorded_at",
            "collection_recorded_by",
            "updated_at",
        ]
    )


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _first_text(results: list[dict[str, Any]], path: str) -> str:
    for result in results:
        value = _value(result, path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _first_date(results: list[dict[str, Any]], path: str) -> dt.date | None:
    for result in results:
        value = _date(_value(result, path))
        if value is not None:
            return value
    return None


def _last_int(results: list[dict[str, Any]], path: str) -> int | None:
    for result in reversed(results):
        value = _int_or_none(_value(result, path))
        if value is not None:
            return value
    return None


def _aware_datetime(date_value: Any, time_value: Any) -> dt.datetime | None:
    day = _date(date_value)
    if day is None or not isinstance(time_value, str):
        return None
    parsed_time: dt.time | None = None
    for pattern in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            parsed_time = dt.datetime.strptime(time_value.strip(), pattern).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None
    zone = ZoneInfo(settings.STORE_TIMEZONE)
    return timezone.make_aware(dt.datetime.combine(day, parsed_time), timezone=zone)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int | None:
    parsed = _int_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
