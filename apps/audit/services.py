from __future__ import annotations

from typing import Any

from .models import AuditEvent


def record_event(request, action: str, target: Any, detail: dict | None = None) -> AuditEvent:
    """Append one audit event without coupling views to storage details."""
    actor = getattr(request, "user", None)
    if not getattr(actor, "is_authenticated", False):
        actor = None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip_address = forwarded.split(",", 1)[0].strip() or request.META.get("REMOTE_ADDR")
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target._meta.label_lower,
        target_id=str(target.pk),
        detail=detail or {},
        ip_address=ip_address or None,
    )
