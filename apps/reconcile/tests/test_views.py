import datetime as dt
from types import SimpleNamespace

import pytest
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
from apps.inventory.models import Delivery
from apps.reconcile.models import (
    DailyCashCount,
    DailyReconciliation,
    PayoutRecord,
    PayoutStatus,
    ReconciliationStatus,
)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "OWNER",
        "owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner-reconcile",
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "EMP01",
        "1234",
        display_name="Employee One",
        square_team_member_id="TM-reconcile-employee",
    )


def make_submission(
    employee,
    *,
    kind=SubmissionKind.PAYOUT,
    status=SubmissionStatus.READY,
):
    return Submission.objects.create(
        kind=kind,
        status=status,
        business_day=dt.date(2026, 9, 26),
        submitted_by=employee,
    )


def add_extracted_document(submission, *, sha256="b" * 64):
    return Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("evidence.jpg", b"image bytes", "image/jpeg"),
        original_name="evidence.jpg",
        media_type="image/jpeg",
        size_bytes=11,
        sha256=sha256,
        requested_type=DocumentType.LOTTERY_PAYOUT,
        status=DocumentStatus.EXTRACTED,
    )


def make_payout(
    employee,
    *,
    status=SubmissionStatus.READY,
    amount_cents=5_000,
    confirmed=None,
    sha256="b" * 64,
    ticket_reference="",
    validation_reference="",
):
    submission = make_submission(employee, status=status)
    add_extracted_document(submission, sha256=sha256)
    if confirmed is None:
        confirmed = amount_cents is not None
    payout = PayoutRecord.objects.create(
        submission=submission,
        amount_cents=amount_cents,
        employee_reported_amount_cents=amount_cents,
        extracted_amount_cents=amount_cents,
        amount_confirmed_at=timezone.now() if confirmed else None,
        ticket_reference=ticket_reference,
        validation_reference=validation_reference,
    )
    return submission, payout


def test_employee_cannot_reach_owner_dashboard_or_mutations(client, employee):
    submission, payout = make_payout(employee)
    client.force_login(employee)

    assert client.get(reverse("reconcile:owner-dashboard")).status_code == 403
    assert (
        client.post(
            reverse("reconcile:decision", args=[submission.pk]),
            {"decision": "reject", "owner_note": "No"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            reverse("reconcile:payout-amount", args=[payout.pk]),
            {"amount": "10.00"},
        ).status_code
        == 403
    )
    assert client.post(reverse("reconcile:reimburse-payout", args=[payout.pk])).status_code == 403


def test_payout_approval_records_final_decision(client, owner, employee):
    submission, payout = make_payout(employee)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve", "owner_note": "Ticket matches"},
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    payout.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert submission.approved_by == owner
    assert submission.approved_at is not None
    assert submission.owner_note == "Ticket matches"
    assert payout.status == PayoutStatus.PENDING
    assert AuditEvent.objects.filter(
        action="submission.approved",
        actor=owner,
        target_id=str(submission.pk),
    ).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("validation_reference", "VALID-7788"),
        ("ticket_reference", "TICKET-7788"),
    ],
)
def test_payout_approval_blocks_duplicate_reference(
    client,
    owner,
    employee,
    field,
    value,
):
    make_payout(
        employee,
        status=SubmissionStatus.APPROVED,
        sha256="1" * 64,
        **{field: value},
    )
    submission, payout = make_payout(
        employee,
        sha256="2" * 64,
        **{field: value.lower()},
    )
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
        follow=True,
    )

    submission.refresh_from_db()
    assert response.status_code == 200
    assert submission.status == SubmissionStatus.READY
    assert b"Duplicate payout" in response.content
    event = AuditEvent.objects.get(action="payout.duplicate_approval_blocked")
    assert event.target_id == str(payout.pk)
    assert event.detail["match_type"] == field
    assert not AuditEvent.objects.filter(action="submission.approved").exists()


