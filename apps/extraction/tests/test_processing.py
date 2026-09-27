from __future__ import annotations

import hashlib

import pytest
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile

from apps.accounts.models import Role, User
from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.extraction.processing import aggregate_submission_status, process_document
from apps.extraction.providers import FakeVisionProvider, ProviderConfigurationError
from apps.extraction.schemas import ClassifiedDocumentType, DocumentClassification, SquareDrawer
from apps.extraction.tasks import process_document_task
from apps.extraction.tasks import process_submission as process_submission_task

FIXTURES = settings.BASE_DIR / "tests" / "fixtures" / "documents"


def observed(value, text: str | None = None, location: str = "row"):
    return {
        "value": value,
        "verbatim": str(value) if text is None else text,
        "present": True,
        "legible": True,
        "location": location,
    }


def absent():
    return {"value": None, "verbatim": None, "present": False, "legible": False, "location": ""}


def classification(
    document_type=ClassifiedDocumentType.SQUARE_DRAWER_SCREEN,
    *,
    count=1,
    orientation=0,
    entire=True,
):
    return DocumentClassification.model_validate(
        {
            "document_type": observed(document_type, document_type.value, "heading"),
            "document_count": observed(count, str(count), "frame"),
            "orientation_degrees": orientation,
            "entire_document_visible": entire,
            "notes": "visible heading",
        }
    )


def drawer(*, expected=29_991):
    return SquareDrawer.model_validate(
        {
            "started_at": observed("9/26/26, 10:12 AM"),
            "started_by": observed("employee"),
            "drawer_state": observed("OPEN"),
            "starting_cash_cents": observed(26_500, "$265.00"),
            "paid_in_out_cents": observed(0, "$0.00"),
            "cash_sales_cents": observed(3_491, "$34.91"),
            "cash_refunds_cents": observed(0, "$0.00"),
            "expected_in_drawer_cents": observed(expected, f"${expected / 100:.2f}"),
            "counted_cash_cents": absent(),
            "over_short_cents": absent(),
        }
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(login_code="E7", pin="1234", display_name="Employee Seven")


@pytest.fixture
def demo_owner(db):
    return User.objects.create_user(
        login_code="AIDEMO",
        password="demo-password",
        display_name="AI Demo Owner",
        role=Role.OWNER,
        is_demo=True,
        is_staff=False,
    )


def make_document(
    employee,
    *,
    requested_type=DocumentType.AUTO,
    kind=SubmissionKind.DAILY_REPORT,
):
    submission = Submission.objects.create(kind=kind, submitted_by=employee)
    raw = (FIXTURES / "square_drawer_screen.jpg").read_bytes()
    document = Document(
        submission=submission,
        original_name="drawer.jpg",
        media_type="image/jpeg",
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        requested_type=requested_type,
    )
    document.file.save("drawer.jpg", ContentFile(raw), save=False)
    document.save()
    return submission, document


@pytest.mark.django_db
def test_demo_documents_fail_before_direct_or_celery_extraction(demo_owner):
    submission, document = make_document(demo_owner)
    provider = FakeVisionProvider(
        classifications=classification(),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer()},
    )

    with pytest.raises(PermissionDenied, match="never sends photos"):
        process_document(document.pk, provider=provider)
    with pytest.raises(PermissionDenied, match="never sends photos"):
        process_document_task.run(str(document.pk))
    with pytest.raises(PermissionDenied, match="never sends photos"):
        process_submission_task.run(str(submission.pk))

    document.refresh_from_db()
    submission.refresh_from_db()
    assert provider.calls == []
    assert document.status == DocumentStatus.UPLOADED
    assert submission.status == SubmissionStatus.DRAFT


@pytest.mark.django_db
def test_process_document_persists_evidence_checks_and_ready_status(employee):
    submission, document = make_document(employee)
    provider = FakeVisionProvider(
        classifications=classification(),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer()},
    )

    result = process_document(document.pk, provider=provider)
    submission.refresh_from_db()

    assert result.status == DocumentStatus.EXTRACTED
    assert result.detected_type == DocumentType.SQUARE_DRAWER_SCREEN
    assert result.provider == "fake"
    assert result.model_name == "fake-extractor-v1"
    assert result.document_count == 1
    assert result.processed_at is not None
    assert "flat_field_clahe" in result.preparation_steps
    assert result.extracted_data["schema_version"] == 1
    assert result.extracted_data["classification"]["document_type"]["value"] == (
        DocumentType.SQUARE_DRAWER_SCREEN
    )
    assert result.extracted_data["result"]["expected_in_drawer_cents"]["value"] == 29_991
    assert result.check_results[0]["passed"] is True
    assert submission.status == SubmissionStatus.READY
    assert [call.operation for call in provider.calls] == ["classify", "extract"]


