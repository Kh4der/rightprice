import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.core.models import DEFAULT_FLOAT_CENTS
from apps.reconcile.models import (
    DailyCashCount,
    DailyReconciliation,
    ReconciliationStatus,
)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "CASHOWN",
        "owner-password",
        display_name="Cash Owner",
        role=Role.OWNER,
        square_team_member_id="TM-daily-cash-owner",
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "CASH01",
        "1234",
        display_name="Cash Employee",
        square_team_member_id="TM-daily-cash-employee",
    )


def make_daily(
    employee,
    *,
    day=dt.date(2026, 9, 21),
    submission_status=SubmissionStatus.APPROVED,
    reconciliation_status=ReconciliationStatus.APPROVED,
    counted_cash_cents=50_000,
):
    submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        status=submission_status,
        business_day=day,
        submitted_by=employee,
        approved_at=timezone.now() if submission_status == SubmissionStatus.APPROVED else None,
    )
    reconciliation = DailyReconciliation.objects.create(
        submission=submission,
        status=reconciliation_status,
        counted_cash_cents=counted_cash_cents,
    )
    return submission, reconciliation


def add_extracted_document(submission):
    return Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("daily.jpg", b"daily report", "image/jpeg"),
        original_name="daily.jpg",
        media_type="image/jpeg",
        size_bytes=12,
        sha256="d" * 64,
        requested_type=DocumentType.SQUARE_DRAWER_SCREEN,
        status=DocumentStatus.EXTRACTED,
    )


def make_count(reconciliation, owner, *, counted_cents=23_500):
    return DailyCashCount.objects.create(
        reconciliation=reconciliation,
        drawer_float_cents=DEFAULT_FLOAT_CENTS,
        expected_cents=23_500,
        counted_cents=counted_cents,
        variance_cents=counted_cents - 23_500,
        evidence_hash="e" * 64,
        entered_by=owner,
    )


def test_daily_cash_count_revisions_are_append_only(owner, employee):
    _submission, reconciliation = make_daily(employee)
    count = make_count(reconciliation, owner)

    count.counted_cents = 1
    with pytest.raises(ValidationError, match="cannot be changed"):
        count.save()
    with pytest.raises(ValidationError, match="cannot be changed"):
        DailyCashCount.objects.filter(pk=count.pk).update(counted_cents=1)
    with pytest.raises(ValidationError, match="cannot be deleted"):
        DailyCashCount.objects.filter(pk=count.pk).delete()
    with pytest.raises(ValidationError, match="cannot be deleted"):
        count.delete()

    stored = DailyCashCount.objects.get(pk=count.pk)
    assert stored.counted_cents == 23_500
    assert DailyCashCount.objects.count() == 1


def test_daily_evidence_can_be_approved_before_owner_counts_daily_cash(
    client,
    owner,
    employee,
):
    submission, reconciliation = make_daily(
        employee,
        submission_status=SubmissionStatus.READY,
        reconciliation_status=ReconciliationStatus.MATCHED,
    )
    add_extracted_document(submission)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    reconciliation.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert reconciliation.status == ReconciliationStatus.APPROVED
    assert reconciliation.owner_collected_cents is None
    assert not DailyCashCount.objects.exists()


def test_daily_cash_count_is_owner_only_and_post_only(client, owner, employee):
    submission, _reconciliation = make_daily(employee)
    url = reverse("reconcile:daily-cash-count", args=[submission.pk])

    client.force_login(employee)
    assert client.post(url, {"amount": "235.00"}).status_code == 403

    client.force_login(owner)
    assert client.get(url).status_code == 405
    assert not DailyCashCount.objects.exists()


def test_daily_cash_count_refuses_an_approved_non_daily_submission(
    client,
    owner,
    employee,
):
    submission = Submission.objects.create(
        kind=SubmissionKind.PAYOUT,
        status=SubmissionStatus.APPROVED,
        business_day=dt.date(2026, 9, 21),
        submitted_by=employee,
    )
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": "235.00"},
    )

    assert response.status_code == 404
    assert not DailyCashCount.objects.exists()


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.DRAFT,
        SubmissionStatus.QUEUED,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.NEEDS_REVIEW,
        SubmissionStatus.READY,
        SubmissionStatus.REJECTED,
        SubmissionStatus.FAILED,
    ],
)
def test_daily_cash_count_refuses_every_unapproved_daily_submission(
    client,
    owner,
    employee,
    status,
):
    submission, reconciliation = make_daily(
        employee,
        submission_status=status,
        reconciliation_status=ReconciliationStatus.MATCHED,
    )
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": "235.00"},
    )

    assert response.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents is None
    assert not DailyCashCount.objects.exists()
    assert not AuditEvent.objects.filter(
        action__in=["cash.daily_counted", "cash.daily_corrected"]
    ).exists()


