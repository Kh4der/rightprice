import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory

from apps.accounts.models import Role, User
from apps.audit.admin import AuditEventAdmin, FieldCorrectionAdmin
from apps.audit.models import AuditEvent, FieldCorrection
from apps.capture.models import Document, Submission, SubmissionKind


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        "OWNER",
        "owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner-audit-append",
    )


@pytest.fixture
def audit_event(owner):
    return AuditEvent.objects.create(
        actor=owner,
        action="submission.approved",
        target_type="capture.submission",
        target_id="record-1",
    )


@pytest.fixture
def field_correction(owner):
    submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        submitted_by=owner,
    )
    document = Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("evidence.jpg", b"image bytes", "image/jpeg"),
        original_name="evidence.jpg",
        media_type="image/jpeg",
        size_bytes=11,
        sha256="c" * 64,
    )
    return FieldCorrection.objects.create(
        document=document,
        field_path="counted_cash_cents",
        previous_value=100,
        corrected_value=200,
        reason="Digit checked against photo",
        corrected_by=owner,
    )


@pytest.mark.parametrize("manager_name", ["objects", "_base_manager", "_default_manager"])
def test_audit_event_cannot_be_bulk_updated_or_deleted(audit_event, manager_name):
    manager = getattr(AuditEvent, manager_name)

    with pytest.raises(ValidationError, match="append-only"):
        manager.filter(pk=audit_event.pk).update(action="submission.rejected")
    with pytest.raises(ValidationError, match="append-only"):
        manager.filter(pk=audit_event.pk).delete()


def test_audit_event_cannot_be_changed_or_deleted_through_instance(audit_event):
    audit_event.action = "submission.rejected"

    with pytest.raises(ValidationError, match="append-only"):
        audit_event.save()
    with pytest.raises(ValidationError, match="append-only"):
        audit_event.delete()


def test_field_correction_is_append_only_for_instance_and_queryset(field_correction):
    field_correction.reason = "Rewritten reason"

    with pytest.raises(ValidationError, match="append-only"):
        field_correction.save()
    with pytest.raises(ValidationError, match="append-only"):
        field_correction.delete()
    with pytest.raises(ValidationError, match="append-only"):
        FieldCorrection.objects.filter(pk=field_correction.pk).update(reason="Rewritten")
    with pytest.raises(ValidationError, match="append-only"):
        FieldCorrection.objects.filter(pk=field_correction.pk).delete()


@pytest.mark.parametrize(
    ("model", "admin_class"),
    [(AuditEvent, AuditEventAdmin), (FieldCorrection, FieldCorrectionAdmin)],
)
def test_audit_admin_exposes_no_add_change_or_delete_actions(owner, model, admin_class):
    request = RequestFactory().get("/admin/audit/")
    request.user = owner
    model_admin = admin_class(model, admin.site)

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
    assert "delete_selected" not in model_admin.get_actions(request)