@pytest.mark.django_db
def test_failed_arithmetic_routes_document_and_submission_to_review(employee):
    submission, document = make_document(employee)
    provider = FakeVisionProvider(
        classifications=classification(),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer(expected=99_999)},
    )

    result = process_document(document.pk, provider=provider)
    submission.refresh_from_db()

    assert result.status == DocumentStatus.NEEDS_REVIEW
    failed = [check for check in result.check_results if check["passed"] is False]
    assert failed[0]["name"].startswith("expected =")
    assert submission.status == SubmissionStatus.NEEDS_REVIEW


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("document_type", "count", "message"),
    [
        (ClassifiedDocumentType.SQUARE_DRAWER_SCREEN, 2, "one document only"),
        (ClassifiedDocumentType.UNKNOWN, 1, "could not be identified"),
        (ClassifiedDocumentType.LOTTERY_DRAW_SCHEDULE, 1, "draw schedule"),
    ],
)
def test_unsafe_classifications_are_rejected_without_extraction(
    employee, document_type, count, message
):
    submission, document = make_document(employee)
    provider = FakeVisionProvider(
        classifications=classification(document_type, count=count),
        extractions={},
    )

    result = process_document(document.pk, provider=provider)
    submission.refresh_from_db()

    assert result.status == DocumentStatus.REJECTED
    assert message.lower() in result.processing_error.lower()
    assert result.model_name == "fake-classifier-v1"
    assert [call.operation for call in provider.calls] == ["classify"]
    assert submission.status == SubmissionStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_explicit_type_mismatch_is_rejected(employee):
    _, document = make_document(employee, requested_type=DocumentType.SQUARE_SALES_REPORT)
    provider = FakeVisionProvider(classifications=classification(), extractions={})

    result = process_document(document.pk, provider=provider)

    assert result.status == DocumentStatus.REJECTED
    assert "labelled square_sales_report" in result.processing_error.lower()


@pytest.mark.django_db
def test_classifier_rotation_is_applied_before_document_specific_preprocessing(employee):
    _, document = make_document(employee)
    provider = FakeVisionProvider(
        classifications=classification(orientation=90),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer()},
    )

    result = process_document(document.pk, provider=provider)

    assert "rotate_clockwise_90" in result.preparation_steps


class ExplodingProvider(FakeVisionProvider):
    def extract(self, *args, **kwargs):
        raise RuntimeError("secret-provider-request-payload")


@pytest.mark.django_db
def test_provider_failure_is_stored_without_leaking_exception_text(employee):
    submission, document = make_document(employee)
    provider = ExplodingProvider(classifications=classification(), extractions={})

    with pytest.raises(RuntimeError, match="secret-provider"):
        process_document(document.pk, provider=provider)

    document.refresh_from_db()
    submission.refresh_from_db()
    assert document.status == DocumentStatus.FAILED
    assert "RuntimeError" in document.processing_error
    assert "secret-provider" not in document.processing_error
    assert submission.status == SubmissionStatus.FAILED


@pytest.mark.django_db
def test_provider_configuration_failure_cannot_leave_document_stuck_processing(
    employee, monkeypatch
):
    submission, document = make_document(employee)

    def unavailable():
        raise ProviderConfigurationError("do-not-store-this-secret")

    monkeypatch.setattr("apps.extraction.processing.get_provider", unavailable)

    with pytest.raises(ProviderConfigurationError):
        process_document(document.pk)

    document.refresh_from_db()
    submission.refresh_from_db()
    assert document.status == DocumentStatus.FAILED
    assert "do-not-store" not in document.processing_error
    assert submission.status == SubmissionStatus.FAILED


@pytest.mark.django_db
def test_completed_document_is_idempotent_without_force(employee):
    _, document = make_document(employee)
    first_provider = FakeVisionProvider(
        classifications=classification(),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer()},
    )
    process_document(document.pk, provider=first_provider)
    unused_provider = FakeVisionProvider(classifications=classification(), extractions={})

    result = process_document(document.pk, provider=unused_provider)

    assert result.status == DocumentStatus.EXTRACTED
    assert unused_provider.calls == []


@pytest.mark.django_db
def test_submission_aggregation_waits_for_all_uploaded_documents(employee):
    submission, first = make_document(employee)
    second = Document.objects.create(
        submission=submission,
        file=first.file.name,
        original_name="second.jpg",
        media_type="image/jpeg",
        size_bytes=first.size_bytes,
        sha256="b" * 64,
    )
    first.status = DocumentStatus.EXTRACTED
    first.save(update_fields=["status"])

    assert aggregate_submission_status(submission.pk) == SubmissionStatus.QUEUED

    second.status = DocumentStatus.NEEDS_REVIEW
    second.save(update_fields=["status"])
    assert aggregate_submission_status(submission.pk) == SubmissionStatus.NEEDS_REVIEW
