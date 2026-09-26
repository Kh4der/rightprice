import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from PIL import Image

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
from apps.reconcile.models import DailyReconciliation, PayoutRecord
from apps.squareapi.client import business_day_for


def image_upload(name="photo.jpg", *, content_type="image/jpeg"):
    output = io.BytesIO()
    Image.new("RGB", (700, 900), "white").save(output, format="JPEG")
    return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "OWNER",
        "owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner-capture",
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "EMP01",
        "1234",
        display_name="Employee One",
        square_team_member_id="TM-capture-one",
    )


@pytest.fixture
def other_employee(db):
    return User.objects.create_user(
        "EMP02",
        "5678",
        display_name="Employee Two",
        square_team_member_id="TM-capture-two",
    )


def make_submission(user, *, status=SubmissionStatus.READY, kind=SubmissionKind.DAILY_REPORT):
    return Submission.objects.create(
        kind=kind,
        status=status,
        business_day=business_day_for(timezone.now()),
        submitted_by=user,
    )


def add_document(submission, *, status=DocumentStatus.EXTRACTED, media_type="image/jpeg"):
    return Document.objects.create(
        submission=submission,
        file=image_upload("evidence.jpg"),
        original_name="evidence.jpg",
        media_type=media_type,
        size_bytes=100,
        sha256="a" * 64,
        requested_type=DocumentType.SQUARE_SALES_REPORT,
        status=status,
    )


@pytest.mark.parametrize(
    "url_name",
    ["capture:daily", "capture:payout", "capture:inventory"],
)
def test_capture_forms_render_the_camera_widget(client, employee, url_name):
    client.force_login(employee)

    response = client.get(reverse(url_name))

    assert response.status_code == 200
    assert b"Take photo or choose file" in response.content


def test_inventory_navigation_is_visible_to_owner_and_employee(client, owner, employee):
    inventory_url = reverse("capture:inventory").encode()

    for user in (owner, employee):
        client.force_login(user)
        response = client.get(reverse("capture:daily"))

        assert response.status_code == 200
        assert response.content.count(b'href="' + inventory_url + b'"') == 2
        assert b">Inventory</a>" in response.content


def test_submission_list_and_detail_are_scoped_to_employee(client, employee, other_employee):
    own = make_submission(employee)
    other = make_submission(other_employee)
    client.force_login(employee)

    listing = client.get(reverse("capture:list"))

    assert listing.status_code == 200
    assert list(listing.context["submissions"]) == [own]
    assert client.get(reverse("capture:detail", args=[own.pk])).status_code == 200
    assert client.get(reverse("capture:detail", args=[other.pk])).status_code == 404


def test_owner_can_see_every_submission(client, owner, employee, other_employee):
    first = make_submission(employee)
    second = make_submission(other_employee)
    client.force_login(owner)

    response = client.get(reverse("capture:list"))

    assert response.status_code == 200
    assert set(response.context["submissions"]) == {first, second}


def test_home_summary_is_scoped_to_the_signed_in_employee(client, employee, other_employee):
    own = make_submission(employee, status=SubmissionStatus.NEEDS_REVIEW)
    make_submission(other_employee, status=SubmissionStatus.NEEDS_REVIEW)
    client.force_login(employee)

    response = client.get(reverse("core:home"))

    assert response.status_code == 200
    assert list(response.context["recent"]) == [own]
    assert response.context["pending_count"] == 1
    assert response.context["payout_summary"] is None


@pytest.mark.parametrize(
    "url_name",
    [
        "core:home",
        "capture:list",
        "capture:daily",
        "capture:payout",
        "capture:inventory",
    ],
)
def test_employee_pages_require_login(client, url_name):
    response = client.get(reverse(url_name))

    assert response.status_code == 302
    assert reverse("accounts:login") in response.headers["Location"]


def test_document_download_is_private_and_has_safe_headers(
    client,
    owner,
    employee,
    other_employee,
):
    submission = make_submission(employee)
    document = add_document(submission, media_type="text/html")

    client.force_login(other_employee)
    assert client.get(reverse("capture:document-file", args=[document.pk])).status_code == 404

    client.force_login(owner)
    response = client.get(reverse("capture:document-file", args=[document.pk]))
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "private, no-store"
    response.close()