def test_payout_approval_blocks_duplicate_source_photo(client, owner, employee):
    duplicate_sha = "3" * 64
    make_payout(
        employee,
        status=SubmissionStatus.APPROVED,
        sha256=duplicate_sha,
    )
    submission, payout = make_payout(employee, sha256=duplicate_sha)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
        follow=True,
    )

    submission.refresh_from_db()
    assert response.status_code == 200
    assert submission.status == SubmissionStatus.READY
    assert b"same evidence photo" in response.content
    event = AuditEvent.objects.get(action="payout.duplicate_approval_blocked")
    assert event.target_id == str(payout.pk)
    assert event.detail["match_type"] == "source_photo"


def test_rejected_payout_duplicate_does_not_block_approval(client, owner, employee):
    make_payout(
        employee,
        status=SubmissionStatus.REJECTED,
        sha256="4" * 64,
        validation_reference="VALID-9000",
    )
    submission, _payout = make_payout(
        employee,
        sha256="5" * 64,
        validation_reference="VALID-9000",
    )
    client.force_login(owner)

    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    submission.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert not AuditEvent.objects.filter(action="payout.duplicate_approval_blocked").exists()


def test_final_decision_cannot_be_replaced_by_a_second_request(client, owner, employee):
    submission, payout = make_payout(employee)
    client.force_login(owner)
    url = reverse("reconcile:decision", args=[submission.pk])
    client.post(url, {"decision": "approve", "owner_note": "Approved once"})

    response = client.post(
        url,
        {"decision": "reject", "owner_note": "Changed mind"},
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    payout.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert submission.owner_note == "Approved once"
    assert payout.status == PayoutStatus.PENDING
    assert AuditEvent.objects.filter(action="submission.approved").count() == 1
    assert not AuditEvent.objects.filter(action="submission.rejected").exists()


def test_approval_requires_all_documents_to_be_extracted(client, owner, employee):
    submission, _payout = make_payout(employee)
    submission.documents.update(status=DocumentStatus.NEEDS_REVIEW)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    assert submission.status == SubmissionStatus.READY
    assert not AuditEvent.objects.filter(action="submission.approved").exists()


def test_reject_requires_a_reason(client, owner, employee):
    submission, payout = make_payout(employee)
    client.force_login(owner)

    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "reject", "owner_note": "   "},
    )

    submission.refresh_from_db()
    payout.refresh_from_db()
    assert submission.status == SubmissionStatus.READY
    assert payout.status == PayoutStatus.PENDING


def test_rejecting_payout_voids_pending_reimbursement_in_same_workflow(
    client,
    owner,
    employee,
):
    submission, payout = make_payout(employee)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "reject", "owner_note": "Unreadable ticket number"},
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    payout.refresh_from_db()
    assert submission.status == SubmissionStatus.REJECTED
    assert payout.status == PayoutStatus.VOID
    assert AuditEvent.objects.filter(
        action="payout.voided",
        actor=owner,
        target_id=str(payout.pk),
    ).exists()


def test_confirm_payout_amount_is_available_before_approval(client, owner, employee):
    submission, payout = make_payout(employee, amount_cents=None, confirmed=False)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:payout-amount", args=[payout.pk]),
        {"amount": "123.45"},
    )

    assert response.status_code == 302
    payout.refresh_from_db()
    submission.refresh_from_db()
    assert payout.amount_cents == 12_345
    assert payout.amount_confirmed_at is not None
    assert payout.amount_confirmed_by == owner
    assert submission.status == SubmissionStatus.READY
    event = AuditEvent.objects.get(action="payout.amount_confirmed")
    assert event.actor == owner
    assert event.detail == {"previous_cents": None, "amount_cents": 12_345}


