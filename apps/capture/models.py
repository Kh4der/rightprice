"""Capture records shared by the daily, payout and inventory workflows.

The original photographs are evidence, so they are immutable once uploaded.
Extraction results are stored beside (not instead of) the files and every
human correction is recorded by :mod:`apps.audit`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class SubmissionKind(models.TextChoices):
    DAILY_REPORT = "DAILY_REPORT", "Daily report"
    PAYOUT = "PAYOUT", "Lottery payout"
    INVENTORY = "INVENTORY", "Inventory delivery"


class SubmissionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    QUEUED = "QUEUED", "Queued"
    PROCESSING = "PROCESSING", "Reading photos"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
    READY = "READY", "Ready for owner"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    FAILED = "FAILED", "Processing failed"


class DocumentType(models.TextChoices):
    AUTO = "AUTO", "Detect automatically"
    SQUARE_SALES_REPORT = "SQUARE_SALES_REPORT", "Square sales report"
    SQUARE_DRAWER_SCREEN = "SQUARE_DRAWER_SCREEN", "Square drawer screen"
    LOTTERY_DAILY_SALES = "LOTTERY_DAILY_SALES", "Lottery daily sales"
    LOTTERY_TICKET_BALANCE = "LOTTERY_TICKET_BALANCE", "Lottery ticket balance"
    LOTTERY_DRAW_SCHEDULE = "LOTTERY_DRAW_SCHEDULE", "Lottery draw schedule"
    LOTTERY_PAYOUT = "LOTTERY_PAYOUT", "Lottery payout evidence"
    DELIVERY_INVOICE = "DELIVERY_INVOICE", "Delivery invoice"
    UNKNOWN = "UNKNOWN", "Unknown document"


class DocumentStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    PROCESSING = "PROCESSING", "Processing"
    EXTRACTED = "EXTRACTED", "Extracted"
    REVIEWED = "REVIEWED", "Human reviewed"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
    REJECTED = "REJECTED", "Retake required"
    FAILED = "FAILED", "Failed"


def current_business_day():
    """Resolve the store business day without creating an import cycle at startup."""
    from apps.squareapi.client import business_day_for

    return business_day_for(timezone.now())


def document_upload_path(instance: Document, filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:10]
    return f"submissions/{instance.submission_id}/{instance.id}{suffix}"


def pending_upload_expiry():
    """Give an unfinished browser upload a short, bounded lifetime."""

    return timezone.now() + timedelta(hours=2)


def pending_upload_path(instance: PendingUpload, filename: str) -> str:
    suffix = Path(filename).suffix.lower()[:10]
    return f"pending/{instance.uploaded_by_id}/{instance.id}{suffix}"


class Submission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=24, choices=SubmissionKind.choices)
    status = models.CharField(
        max_length=24,
        choices=SubmissionStatus.choices,
        default=SubmissionStatus.DRAFT,
        db_index=True,
    )
    business_day = models.DateField(default=current_business_day, db_index=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    employee_note = models.TextField(blank=True)
    owner_note = models.TextField(blank=True)
    processing_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_submissions",
    )

    class Meta:
        ordering = ["-business_day", "-created_at"]
        indexes = [
            models.Index(fields=["kind", "status", "business_day"]),
            models.Index(fields=["submitted_by", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} for {self.business_day}"

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            SubmissionStatus.APPROVED,
            SubmissionStatus.REJECTED,
        }

    @property
    def can_edit(self) -> bool:
        return not self.is_terminal


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    file = models.FileField(upload_to=document_upload_path, max_length=500)
    original_name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64, db_index=True)
    requested_type = models.CharField(
        max_length=40,
        choices=DocumentType.choices,
        default=DocumentType.AUTO,
    )
    detected_type = models.CharField(
        max_length=40,
        choices=DocumentType.choices,
        default=DocumentType.AUTO,
    )
    status = models.CharField(
        max_length=24,
        choices=DocumentStatus.choices,
        default=DocumentStatus.UPLOADED,
        db_index=True,
    )
    extracted_data = models.JSONField(default=dict, blank=True)
    check_results = models.JSONField(default=list, blank=True)
    preparation_steps = models.JSONField(default=list, blank=True)
    document_count = models.PositiveSmallIntegerField(default=1)
    processing_error = models.TextField(blank=True)
    provider = models.CharField(max_length=30, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["submission", "status"]),
            models.Index(fields=["detected_type"]),
        ]

    def __str__(self) -> str:
        return self.original_name

    def save(self, *args, **kwargs):
        if self.pk:
            original = (
                Document.objects.filter(pk=self.pk)
                .values(
                    "submission_id",
                    "file",
                    "original_name",
                    "media_type",
                    "size_bytes",
                    "sha256",
                    "requested_type",
                )
                .first()
            )
            if original:
                current = {
                    "submission_id": self.submission_id,
                    "file": self.file.name,
                    "original_name": self.original_name,
                    "media_type": self.media_type,
                    "size_bytes": self.size_bytes,
                    "sha256": self.sha256,
                    "requested_type": self.requested_type,
                }
                if current != original:
                    raise ValidationError(
                        "The original evidence file and its identity fields are immutable."
                    )
        return super().save(*args, **kwargs)

    @property
    def effective_type(self) -> str:
        if self.detected_type not in {DocumentType.AUTO, DocumentType.UNKNOWN}:
            return self.detected_type
        return self.requested_type


class PendingUpload(models.Model):
    """A private photo staged before the lightweight submission POST.

    Vercel rejects request bodies above 4.5 MB.  The browser therefore sends
    photos one at a time and the final form carries only these random IDs.  A
    staged photo remains owned by its uploader and can be consumed exactly
    once into an immutable :class:`Document`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_uploads",
    )
    submission_kind = models.CharField(max_length=24, choices=SubmissionKind.choices)
    field_name = models.CharField(max_length=40)
    file = models.FileField(upload_to=pending_upload_path, max_length=500)
    original_name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(validators=[MinValueValidator(1)])
    sha256 = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(default=pending_upload_expiry, db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_document = models.OneToOneField(
        Document,
        null=True,
        blank=True,
        # The staged record is part of the chain of custody.  Once linked, it
        # must not outlive its immutable evidence document in an inconsistent
        # state (and the database constraint below enforces the same invariant).
        on_delete=models.PROTECT,
        related_name="staged_upload",
    )

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["uploaded_by", "consumed_at", "expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(consumed_at__isnull=True, consumed_document__isnull=True)
                    | models.Q(consumed_at__isnull=False, consumed_document__isnull=False)
                ),
                name="capture_pending_consumption_consistent",
            )
        ]

    def __str__(self) -> str:
        return self.original_name

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()
