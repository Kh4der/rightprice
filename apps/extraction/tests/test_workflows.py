from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.extraction.processing import process_submission
from apps.extraction.workflows import materialize_submission
from apps.inventory.models import DeliveryStatus, LineMatchStatus
from apps.reconcile.models import PayoutStatus, ReconciliationStatus


def evidence(value):
    return {
        "value": value,
        "verbatim": str(value),
        "present": True,
        "legible": True,
        "location": "row",
    }


@pytest.fixture
def employee(db):
    return User.objects.create_user(login_code="M8", pin="1234", display_name="Materializer")


def extracted_document(submission, document_type, result, *, checks=None, index=1):
    return Document.objects.create(
        submission=submission,
        file=f"test/{submission.pk}/{index}.jpg",
        original_name=f"{document_type}.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        sha256=f"{index:064x}",
        detected_type=document_type,
        status=DocumentStatus.EXTRACTED,
        extracted_data={"schema_version": 1, "classification": {}, "result": result},
        check_results=checks or [],
    )


@pytest.mark.django_db
def test_process_submission_materializes_delivery_without_guessing_catalog_match(employee):
    submission = Submission.objects.create(kind=SubmissionKind.INVENTORY, submitted_by=employee)
    extracted_document(
        submission,
        DocumentType.DELIVERY_INVOICE,
        {
            "vendor_name": evidence("Southern Distributor"),
            "invoice_number": evidence("INV-42"),
            "invoice_date": evidence("2026-09-26"),
            "invoice_total_cents": evidence(12_500),
            "lines": [
                {
                    "line_number": evidence(1),
                    "vendor_sku": evidence("SKU-1"),
                    "upc": evidence("0 12345 67890 5"),
                    "description": evidence("Example Vodka 750ML"),
                    "pack_text": evidence("12/750ML"),
                    "cases": evidence("2"),
                    "stated_units": evidence("24"),
                    "unit_cost_cents": evidence(500),
                    "line_total_cents": evidence(12_000),
                },
                {
                    "line_number": evidence(2),
                    "vendor_sku": evidence("FEE"),
                    "upc": evidence(""),
                    "description": evidence("Bottle deposit"),
                    "pack_text": evidence(""),
                    "cases": evidence("1"),
                    "stated_units": evidence("1"),
                    "unit_cost_cents": evidence(500),
                    "line_total_cents": evidence(500),
                },
            ],
        },
    )

    result = process_submission(submission.pk)
    delivery = result.delivery
    lines = list(delivery.lines.all())

    assert result.status == SubmissionStatus.READY
    assert delivery.status == DeliveryStatus.NEEDS_REVIEW
    assert delivery.invoice_number == "INV-42"
    assert delivery.invoice_date == dt.date(2026, 9, 26)
    assert delivery.invoice_total_cents == 12_500
    assert lines[0].upc == "012345678905"
    assert lines[0].match_status == LineMatchStatus.UNMATCHED
    assert lines[1].match_status == LineMatchStatus.EXCLUDED
    assert not lines[1].included

    # Re-materialization must preserve a reviewed catalogue match.
    lines[0].match_status = LineMatchStatus.MATCHED
    lines[0].square_catalog_variation_id = "variation-reviewed"
    lines[0].save(update_fields=["match_status", "square_catalog_variation_id"])
    process_submission(submission.pk)
    lines[0].refresh_from_db()
    assert lines[0].square_catalog_variation_id == "variation-reviewed"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "batch_keys"),
    [
        (DeliveryStatus.PUSHED_WITH_DRIFT, []),
        (DeliveryStatus.PUSHED_UNVERIFIED, []),
        (DeliveryStatus.FAILED, ["protected-key"]),
        (DeliveryStatus.NEEDS_REVIEW, ["protected-key"]),
    ],
)
def test_inventory_materialization_never_reopens_posted_or_claimed_delivery(
    employee, status, batch_keys
):
    submission = Submission.objects.create(kind=SubmissionKind.INVENTORY, submitted_by=employee)
    extracted_document(
        submission,
        DocumentType.DELIVERY_INVOICE,
        {
            "vendor_name": evidence("Distributor"),
            "invoice_number": evidence("INV-PROTECTED"),
            "lines": [],
        },
    )
    materialize_submission(submission)
    delivery = submission.delivery
    delivery.status = status
    delivery.square_batch_keys = batch_keys
    delivery.save(update_fields=["status", "square_batch_keys", "updated_at"])

    materialize_submission(submission)

    delivery.refresh_from_db()
    assert delivery.status == status
    assert delivery.square_batch_keys == batch_keys


@pytest.mark.django_db
def test_process_submission_materializes_pending_payout(employee):
    submission = Submission.objects.create(kind=SubmissionKind.PAYOUT, submitted_by=employee)
    extracted_document(
        submission,
        DocumentType.LOTTERY_PAYOUT,
        {
            "payout_date": evidence("2026-09-26"),
            "payout_time": evidence("12:15:30"),
            "amount_cents": evidence(50_000),
            "game_name": evidence("Florida Lotto"),
            "ticket_reference": evidence("TICKET-99"),
            "validation_reference": evidence("VALID-31415"),
        },
    )

    result = process_submission(submission.pk)
    payout = result.payout_record

    assert payout.status == PayoutStatus.PENDING
    assert payout.amount_cents == 50_000
    assert payout.ticket_reference == "TICKET-99"
    assert payout.validation_reference == "VALID-31415"
    assert (
        payout.paid_at.astimezone(ZoneInfo(settings.STORE_TIMEZONE))
        .isoformat()
        .startswith("2026-09-26T12:15:30")
    )