def test_payout_amount_change_rolls_back_if_audit_write_fails(
    client,
    owner,
    employee,
    monkeypatch,
):
    _submission, payout = make_payout(employee, amount_cents=1_000, confirmed=False)
    client.force_login(owner)
    monkeypatch.setattr(
        "apps.reconcile.views.record_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(
            reverse("reconcile:payout-amount", args=[payout.pk]),
            {"amount": "99.00"},
        )

    payout.refresh_from_db()
    assert payout.amount_cents == 1_000
    assert payout.amount_confirmed_at is None
    assert payout.amount_confirmed_by is None


@pytest.mark.parametrize("bad_amount", ["-1.00", "1.999", "1000000000.00", "NaN", ""])
def test_confirm_payout_amount_rejects_unsafe_money_values(
    client,
    owner,
    employee,
    bad_amount,
):
    _submission, payout = make_payout(employee, amount_cents=1_000, confirmed=False)
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:payout-amount", args=[payout.pk]),
        {"amount": bad_amount},
    )

    assert response.status_code == 302
    payout.refresh_from_db()
    assert payout.amount_cents == 1_000
    assert not AuditEvent.objects.filter(action="payout.amount_confirmed").exists()


def test_disagreeing_payout_amounts_require_owner_confirmation_before_approval(
    client,
    owner,
    employee,
):
    submission, payout = make_payout(employee, amount_cents=None, confirmed=False)
    payout.employee_reported_amount_cents = 10_000
    payout.extracted_amount_cents = 25_000
    payout.save(
        update_fields=[
            "employee_reported_amount_cents",
            "extracted_amount_cents",
            "updated_at",
        ]
    )
    client.force_login(owner)

    detail = client.get(reverse("capture:detail", args=[submission.pk]))
    blocked = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    assert detail.status_code == 200
    assert b"Employee entered" in detail.content
    assert b"Photo reading" in detail.content
    assert b"disagree" in detail.content
    assert blocked.status_code == 302
    submission.refresh_from_db()
    assert submission.status == SubmissionStatus.READY

    client.post(
        reverse("reconcile:payout-amount", args=[payout.pk]),
        {"amount": "250.00"},
    )
    approved = client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve", "owner_note": "Confirmed against ticket"},
    )

    assert approved.status_code == 302
    payout.refresh_from_db()
    submission.refresh_from_db()
    assert payout.amount_cents == 25_000
    assert payout.amount_confirmed_by == owner
    assert payout.amount_confirmed_at is not None
    assert submission.status == SubmissionStatus.APPROVED


def test_final_payout_amount_cannot_be_changed(client, owner, employee):
    _submission, payout = make_payout(
        employee,
        status=SubmissionStatus.APPROVED,
        amount_cents=1_000,
    )
    client.force_login(owner)

    client.post(
        reverse("reconcile:payout-amount", args=[payout.pk]),
        {"amount": "99.00"},
    )

    payout.refresh_from_db()
    assert payout.amount_cents == 1_000


def test_payout_cannot_be_reimbursed_before_evidence_is_approved(client, owner, employee):
    submission, payout = make_payout(employee, status=SubmissionStatus.READY)
    client.force_login(owner)

    response = client.post(reverse("reconcile:reimburse-payout", args=[payout.pk]))

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("capture:detail", args=[submission.pk])
    payout.refresh_from_db()
    assert payout.status == PayoutStatus.PENDING
    assert payout.reimbursed_by is None


def test_approved_payout_can_be_reimbursed_once(client, owner, employee):
    _submission, payout = make_payout(employee, status=SubmissionStatus.APPROVED)
    client.force_login(owner)
    url = reverse("reconcile:reimburse-payout", args=[payout.pk])

    first = client.post(url, {"note": "Paid from office safe"})
    second = client.post(url)

    assert first.status_code == 302
    assert second.status_code == 302
    payout.refresh_from_db()
    assert payout.status == PayoutStatus.REIMBURSED
    assert payout.reimbursed_by == owner
    assert payout.reimbursed_at is not None
    assert payout.reimbursement_note == "Paid from office safe"
    assert AuditEvent.objects.filter(action="payout.reimbursed").count() == 1