def test_first_mismatched_count_is_saved_as_an_issue(client, owner, employee):
    submission, reconciliation = make_daily(employee)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": "234.99", "note": "First physical count"},
    )

    assert response.status_code == 302
    count = DailyCashCount.objects.get()
    assert count.correction_of is None
    assert count.drawer_float_cents == DEFAULT_FLOAT_CENTS
    assert count.expected_cents == 23_500
    assert count.counted_cents == 23_499
    assert count.variance_cents == -1
    assert count.note == "First physical count"
    assert len(count.evidence_hash) == 64
    assert count.entered_by == owner

    reconciliation.refresh_from_db()
    assert reconciliation.drawer_float_cents == DEFAULT_FLOAT_CENTS
    assert reconciliation.expected_collection_cents == 23_500
    assert reconciliation.owner_collected_cents == 23_499
    assert reconciliation.collection_variance_cents == -1
    assert reconciliation.collection_note == "First physical count"
    assert reconciliation.collection_evidence_hash == count.evidence_hash
    assert reconciliation.collection_recorded_at is not None
    assert reconciliation.collection_recorded_by == owner

    event = AuditEvent.objects.get(action="cash.daily_counted")
    assert event.actor == owner
    assert event.target_id == str(count.pk)


def test_correction_updates_latest_snapshot_but_preserves_history_and_audit(
    client,
    owner,
    employee,
):
    submission, reconciliation = make_daily(employee)
    url = reverse("reconcile:daily-cash-count", args=[submission.pk])
    client.force_login(owner)

    client.post(url, {"amount": "234.99", "note": "Initial count"})
    first = DailyCashCount.objects.get()
    client.post(url, {"amount": "235.00", "note": "Recounted daily cash"})

    revisions = list(DailyCashCount.objects.order_by("created_at", "pk"))
    assert len(revisions) == 2
    assert revisions[0] == first
    assert revisions[0].counted_cents == 23_499
    assert revisions[0].variance_cents == -1
    assert revisions[1].correction_of == revisions[0]
    assert revisions[1].counted_cents == 23_500
    assert revisions[1].variance_cents == 0
    assert revisions[1].note == "Recounted daily cash"

    reconciliation.refresh_from_db()
    assert reconciliation.expected_collection_cents == 23_500
    assert reconciliation.owner_collected_cents == 23_500
    assert reconciliation.collection_variance_cents == 0
    assert reconciliation.collection_note == "Recounted daily cash"
    assert reconciliation.collection_evidence_hash == revisions[1].evidence_hash

    assert AuditEvent.objects.filter(action="cash.daily_counted", actor=owner).count() == 1
    assert AuditEvent.objects.filter(action="cash.daily_corrected", actor=owner).count() == 1


def test_exact_duplicate_count_is_a_noop(client, owner, employee):
    submission, _reconciliation = make_daily(employee)
    url = reverse("reconcile:daily-cash-count", args=[submission.pk])
    client.force_login(owner)
    payload = {"amount": "235.00", "note": "Weekly pickup"}

    client.post(url, payload)
    client.post(url, payload)

    assert DailyCashCount.objects.count() == 1
    assert AuditEvent.objects.filter(action="cash.daily_counted").count() == 1
    assert not AuditEvent.objects.filter(action="cash.daily_corrected").exists()


def test_daily_cash_snapshot_rolls_back_when_audit_write_fails(
    client,
    owner,
    employee,
    monkeypatch,
):
    submission, reconciliation = make_daily(employee)
    client.force_login(owner)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("apps.reconcile.views.record_event", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(
            reverse("reconcile:daily-cash-count", args=[submission.pk]),
            {"amount": "235.00"},
        )

    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents is None
    assert reconciliation.collection_recorded_at is None
    assert not DailyCashCount.objects.exists()


def test_daily_cash_count_converts_dollars_to_exact_cents(client, owner, employee):
    submission, reconciliation = make_daily(employee)
    client.force_login(owner)

    client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": "235.01"},
    )

    count = DailyCashCount.objects.get()
    assert count.expected_cents == 23_500
    assert count.counted_cents == 23_501
    assert count.variance_cents == 1
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents == 23_501
    assert reconciliation.collection_variance_cents == 1