@pytest.mark.django_db
def test_daily_materialization_collects_paper_values_and_missing_evidence(employee):
    submission = Submission.objects.create(kind=SubmissionKind.DAILY_REPORT, submitted_by=employee)
    extracted_document(
        submission,
        DocumentType.SQUARE_DRAWER_SCREEN,
        {
            "counted_cash_cents": evidence(40_000),
            "expected_in_drawer_cents": evidence(30_000),
        },
        index=1,
    )
    extracted_document(
        submission,
        DocumentType.LOTTERY_DAILY_SALES,
        {"pays": {"amount_cents": evidence(2_000)}},
        index=2,
    )
    extracted_document(
        submission,
        DocumentType.LOTTERY_TICKET_BALANCE,
        {"shift_total_cents": evidence(12_000)},
        index=3,
    )

    result = process_submission(submission.pk)
    reconciliation = result.daily_reconciliation

    assert reconciliation.status == ReconciliationStatus.INCOMPLETE
    assert reconciliation.missing_evidence == [DocumentType.SQUARE_SALES_REPORT]
    assert reconciliation.counted_cash_cents == 40_000
    assert reconciliation.paper_expected_cash_cents == 30_000
    assert reconciliation.square_expected_cash_cents is None
    assert reconciliation.lottery_sales_cents == 12_000
    assert reconciliation.lottery_payouts_cents == 2_000
    assert reconciliation.explained_cash_cents == 10_000
    assert reconciliation.unexplained_variance_cents == 0


@pytest.mark.django_db
def test_daily_report_dates_must_match_selected_business_day(employee):
    business_day = dt.date(2026, 9, 26)
    submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        business_day=business_day,
        submitted_by=employee,
    )
    extracted_document(
        submission,
        DocumentType.SQUARE_SALES_REPORT,
        {"report_date": evidence("2026-09-26")},
        index=1,
    )
    extracted_document(
        submission,
        DocumentType.SQUARE_DRAWER_SCREEN,
        {
            "counted_cash_cents": evidence(30_000),
            "expected_in_drawer_cents": evidence(30_000),
        },
        index=2,
    )
    extracted_document(
        submission,
        DocumentType.LOTTERY_DAILY_SALES,
        {
            "report_date": evidence("2026-09-25"),
            "pays": {"amount_cents": evidence(0)},
        },
        index=3,
    )
    extracted_document(
        submission,
        DocumentType.LOTTERY_TICKET_BALANCE,
        {
            "report_date": evidence("2026-09-26"),
            "shift_total_cents": evidence(0),
        },
        index=4,
    )

    result = process_submission(submission.pk)
    reconciliation = result.daily_reconciliation
    checks = {
        check["name"]: check
        for check in reconciliation.check_results
        if check.get("source") == "workflow"
    }

    assert reconciliation.status == ReconciliationStatus.MISMATCH
    assert checks["business_day:SQUARE_SALES_REPORT"]["passed"] is True
    mismatch = checks["business_day:LOTTERY_DAILY_SALES"]
    assert mismatch["passed"] is False
    assert mismatch["expected_date"] == "2026-09-26"
    assert mismatch["actual_date"] == "2026-09-25"


@pytest.mark.django_db
def test_rematerialized_cash_evidence_invalidates_owner_collection(employee):
    submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        business_day=dt.date(2026, 9, 26),
        submitted_by=employee,
    )
    drawer = extracted_document(
        submission,
        DocumentType.SQUARE_DRAWER_SCREEN,
        {
            "counted_cash_cents": evidence(40_000),
            "expected_in_drawer_cents": evidence(30_000),
        },
    )
    result = process_submission(submission.pk)
    reconciliation = result.daily_reconciliation
    reconciliation.drawer_float_cents = 6_500
    reconciliation.expected_collection_cents = 33_500
    reconciliation.owner_collected_cents = 33_500
    reconciliation.collection_variance_cents = 0
    reconciliation.collection_note = "Reviewed original drawer"
    reconciliation.collection_evidence_hash = reconciliation.current_collection_evidence_hash()
    reconciliation.collection_recorded_at = timezone.now()
    reconciliation.collection_recorded_by = employee
    reconciliation.save()

    drawer.extracted_data["result"]["counted_cash_cents"] = evidence(41_000)
    drawer.save(update_fields=["extracted_data"])
    process_submission(submission.pk)

    reconciliation.refresh_from_db()
    assert reconciliation.counted_cash_cents == 41_000
    assert reconciliation.owner_collected_cents is None
    assert reconciliation.collection_recorded_at is None
    assert reconciliation.collection_recorded_by is None
    assert reconciliation.collection_evidence_hash == ""