def test_reimbursement_rechecks_duplicate_evidence(client, owner, employee):
    _submission, payout = make_payout(
        employee,
        status=SubmissionStatus.APPROVED,
        sha256="6" * 64,
        ticket_reference="DUPLICATE-44",
    )
    make_payout(
        employee,
        status=SubmissionStatus.READY,
        sha256="7" * 64,
        ticket_reference="DUPLICATE-44",
    )
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:reimburse-payout", args=[payout.pk]),
        follow=True,
    )

    payout.refresh_from_db()
    assert response.status_code == 200
    assert payout.status == PayoutStatus.PENDING
    assert b"Duplicate payout" in response.content
    assert AuditEvent.objects.filter(
        action="payout.duplicate_reimbursement_blocked",
        target_id=str(payout.pk),
    ).exists()
    assert not AuditEvent.objects.filter(action="payout.reimbursed").exists()


def test_owner_dashboard_and_home_count_only_approved_pending_payouts(
    client,
    owner,
    employee,
):
    _ready_submission, ready_payout = make_payout(
        employee,
        status=SubmissionStatus.READY,
        amount_cents=1_000,
    )
    _approved_submission, approved_payout = make_payout(
        employee,
        status=SubmissionStatus.APPROVED,
        amount_cents=2_000,
    )
    client.force_login(owner)

    dashboard = client.get(reverse("reconcile:owner-dashboard"))
    home = client.get(reverse("core:home"))

    assert list(dashboard.context["pending_payouts"]) == [approved_payout]
    assert dashboard.context["payout_totals"] == {"count": 1, "known_amount": 2_000}
    assert home.context["payout_summary"] == {"count": 1, "amount": 2_000}
    assert ready_payout.status == PayoutStatus.PENDING


def test_delivery_links_reverse_on_submission_detail_and_owner_dashboard(
    client,
    owner,
    employee,
):
    submission = make_submission(
        employee,
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.NEEDS_REVIEW,
    )
    delivery = Delivery.objects.create(submission=submission, invoice_number="INV-100")
    expected_url = reverse("inventory:delivery-detail", args=[delivery.pk])
    client.force_login(owner)

    detail = client.get(reverse("capture:detail", args=[submission.pk]))
    dashboard = client.get(reverse("reconcile:owner-dashboard"))

    assert detail.status_code == 200
    assert dashboard.status_code == 200
    assert expected_url.encode() in detail.content
    assert expected_url.encode() in dashboard.content


def make_daily_reconciliation(employee, *, submission_status=SubmissionStatus.READY):
    submission = make_submission(
        employee,
        kind=SubmissionKind.DAILY_REPORT,
        status=submission_status,
    )
    add_extracted_document(submission)
    reconciliation = DailyReconciliation.objects.create(
        submission=submission,
        counted_cash_cents=50_000,
    )
    return submission, reconciliation


def test_square_sync_is_owner_only_and_post_only(client, owner, employee):
    submission, _reconciliation = make_daily_reconciliation(employee)
    url = reverse("reconcile:sync-square-day", args=[submission.pk])

    client.force_login(employee)
    assert client.post(url).status_code == 403

    client.force_login(owner)
    assert client.get(url).status_code == 405


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.DRAFT,
        SubmissionStatus.QUEUED,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.FAILED,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ],
)
def test_square_sync_refuses_unsafe_submission_states(
    client,
    owner,
    employee,
    monkeypatch,
    status,
):
    submission, _reconciliation = make_daily_reconciliation(
        employee,
        submission_status=status,
    )
    calls = []
    monkeypatch.setattr(
        "apps.reconcile.views.sync_daily_reconciliation",
        lambda *args, **kwargs: calls.append(args),
    )
    client.force_login(owner)

    response = client.post(reverse("reconcile:sync-square-day", args=[submission.pk]))

    assert response.status_code == 302
    assert calls == []
    assert not AuditEvent.objects.filter(action="reconciliation.square_synced").exists()


