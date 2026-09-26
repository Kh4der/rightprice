"""Background entry points for the optional self-hosted Claude inventory path."""

from __future__ import annotations

from celery import shared_task

from apps.accounts.models import User
from apps.capture.models import SubmissionStatus

from .claude_sandbox import dispatch_inventory_sandbox_job
from .models import Delivery, DeliveryStatus


@shared_task(name="inventory.dispatch_claude_sandbox")
def dispatch_claude_inventory_job(delivery_id: str, requested_by_id: str) -> str:
    delivery = Delivery.objects.select_related("submission").get(pk=delivery_id)
    requested_by = User.objects.get(pk=requested_by_id)
    try:
        job = dispatch_inventory_sandbox_job(delivery, requested_by=requested_by)
    except Exception as exc:
        # Uploaded evidence remains safe. Move the submission to a visible
        # review state rather than leaving it stuck as queued.
        Delivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.NEEDS_REVIEW)
        delivery.submission.status = SubmissionStatus.NEEDS_REVIEW
        delivery.submission.processing_error = (
            "Invoice photos were saved, but Claude sandbox processing could not start: "
            f"{type(exc).__name__}"
        )
        delivery.submission.save(
            update_fields=["status", "processing_error", "updated_at"]
        )
        raise
    return str(job.pk)
