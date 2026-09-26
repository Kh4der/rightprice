"""Duplicate-evidence checks for the payout reimbursement workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from django.db.models import Q

from apps.capture.models import SubmissionStatus

from .models import PayoutRecord


@dataclass(frozen=True)
class PayoutDuplicate:
    """One concrete reason a payout must not be approved or reimbursed."""

    match_type: str
    value: str
    other_payout_id: str
    other_submission_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def message(self) -> str:
        if self.match_type == "source_photo":
            return (
                "This payout uses the same evidence photo as another non-rejected "
                "payout. Reject one record before continuing."
            )
        label = (
            "validation reference"
            if self.match_type == "validation_reference"
            else "ticket reference"
        )
        return (
            f"Duplicate payout: {label} {self.value} is already attached to "
            "another non-rejected payout. Reject one record before continuing."
        )


def find_payout_duplicate(payout: PayoutRecord) -> PayoutDuplicate | None:
    """Return the first stable duplicate signal for a payout, if one exists.

    Rejected submissions are intentionally ignored: rejection is the owner's
    explicit way to retire a mistaken duplicate without deleting its evidence.
    """

    candidates = PayoutRecord.objects.exclude(pk=payout.pk).exclude(
        submission__status=SubmissionStatus.REJECTED
    )

    validation_reference = payout.validation_reference.strip()
    if validation_reference:
        other = (
            candidates.filter(validation_reference__iexact=validation_reference)
            .order_by("created_at", "pk")
            .first()
        )
        if other is not None:
            return _duplicate("validation_reference", validation_reference, other)

    ticket_reference = payout.ticket_reference.strip()
    if ticket_reference:
        other = (
            candidates.filter(ticket_reference__iexact=ticket_reference)
            .order_by("created_at", "pk")
            .first()
        )
        if other is not None:
            return _duplicate("ticket_reference", ticket_reference, other)

    source_hashes = tuple(
        payout.submission.documents.exclude(sha256="").values_list("sha256", flat=True).distinct()
    )
    if source_hashes:
        other = (
            candidates.filter(Q(submission__documents__sha256__in=source_hashes))
            .order_by("created_at", "pk")
            .first()
        )
        if other is not None:
            return _duplicate("source_photo", "", other)

    return None


def _duplicate(match_type: str, value: str, other: PayoutRecord) -> PayoutDuplicate:
    return PayoutDuplicate(
        match_type=match_type,
        value=value,
        other_payout_id=str(other.pk),
        other_submission_id=str(other.submission_id),
    )


__all__ = ["PayoutDuplicate", "find_payout_duplicate"]