@pytest.mark.parametrize("status", [SubmissionStatus.READY, SubmissionStatus.NEEDS_REVIEW])
def test_square_sync_runs_in_reviewable_states_and_is_audited(
    client,
    owner,
    employee,
    monkeypatch,
    status,
):
    submission, reconciliation = make_daily_reconciliation(
        employee,
        submission_status=status,
    )
    result = SimpleNamespace(
        status=ReconciliationStatus.MATCHED,
        problems=(),
        comparisons=({"name": "square.payments.cash", "passed": True},),
    )
    calls = []

    def fake_sync(target, *, on_persist=None):
        calls.append(target.pk)
        if on_persist is not None:
            on_persist(target, result)
        return result

    monkeypatch.setattr("apps.reconcile.views.sync_daily_reconciliation", fake_sync)
    client.force_login(owner)

    response = client.post(reverse("reconcile:sync-square-day", args=[submission.pk]))

    assert response.status_code == 302
    assert calls == [reconciliation.pk]
    event = AuditEvent.objects.get(action="reconciliation.square_synced")
    assert event.actor == owner
    assert event.target_id == str(reconciliation.pk)
    assert event.detail == {
        "status": ReconciliationStatus.MATCHED,
        "problems": [],
        "comparison_count": 1,
    }


def test_daily_cash_count_uses_exact_cents_and_effective_drawer_float(
    client,
    owner,
    employee,
):
    submission, reconciliation = make_daily_reconciliation(
        employee,
        submission_status=SubmissionStatus.APPROVED,
    )
    expected = 50_000 - DEFAULT_FLOAT_CENTS
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": f"{expected / 100:.2f}", "note": "Counted together"},
    )

    assert response.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.drawer_float_cents == DEFAULT_FLOAT_CENTS
    assert reconciliation.expected_collection_cents == expected
    assert reconciliation.owner_collected_cents == expected
    assert reconciliation.collection_variance_cents == 0
    assert reconciliation.collection_note == "Counted together"
    assert reconciliation.collection_evidence_hash == (
        reconciliation.current_collection_evidence_hash()
    )
    assert reconciliation.collection_recorded_by == owner
    assert DailyCashCount.objects.filter(reconciliation=reconciliation).count() == 1
    assert AuditEvent.objects.filter(action="cash.daily_counted", actor=owner).exists()


def test_daily_cash_count_cannot_change_a_rejected_submission(client, owner, employee):
    submission, reconciliation = make_daily_reconciliation(
        employee,
        submission_status=SubmissionStatus.REJECTED,
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
    assert not AuditEvent.objects.filter(action="cash.daily_counted").exists()


def test_daily_cash_difference_is_saved_then_corrected(client, owner, employee):
    submission, reconciliation = make_daily_reconciliation(
        employee,
        submission_status=SubmissionStatus.APPROVED,
    )
    expected = 50_000 - DEFAULT_FLOAT_CENTS
    client.force_login(owner)
    url = reverse("reconcile:daily-cash-count", args=[submission.pk])

    without_note = client.post(url, {"amount": f"{(expected + 1) / 100:.2f}"})

    assert without_note.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents == expected + 1
    assert reconciliation.collection_variance_cents == 1
    assert AuditEvent.objects.filter(action="cash.daily_counted").exists()

    corrected = client.post(
        url,
        {
            "amount": f"{expected / 100:.2f}",
            "note": "Recounted and corrected",
        },
    )

    assert corrected.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents == expected
    assert reconciliation.collection_variance_cents == 0
    assert reconciliation.collection_note == "Recounted and corrected"
    assert DailyCashCount.objects.filter(reconciliation=reconciliation).count() == 2
    assert AuditEvent.objects.filter(action="cash.daily_corrected").exists()


@pytest.mark.parametrize("bad_amount", ["-0.01", "1.999", "1000000000.00", "NaN", ""])
def test_daily_cash_rejects_invalid_or_unbounded_amounts(
    client,
    owner,
    employee,
    bad_amount,
):
    submission, reconciliation = make_daily_reconciliation(
        employee,
        submission_status=SubmissionStatus.APPROVED,
    )
    client.force_login(owner)

    response = client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": bad_amount},
    )

    assert response.status_code == 302
    reconciliation.refresh_from_db()
    assert reconciliation.owner_collected_cents is None
    assert not DailyCashCount.objects.exists()
    assert not AuditEvent.objects.filter(action="cash.daily_counted").exists()


