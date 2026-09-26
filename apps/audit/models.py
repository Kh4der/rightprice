"""Append-only audit events and field-level correction history."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class AppendOnlyQuerySet(models.QuerySet):
    """Block ORM bulk operations that bypass model save/delete methods."""

    def update(self, **kwargs):
        raise ValidationError("Audit records are append-only and cannot be changed.")

    def delete(self):
        raise ValidationError("Audit records are append-only and cannot be deleted.")


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=80, db_index=True)
    target_id = models.CharField(max_length=80, db_index=True)
    detail = models.JSONField(default=dict, blank=True)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        default_manager_name = "objects"
        indexes = [models.Index(fields=["target_type", "target_id", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"

    def save(self, *args, **kwargs):
        if self.pk and AuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("Audit events are append-only and cannot be changed.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events are append-only and cannot be deleted.")


class FieldCorrection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        "capture.Document",
        on_delete=models.PROTECT,
        related_name="corrections",
    )
    field_path = models.CharField(max_length=240)
    previous_value = models.JSONField(null=True, blank=True)
    corrected_value = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=300)
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="field_corrections",
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        base_manager_name = "objects"
        default_manager_name = "objects"

    def __str__(self) -> str:
        return f"{self.document_id}: {self.field_path}"

    def save(self, *args, **kwargs):
        if self.pk and FieldCorrection.objects.filter(pk=self.pk).exists():
            raise ValidationError("Field corrections are append-only and cannot be changed.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Field corrections are append-only and cannot be deleted.")
