import io
import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.deletion import ProtectedError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.capture.models import Document, PendingUpload, Submission, SubmissionKind
from apps.capture.staging import cleanup_expired_uploads
from apps.squareapi.client import business_day_for


def image_upload(name="photo.jpg", *, padding=0, content_type="image/jpeg"):
    output = io.BytesIO()
    Image.new("RGB", (700, 900), "white").save(output, format="JPEG")
    return SimpleUploadedFile(
        name,
        output.getvalue() + (b"x" * padding),
        content_type=content_type,
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "STAGE01",
        "1234",
        display_name="Stage Employee",
        square_team_member_id="TM-stage-one",
    )


@pytest.fixture
def other_employee(db):
    return User.objects.create_user(
        "STAGE02",
        "5678",
        display_name="Other Employee",
        square_team_member_id="TM-stage-two",
    )


def stage(client, *, kind=SubmissionKind.DAILY_REPORT, field_name="sales_report", name="photo.jpg"):
    response = client.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": kind,
            "field_name": field_name,
            "photo": image_upload(name),
        },
    )
    assert response.status_code == 201, response.content
    return response.json()["stage_id"]


@override_settings(IS_VERCEL=True)
def test_vercel_capture_form_enables_sequential_staging(client, employee):
    client.force_login(employee)

    response = client.get(reverse("capture:inventory"))

    assert response.status_code == 200
    assert b'data-stage-upload-url="/capture/stage-photo/"' in response.content
    assert b'data-submission-kind="INVENTORY"' in response.content
    assert b'name="staged_uploads"' in response.content


@override_settings(IS_VERCEL=False)
def test_non_vercel_capture_form_keeps_multipart_fallback(client, employee):
    client.force_login(employee)

    response = client.get(reverse("capture:inventory"))

    assert response.status_code == 200
    assert b"data-stage-upload-url" not in response.content
    assert b'enctype="multipart/form-data"' in response.content


def staged_daily_payload(sales_id, drawer_id):
    return {
        "business_day": business_day_for(timezone.now()),
        "note": "Staged from phone",
        "staged_uploads": json.dumps(
            {
                "sales_report": [sales_id],
                "drawer": [drawer_id],
            }
        ),
    }


def test_stage_endpoint_requires_login_and_csrf(employee):
    anonymous = Client()
    response = anonymous.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": SubmissionKind.DAILY_REPORT,
            "field_name": "sales_report",
            "photo": image_upload(),
        },
    )
    assert response.status_code == 302

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(employee)
    response = csrf_client.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": SubmissionKind.DAILY_REPORT,
            "field_name": "sales_report",
            "photo": image_upload(),
        },
    )
    assert response.status_code == 403


def test_stage_endpoint_validates_slot_and_uses_decoded_media_type(client, employee):
    client.force_login(employee)
    response = client.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": SubmissionKind.DAILY_REPORT,
            "field_name": "not_a_slot",
            "photo": image_upload(content_type="text/html"),
        },
    )

    assert response.status_code == 400
    assert PendingUpload.objects.count() == 0

    response = client.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": SubmissionKind.DAILY_REPORT,
            "field_name": "sales_report",
            "photo": image_upload(content_type="text/html"),
        },
    )
    assert response.status_code == 201
    pending = PendingUpload.objects.get()
    assert pending.uploaded_by == employee
    assert pending.media_type == "image/jpeg"
    assert pending.file.name.startswith(f"pending/{employee.pk}/")
    assert response.headers["Cache-Control"] == "no-store"


