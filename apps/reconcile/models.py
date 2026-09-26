"""Stored results for daily reconciliation and lottery payout tracking."""

from __future__ import annotations

import hashlib
import json
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def cash_collection_evidence_hash(
    *,
    submission_id: object,
    business_day: object,
    counted_cash_cents: int | None,
    paper_expected_cash_cents: int | None,
) -> str:
    """Bind an owner's physical collection to the paper values they reviewed."""

    payload = {
        "schema_version": 1,
        "submission_id": str(submission_id),
        "business_day": str(business_day),
        "counted_cash_cents": counted_cash_cents,
        "paper_expected_cash_cents": paper_expected_cash_cents,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ReconciliationStatus(models.TextChoices):
    INCOMPLETE = "INCOMPLETE", "Missing evidence"
    PROVISIONAL = "PROVISIONAL", "Waiting for Square"
    MISMATCH = "MISMATCH", "Needs review"
    MATCHED = "MATCHED", "Matched"
    APPROVED = "APPROVED", "Owner approved"


class DailyReconciliation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        "capture.Submission",
        on_delete=models.CASCADE,
        related_name="daily_reconciliation",
    )
    status = models.CharField(
        max_length=20,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.INCOMPLETE,
        db_index=True,
    )
    paper_values = models.JSONField(default=dict, blank=True)
    square_values = models.JSONField(default=dict, blank=True)
    check_results = models.JSONField(default=list, blank=True)
    missing_evidence = models.JSONField(default=list, blank=True)
    counted_cash_cents = models.BigIntegerField(null=True, blank=True)
    paper_expected_cash_cents = models.BigIntegerField(null=True, blank=True)
    square_expected_cash_cents = models.BigIntegerField(null=True, blank=True)
    lottery_sales_cents = models.BigIntegerField(null=True, blank=True)
    lottery_payouts_cents = models.BigIntegerField(null=True, blank=True)
    explained_cash_cents = models.BigIntegerField(null=True, blank=True)
    unexplained_variance_cents = models.BigIntegerField(null=True, blank=True)
    drawer_float_cents = models.BigIntegerField(null=True, blank=True)
    expected_collection_cents = models.BigIntegerField(null=True, blank=True)
    owner_collected_cents = models.BigIntegerField(null=True, blank=True)
    collection_variance_cents = models.BigIntegerField(null=True, blank=True)
    collection_note = models.CharField(max_length=300, blank=True)
    collection_evidence_hash = models.CharField(max_length=64, blank=True)
    collection_recorded_at = models.DateTimeField(null=True, blank=True)
    collection_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cash_collections",
    )
    square_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Reconciliation {self.submission.business_day}"

    def current_collection_evidence_hash(self) -> str:
        return cash_collection_evidence_hash(
            submission_id=self.submission_id,
            business_day=self.submission.business_day,
            counted_cash_cents=self.counted_cash_cents,
            paper_expected_cash_cents=self.paper_expected_cash_cents,
        )


class DailyCashCountQuerySet(models.QuerySet):
    """Cash-count revisions are financial evidence and therefore append-only."""

    def update(self, **kwargs):
        raise ValidationError("Daily cash count revisions cannot be changed.")

    def delete(self):
        raise ValidationError("Daily cash count revisions cannot be deleted.")


class DailyCashCount(models.Model):
    """One physical count of the cash removed for a business day.

    ``DailyReconciliation`` keeps the latest values for fast queues and totals;
    this table keeps every earlier entry when an owner recounts or fixes a
    typo.  A correction is a new row, never an overwrite.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reconciliation = models.ForeignKey(
        DailyReconciliation,
        on_delete=models.PROTECT,
        related_name="daily_cash_counts",
    )
    correction_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrections",
    )
    drawer_float_cents = models.BigIntegerField()
    expected_cents = models.BigIntegerField()
    counted_cents = models.BigIntegerField()
    variance_cents = models.BigIntegerField()
    evidence_hash = models.CharField(max_length=64)
    note = models.CharField(max_length=300, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="daily_cash_counts",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = DailyCashCountQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(drawer_float_cents__gte=0),
                name="daily_cash_float_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(counted_cents__gte=0),
                name="daily_cash_count_nonnegative",
            ),
        ]
        indexes = [models.Index(fields=["reconciliation", "-created_at"])]

    def __str__(self) -> str:
        return f"Daily cash {self.reconciliation.submission.business_day}: {self.counted_cents}"

    def save(self, *args, **kwargs):
        if self.pk and DailyCashCount.objects.filter(pk=self.pk).exists():
            raise ValidationError("Daily cash count revisions cannot be changed.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Daily cash count revisions cannot be deleted.")

    def clean(self):
        super().clean()
        if self.variance_cents != self.counted_cents - self.expected_cents:
            raise ValidationError(
                {"variance_cents": "Variance must equal counted cash minus expected cash."}
            )
        if self.correction_of_id:
            previous_reconciliation_id = self.correction_of.reconciliation_id
            if previous_reconciliation_id != self.reconciliation_id:
                raise ValidationError(
                    {"correction_of": "A correction must belong to the same business day."}
                )


class PayoutStatus(models.TextChoices):
    PENDING = "PENDING", "Awaiting owner reimbursement"
    REIMBURSED = "REIMBURSED", "Reimbursed"
    VOID = "VOID", "Void"


class PayoutRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        "capture.Submission",
        on_delete=models.PROTECT,
        related_name="payout_record",
    )
    amount_cents = models.BigIntegerField(null=True, blank=True)
    employee_reported_amount_cents = models.BigIntegerField(null=True, blank=True)
    extracted_amount_cents = models.BigIntegerField(null=True, blank=True)
    amount_confirmed_at = models.DateTimeField(null=True, blank=True)
    amount_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_payout_amounts",
    )
    game_name = models.CharField(max_length=120, blank=True)
    ticket_reference = models.CharField(max_length=120, blank=True, db_index=True)
    validation_reference = models.CharField(max_length=120, blank=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=PayoutStatus.choices,
        default=PayoutStatus.PENDING,
        db_index=True,
    )
    square_event_id = models.CharField(max_length=64, blank=True)
    reimbursement_note = models.CharField(max_length=300, blank=True)
    reimbursed_at = models.DateTimeField(null=True, blank=True)
    reimbursed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reimbursed_payouts",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-created_at"]

    def __str__(self) -> str:
        return f"Payout {self.amount_cents or 0} cents"