def test_daily_approval_does_not_require_weekly_cash_count(client, owner, employee):
    submission, reconciliation = make_daily_reconciliation(employee)
    reconciliation.status = ReconciliationStatus.MATCHED
    reconciliation.save(update_fields=["status", "updated_at"])
    client.force_login(owner)

    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    submission.refresh_from_db()
    reconciliation.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert reconciliation.status == ReconciliationStatus.APPROVED
    assert reconciliation.owner_collected_cents is None
    assert reconciliation.expected_collection_cents == 50_000 - DEFAULT_FLOAT_CENTS


def test_approved_daily_submission_marks_reconciliation_approved(client, owner, employee):
    submission, reconciliation = make_daily_reconciliation(employee)
    client.force_login(owner)
    reconciliation.status = ReconciliationStatus.MATCHED
    reconciliation.save(update_fields=["status", "updated_at"])

    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    submission.refresh_from_db()
    reconciliation.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert reconciliation.status == ReconciliationStatus.APPROVED


def test_daily_approval_materializes_expected_cash_from_latest_drawer_count(
    client,
    owner,
    employee,
):
    submission, reconciliation = make_daily_reconciliation(employee)
    client.force_login(owner)
    reconciliation.counted_cash_cents += 100
    reconciliation.status = ReconciliationStatus.MATCHED
    reconciliation.save(update_fields=["counted_cash_cents", "status", "updated_at"])

    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )

    submission.refresh_from_db()
    reconciliation.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert reconciliation.status == ReconciliationStatus.APPROVED
    assert reconciliation.drawer_float_cents == DEFAULT_FLOAT_CENTS
    assert reconciliation.expected_collection_cents == 50_100 - DEFAULT_FLOAT_CENTS
    assert AuditEvent.objects.filter(action="submission.approved").exists()


def test_daily_cash_issue_does_not_reopen_approved_daily_evidence(
    client,
    owner,
    employee,
):
    submission, reconciliation = make_daily_reconciliation(employee)
    expected = 50_000 - DEFAULT_FLOAT_CENTS
    client.force_login(owner)
    reconciliation.status = ReconciliationStatus.MATCHED
    reconciliation.save(update_fields=["status", "updated_at"])
    client.post(
        reverse("reconcile:decision", args=[submission.pk]),
        {"decision": "approve"},
    )
    client.post(
        reverse("reconcile:daily-cash-count", args=[submission.pk]),
        {"amount": f"{(expected - 100) / 100:.2f}"},
    )

    submission.refresh_from_db()
    reconciliation.refresh_from_db()
    assert submission.status == SubmissionStatus.APPROVED
    assert reconciliation.status == ReconciliationStatus.APPROVED
    assert reconciliation.collection_variance_cents == -100
    assert AuditEvent.objects.filter(action="cash.daily_counted").exists()


@pytest.mark.parametrize(
    "url_name",
    ["reconcile:decision", "reconcile:reimburse-payout", "reconcile:payout-amount"],
)
def test_owner_mutations_are_post_only(client, owner, employee, url_name):
    submission, payout = make_payout(employee)
    client.force_login(owner)
    identifier = submission.pk if url_name == "reconcile:decision" else payout.pk

    assert client.get(reverse(url_name, args=[identifier])).status_code == 405