def test_stage_endpoint_enforces_body_safe_file_limit(client, employee):
    client.force_login(employee)
    response = client.post(
        reverse("capture:stage-document"),
        {
            "submission_kind": SubmissionKind.DAILY_REPORT,
            "field_name": "sales_report",
            "photo": image_upload(padding=3_800_000),
        },
    )

    assert response.status_code == 400
    assert "too large" in response.json()["error"]
    assert PendingUpload.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_staged_daily_upload_is_consumed_once(client, employee, monkeypatch):
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: None)
    client.force_login(employee)
    sales_id = stage(client, field_name="sales_report", name="sales.jpg")
    drawer_id = stage(client, field_name="drawer", name="drawer.jpg")
    payload = staged_daily_payload(sales_id, drawer_id)

    response = client.post(reverse("capture:daily"), payload)

    assert response.status_code == 302
    submission = Submission.objects.get()
    documents = list(submission.documents.order_by("requested_type"))
    assert len(documents) == 2
    pending = list(PendingUpload.objects.order_by("field_name"))
    assert all(item.consumed_at is not None for item in pending)
    assert {item.consumed_document_id for item in pending} == {item.pk for item in documents}
    assert {item.file.name for item in pending} == {item.file.name for item in documents}

    with pytest.raises(ProtectedError):
        documents[0].delete()

    replay = client.post(reverse("capture:daily"), payload)
    assert replay.status_code == 200
    assert b"staged photo is unavailable" in replay.content
    assert Submission.objects.count() == 1
    assert Document.objects.count() == 2


def test_staged_token_cannot_cross_users(client, employee, other_employee, monkeypatch):
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: None)
    client.force_login(employee)
    sales_id = stage(client, field_name="sales_report")
    client.force_login(other_employee)

    response = client.post(
        reverse("capture:daily"),
        {
            "business_day": business_day_for(timezone.now()),
            "note": "",
            "staged_uploads": json.dumps({"sales_report": [sales_id]}),
            "drawer": image_upload("drawer.jpg"),
        },
    )

    assert response.status_code == 200
    assert b"staged photo is unavailable" in response.content
    assert Submission.objects.count() == 0
    pending = PendingUpload.objects.get()
    assert pending.consumed_at is None


def test_expired_staged_token_is_rejected(client, employee, monkeypatch):
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: None)
    client.force_login(employee)
    sales_id = stage(client, field_name="sales_report")
    drawer_id = stage(client, field_name="drawer")
    PendingUpload.objects.filter(pk=sales_id).update(expires_at=timezone.now() - timedelta(seconds=1))

    response = client.post(
        reverse("capture:daily"),
        staged_daily_payload(sales_id, drawer_id),
    )

    assert response.status_code == 200
    assert b"staged photo is unavailable" in response.content
    assert Submission.objects.count() == 0
    assert not PendingUpload.objects.filter(consumed_at__isnull=False).exists()


def test_cleanup_removes_only_expired_unconsumed_files(client, employee, monkeypatch):
    monkeypatch.setattr("apps.capture.views._queue", lambda *args, **kwargs: None)
    client.force_login(employee)
    orphan_id = stage(client, field_name="sales_report", name="orphan.jpg")
    orphan = PendingUpload.objects.get(pk=orphan_id)
    orphan.expires_at = timezone.now() - timedelta(minutes=1)
    orphan.save(update_fields=["expires_at"])
    storage = orphan.file.storage
    orphan_name = orphan.file.name

    deleted, failed = cleanup_expired_uploads()

    assert (deleted, failed) == (1, 0)
    assert not PendingUpload.objects.filter(pk=orphan_id).exists()
    assert not storage.exists(orphan_name)


def test_inventory_form_accepts_twelve_staged_ids_but_not_thirteen(client, employee):
    client.force_login(employee)
    common = {
        "business_day": business_day_for(timezone.now()),
        "note": "",
        "vendor_name": "Distributor",
    }
    fake_ids = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 14)]

    twelve = client.post(
        reverse("capture:inventory"),
        {**common, "staged_uploads": json.dumps({"invoice_photos": fake_ids[:12]})},
    )
    thirteen = client.post(
        reverse("capture:inventory"),
        {**common, "staged_uploads": json.dumps({"invoice_photos": fake_ids})},
    )

    # Twelve passes aggregate form validation and reaches secure token lookup;
    # thirteen is stopped before any model write.
    assert twelve.status_code == 200
    assert b"staged photo is unavailable" in twelve.content
    assert thirteen.status_code == 200
    assert b"no more than 12" in thirteen.content or b"Too many photos" in thirteen.content
    assert Submission.objects.count() == 0
