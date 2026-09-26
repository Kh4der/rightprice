"""Secure, one-time staging for photos sent ahead of a capture form."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import suppress

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from .models import Document, PendingUpload

STAGED_FIELD_LIMITS = {
    "DAILY_REPORT": {
        "sales_report": 1,
        "drawer": 1,
        "lottery_daily": 1,
        "ticket_balance": 1,
    },
    "PAYOUT": {"payout_photo": 1},
    "INVENTORY": {"invoice_photos": 12},
}

# This is deliberately higher than any one form but low enough to stop an
# authenticated account from using an abandoned form as unbounded blob storage.
MAX_ACTIVE_PENDING_UPLOADS = 32
# Leaves ample room for multipart headers beneath Vercel's 4.5 MB body limit.
STAGED_UPLOAD_MAX_BYTES = 3_750_000


class PendingUploadError(ValueError):
    """A staged token is invalid, expired, owned elsewhere, or already used."""


def stage_upload(*, user, submission_kind: str, field_name: str, uploaded: UploadedFile):
    allowed = STAGED_FIELD_LIMITS.get(submission_kind, {})
    if field_name not in allowed:
        raise PendingUploadError("This photo slot is not valid for the selected form.")
    if uploaded.size > STAGED_UPLOAD_MAX_BYTES:
        raise PendingUploadError(
            "This photo is still too large after preparation. Choose a photo smaller than 3.5 MB."
        )

    cleanup_expired_uploads(user=user, limit=8)
    position = uploaded.tell()
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(position)

    original_name = str(uploaded.name or "photo").replace("\\", "/").rsplit("/", 1)[-1]
    original_name = (original_name or "photo")[:255]
    media_type = (getattr(uploaded, "content_type", "") or "application/octet-stream")[:100]
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }.get(media_type, ".img")
    pending = None
    try:
        with transaction.atomic():
            # Serialize staging for one account so parallel requests cannot
            # race through the storage quota check.
            user.__class__.objects.select_for_update().only("pk").get(pk=user.pk)
            active_count = PendingUpload.objects.filter(
                uploaded_by=user,
                consumed_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).count()
            if active_count >= MAX_ACTIVE_PENDING_UPLOADS:
                raise PendingUploadError(
                    "Too many unfinished photos are waiting. Finish this form or try again later."
                )

            pending = PendingUpload(
                uploaded_by=user,
                submission_kind=submission_kind,
                field_name=field_name,
                original_name=original_name,
                media_type=media_type,
                size_bytes=uploaded.size,
                sha256=digest.hexdigest(),
            )
            pending.file.save(f"photo{extension}", uploaded, save=False)
            pending.save()
    except Exception:
        # A database or commit failure after the blob write must not strand
        # private data. Preserve the original exception if cleanup also fails.
        if pending is not None and pending.file.name:
            with suppress(Exception):
                pending.file.storage.delete(pending.file.name)
        raise
    return pending


def lock_pending_uploads(*, user, submission_kind: str, staged: Mapping[str, list[str]]):
    """Lock and revalidate every supplied ID without leaking cross-user state."""

    requested = [item for items in staged.values() for item in items]
    if not requested:
        return {}
    if len(requested) != len(set(requested)):
        raise PendingUploadError("A staged photo cannot be used more than once.")

    rows = list(PendingUpload.objects.select_for_update().filter(pk__in=requested))
    by_id = {str(row.pk): row for row in rows}
    if len(rows) != len(requested):
        raise PendingUploadError("A staged photo is unavailable. Choose the photo again.")

    now = timezone.now()
    locked: dict[str, list[PendingUpload]] = {}
    allowed = STAGED_FIELD_LIMITS.get(submission_kind, {})
    for field_name, ids in staged.items():
        if field_name not in allowed or len(ids) > allowed[field_name]:
            raise PendingUploadError("The staged photo list is invalid. Choose the photos again.")
        field_rows = []
        for pending_id in ids:
            pending = by_id.get(str(pending_id))
            if (
                pending is None
                or pending.uploaded_by_id != user.pk
                or pending.submission_kind != submission_kind
                or pending.field_name != field_name
                or pending.consumed_at is not None
                or pending.consumed_document_id is not None
                or pending.expires_at <= now
            ):
                # Keep the response identical for missing, cross-user, expired,
                # and replayed IDs so the token cannot be used as an oracle.
                raise PendingUploadError("A staged photo is unavailable. Choose the photo again.")
            field_rows.append(pending)
        if field_rows:
            locked[field_name] = field_rows
    return locked


def consume_pending_upload(
    *, pending: PendingUpload, submission, requested_type: str
) -> Document:
    """Attach an already-private object to evidence and burn its staging token."""

    if pending.consumed_at is not None or pending.consumed_document_id is not None:
        raise PendingUploadError("A staged photo is unavailable. Choose the photo again.")
    document = Document.objects.create(
        submission=submission,
        file=pending.file.name,
        original_name=pending.original_name,
        media_type=pending.media_type,
        size_bytes=pending.size_bytes,
        sha256=pending.sha256,
        requested_type=requested_type,
    )
    pending.consumed_at = timezone.now()
    pending.consumed_document = document
    pending.save(update_fields=["consumed_at", "consumed_document"])
    return document


def cleanup_expired_uploads(*, user=None, limit: int = 100) -> tuple[int, int]:
    """Delete expired, never-consumed photos; return (deleted, failed)."""

    queryset = PendingUpload.objects.filter(
        consumed_at__isnull=True,
        consumed_document__isnull=True,
        expires_at__lte=timezone.now(),
    ).order_by("expires_at")
    if user is not None:
        queryset = queryset.filter(uploaded_by=user)

    deleted = failed = 0
    for pending in queryset[: max(0, limit)]:
        with transaction.atomic():
            # Recheck after acquiring a row lock. A concurrent final submit may
            # have consumed the token after the candidate query above.
            try:
                candidate = PendingUpload.objects.select_for_update().get(
                    pk=pending.pk,
                    consumed_at__isnull=True,
                    consumed_document__isnull=True,
                    expires_at__lte=timezone.now(),
                )
            except PendingUpload.DoesNotExist:
                continue
            try:
                candidate.file.storage.delete(candidate.file.name)
            except Exception:
                failed += 1
                continue
            candidate.delete()
            deleted += 1
    return deleted, failed