@pytest.mark.django_db(transaction=True)
def test_daily_upload_creates_evidence_and_queues_after_commit(client, monkeypatch):
    employee = User.objects.create_user(
        "EMP01",
        "1234",
        display_name="Employee One",
        square_team_member_id="TM-capture-upload",
    )
    queued = []
    monkeypatch.setattr(
        "apps.extraction.tasks.process_submission.delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    client.force_login(employee)

    response = client.post(
        reverse("capture:daily"),
        {
            "business_day": business_day_for(timezone.now()),
            "note": "Closing shift",
            "sales_report": image_upload("sales.jpg", content_type="text/html"),
            "drawer": image_upload("drawer.jpg"),
        },
    )

    assert response.status_code == 302
    submission = Submission.objects.get()
    assert submission.status == SubmissionStatus.QUEUED
    assert submission.employee_note == "Closing shift"
    assert submission.documents.count() == 2
    assert set(submission.documents.values_list("media_type", flat=True)) == {"image/jpeg"}
    assert DailyReconciliation.objects.filter(submission=submission).exists()
    assert queued == [((str(submission.pk),), {"force": False})]
    assert AuditEvent.objects.filter(action="submission.created", actor=employee).exists()


@pytest.mark.django_db(transaction=True)
def test_inventory_upload_can_route_to_claude_self_hosted_background_job(
    client, monkeypatch, settings
):
    settings.CLAUDE_INVENTORY_SANDBOX_ENABLED = True
    settings.CLAUDE_INVENTORY_SANDBOX_PRIMARY = True
    employee = User.objects.create_user("INV01", "1234", display_name="Inventory Clerk")
    claude_queued = []
    ordinary_queued = []
    monkeypatch.setattr(
        "apps.inventory.tasks.dispatch_claude_inventory_job.delay",
        lambda *args, **kwargs: claude_queued.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "apps.extraction.tasks.process_submission.delay",
        lambda *args, **kwargs: ordinary_queued.append((args, kwargs)),
    )
    client.force_login(employee)

    response = client.post(
        reverse("capture:inventory"),
        {
            "business_day": business_day_for(timezone.now()),
            "vendor_name": "Distributor",
            "note": "Truck delivery",
            "invoice_photos": [image_upload("invoice.jpg")],
        },
    )

    assert response.status_code == 302
    submission = Submission.objects.get()
    assert claude_queued == [
        ((str(submission.delivery.pk), str(employee.pk)), {}),
    ]
    assert ordinary_queued == []
    assert submission.status == SubmissionStatus.QUEUED


def test_invalid_upload_creates_no_partial_submission(client, employee):
    client.force_login(employee)
    invalid = SimpleUploadedFile("fake.jpg", b"not an image", content_type="image/jpeg")

    response = client.post(
        reverse("capture:daily"),
        {
            "business_day": business_day_for(timezone.now()),
            "note": "",
            "sales_report": invalid,
            "drawer": image_upload("drawer.jpg"),
        },
    )

    assert response.status_code == 200
    assert Submission.objects.count() == 0
    assert Document.objects.count() == 0


def test_employee_payout_amount_is_preserved_as_unconfirmed_input(
    client,
    employee,
    monkeypatch,
):
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: None)
    client.force_login(employee)

    response = client.post(
        reverse("capture:payout"),
        {
            "business_day": business_day_for(timezone.now()),
            "note": "Winner paid at register",
            "payout_photo": image_upload("ticket.jpg"),
            "amount": "50.25",
        },
    )

    assert response.status_code == 302
    payout = PayoutRecord.objects.select_related("submission").get()
    assert payout.amount_cents == 5_025
    assert payout.employee_reported_amount_cents == 5_025
    assert payout.extracted_amount_cents is None
    assert payout.amount_confirmed_at is None
    assert payout.amount_confirmed_by is None
    assert payout.submission.submitted_by == employee