@pytest.mark.parametrize("bad_amount", ["-0.01", "1.999", "1000000000.00", "NaN", ""])
def test_daily_cash_count_rejects_invalid_or_unbounded_amounts(
    client,
    owner,
    employee,
    bad_amount,
):
    submission, reconciliation = make_daily(employee)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": bad_amount},
    )

    assert response.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents is None
    assert not DailyCashCount.objects.exists()
    assert not AuditEvent.objects.filter(
        action__in=["cash.daily_counted", "cash.daily_corrected"]
    ).exists()


def test_weekly_page_lists_daily_cash_and_totals_only_counted_days(
    client,
    owner,
    employee,
):
    first_submission, _first = make_daily(
        employee,
        day=dt.date(2026, 9, 21),
        counted_cash_cents=50_000,
    )
    second_submission, _second = make_daily(
        employee,
        day=dt.date(2026, 9, 22),
        counted_cash_cents=60_000,
    )
    make_daily(
        employee,
        day=dt.date(2026, 9, 23),
        counted_cash_cents=90_000,
    )
    client.force_login(owner)
    client.post(
        reverse("reconcile:daily-cash-count", args=[first_submission.pk]),
        {"amount": "234.00", "note": "Monday cash"},
    )
    client.post(
        reverse("reconcile:daily-cash-count", args=[second_submission.pk]),
        {"amount": "335.00", "note": "Tuesday cash"},
    )

    response = client.get(reverse("reconcile:daily-cash"))

    assert response.status_code == 200
    assert len(response.context["rows"]) == 3
    assert response.context["daily_cash_count"] == 3
    assert response.context["counted_count"] == 2
    assert response.context["expected_total"] == 120_500
    assert response.context["counted_total"] == 56_900
    # The uncounted Wednesday entry is visible, but it must not create a fake
    # shortage in the weekly variance before the owner physically counts it.
    assert response.context["counted_difference"] == -100
    assert response.context["issue_count"] == 1
    content = response.content.decode()
    assert "Monday, September 21, 2026" in content
    assert "Tuesday, September 22, 2026" in content
    assert "Wednesday, September 23, 2026" in content
    assert "Monday cash" in content
    assert "Tuesday cash" in content
    assert "Difference so far" in content
    assert "-$1.00" in content


def test_daily_cash_page_has_date_picker_equation_and_plain_entry_label(
    client,
    owner,
    employee,
):
    make_daily(
        employee,
        day=dt.date(2026, 9, 23),
        counted_cash_cents=50_000,
    )
    client.force_login(owner)

    response = client.get(reverse("reconcile:daily-cash"), {"date": "2026-09-23"})
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["week_start"] == dt.date(2026, 9, 21)
    assert 'id="cash-date"' in content
    assert 'value="2026-09-23"' in content
    assert "Wednesday, September 23, 2026" in content
    assert "All cash in register" in content
    assert "Leave in drawer" in content
    assert "Cash this pouch should have" in content
    assert "Cash you counted for this day" in content
    assert "Save this day's cash" in content


def test_empty_daily_cash_group_explains_how_to_make_days_appear(
    client,
    owner,
):
    client.force_login(owner)

    response = client.get(reverse("reconcile:daily-cash"), {"date": "2026-09-23"})
    content = response.content.decode()

    assert response.status_code == 200
    assert "No daily reports for Sep 21" in content
    assert "Sep 27, 2026" in content
    assert "after an employee sends that day's report and the owner approves it" in content
    assert "Review reports" in content
    assert "Add a daily close" in content


def test_future_daily_cash_date_is_clamped_to_store_week(client, owner):
    client.force_login(owner)

    response = client.get(reverse("reconcile:daily-cash"), {"date": "2099-01-01"})

    assert response.status_code == 200
    assert response.context["week_start"] <= response.context["today"]
    assert response.context["week_end"] >= response.context["today"]
    assert response.context["selected_date"] == response.context["today"]
    assert 'aria-disabled="true">Next 7 days' in response.content.decode()