@pytest.mark.parametrize("status", [SubmissionStatus.FAILED, SubmissionStatus.NEEDS_REVIEW])
def test_retry_forces_only_retryable_employee_submission(client, employee, monkeypatch, status):
    submission = make_submission(employee, status=status)
    calls = []
    monkeypatch.setattr(
        "apps.capture.views._queue",
        lambda item, **kwargs: calls.append((item, kwargs)),
    )
    client.force_login(employee)

    response = client.post(reverse("capture:retry", args=[submission.pk]))

    assert response.status_code == 302
    assert calls == [(submission, {"force": True})]
    assert AuditEvent.objects.filter(
        action="submission.retried",
        actor=employee,
        target_id=str(submission.pk),
    ).exists()


@pytest.mark.parametrize(
    "status",
    [
        SubmissionStatus.DRAFT,
        SubmissionStatus.QUEUED,
        SubmissionStatus.PROCESSING,
        SubmissionStatus.READY,
        SubmissionStatus.APPROVED,
        SubmissionStatus.REJECTED,
    ],
)
def test_retry_refuses_non_retryable_state(client, employee, monkeypatch, status):
    submission = make_submission(employee, status=status)
    calls = []
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: calls.append(args))
    client.force_login(employee)

    response = client.post(reverse("capture:retry", args=[submission.pk]))

    assert response.status_code == 302
    assert calls == []
    assert not AuditEvent.objects.filter(action="submission.retried").exists()


def test_retry_button_only_appears_for_retryable_state(client, employee):
    ready = make_submission(employee, status=SubmissionStatus.READY)
    failed = make_submission(employee, status=SubmissionStatus.FAILED)
    client.force_login(employee)

    ready_page = client.get(reverse("capture:detail", args=[ready.pk]))
    failed_page = client.get(reverse("capture:detail", args=[failed.pk]))

    assert b"Run automatic reading again" not in ready_page.content
    assert b"Run automatic reading again" in failed_page.content


def test_owner_can_retry_a_failed_employee_submission(client, owner, employee):
    submission = make_submission(employee, status=SubmissionStatus.FAILED)
    client.force_login(owner)

    response = client.get(reverse("capture:detail", args=[submission.pk]))

    assert response.status_code == 200
    assert b"Run automatic reading again" in response.content


def test_owner_can_accept_a_failed_check_with_an_audited_reason(client, owner, employee):
    submission = make_submission(
        employee,
        status=SubmissionStatus.NEEDS_REVIEW,
        kind=SubmissionKind.PAYOUT,
    )
    document = add_document(submission, status=DocumentStatus.NEEDS_REVIEW)
    document.detected_type = DocumentType.LOTTERY_PAYOUT
    document.check_results = [
        {
            "name": "evidence:amount_cents",
            "passed": False,
            "severity": "hard",
            "detail": "Required evidence is missing.",
        }
    ]
    document.save(update_fields=["detected_type", "check_results"])
    client.force_login(owner)

    response = client.post(
        reverse("capture:document-review", args=[document.pk]),
        {"reason": "Checked the original ticket and confirmed the corrected ledger amount."},
    )

    assert response.status_code == 302
    document.refresh_from_db()
    submission.refresh_from_db()
    assert document.status == DocumentStatus.REVIEWED
    assert document.check_results[0]["severity"] == "reviewed"
    assert document.check_results[0]["original_severity"] == "hard"
    assert submission.status == SubmissionStatus.READY
    assert AuditEvent.objects.filter(
        action="document.reviewed",
        actor=owner,
        target_id=str(document.pk),
    ).exists()


def test_employee_cannot_accept_an_ai_review_item(client, employee):
    submission = make_submission(employee, status=SubmissionStatus.NEEDS_REVIEW)
    document = add_document(submission, status=DocumentStatus.NEEDS_REVIEW)
    client.force_login(employee)

    response = client.post(
        reverse("capture:document-review", args=[document.pk]),
        {"reason": "I checked it myself."},
    )

    assert response.status_code == 403
    document.refresh_from_db()
    assert document.status == DocumentStatus.NEEDS_REVIEW


def test_retry_of_another_employees_submission_is_hidden_as_not_found(
    client,
    employee,
    other_employee,
):
    submission = make_submission(other_employee, status=SubmissionStatus.FAILED)
    client.force_login(employee)

    response = client.post(reverse("capture:retry", args=[submission.pk]))

    assert response.status_code == 404
