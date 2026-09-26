import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from apps.accounts.access import owner_required
from apps.audit.services import record_event
from apps.capture.models import Submission, SubmissionKind, SubmissionStatus
from apps.core.models import DrawerFloat
from apps.inventory.models import Delivery, DeliveryStatus

from .duplicates import find_payout_duplicate
from .forms import DailyCashCountForm, PayoutAmountForm
from .models import (
    DailyCashCount,
    DailyReconciliation,
    PayoutRecord,
    PayoutStatus,
    ReconciliationStatus,
)
from .square_sync import sync_daily_reconciliation


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _selected_week(request) -> dt.date:
    requested = parse_date((request.GET.get("week") or "").strip())
    if requested is None:
        from apps.squareapi.client import business_day_for

        requested = business_day_for(timezone.now())
    return _week_start(requested)


def _format_difference(variance_cents: int) -> str:
    amount = f"${abs(variance_cents) / 100:,.2f}"
    if variance_cents < 0:
        return f"Short {amount}"
    if variance_cents > 0:
        return f"Over {amount}"
    return "Match"


def _daily_cash_row(reconciliation: DailyReconciliation) -> dict:
    submission = reconciliation.submission
    drawer_float = reconciliation.drawer_float_cents
    expected = reconciliation.expected_collection_cents
    if reconciliation.counted_cash_cents is not None and (drawer_float is None or expected is None):
        drawer_float = DrawerFloat.cents_for(submission.business_day)
        expected = reconciliation.counted_cash_cents - drawer_float

    history = list(reconciliation.daily_cash_counts.all())
    variance = reconciliation.collection_variance_cents
    is_approved = submission.status == SubmissionStatus.APPROVED
    can_count = is_approved and expected is not None
    if not is_approved:
        state = "not-ready"
        state_label = "Approve day first"
        state_class = "provisional"
    elif expected is None:
        state = "not-ready"
        state_label = "Drawer count unavailable"
        state_class = "failed"
    elif reconciliation.owner_collected_cents is None:
        state = "waiting"
        state_label = "Waiting count"
        state_class = "provisional"
    elif variance == 0:
        state = "matched"
        state_label = "Match"
        state_class = "approved"
    else:
        state = "issue"
        state_label = _format_difference(variance or 0)
        state_class = "mismatch"

    current_cents = reconciliation.owner_collected_cents
    initial = (
        DailyCashCountForm.initial_from_cents(current_cents) if current_cents is not None else ""
    )
    return {
        "reconciliation": reconciliation,
        "submission": submission,
        "week_start": _week_start(submission.business_day),
        "drawer_float_cents": drawer_float,
        "expected_cents": expected,
        "counted_cents": current_cents,
        "variance_cents": variance,
        "state": state,
        "state_label": state_label,
        "state_class": state_class,
        "can_count": can_count,
        "amount_initial": initial,
        "history": history,
        "is_corrected": len(history) > 1,
    }


def _daily_cash_queryset():
    revisions = DailyCashCount.objects.select_related("entered_by", "correction_of")
    return (
        DailyReconciliation.objects.select_related(
            "submission__submitted_by",
            "collection_recorded_by",
        )
        .prefetch_related(Prefetch("daily_cash_counts", queryset=revisions))
        .filter(submission__kind=SubmissionKind.DAILY_REPORT)
    )


@owner_required
def owner_dashboard(request):
    review_queue = (
        Submission.objects.select_related("submitted_by")
        .filter(
            status__in=[
                SubmissionStatus.NEEDS_REVIEW,
                SubmissionStatus.READY,
                SubmissionStatus.FAILED,
            ]
        )
        .prefetch_related("documents")[:30]
    )
    pending_payouts = PayoutRecord.objects.select_related("submission__submitted_by").filter(
        status=PayoutStatus.PENDING,
        submission__status=SubmissionStatus.APPROVED,
    )
    payout_totals = pending_payouts.aggregate(
        count=Count("id"), known_amount=Sum("amount_cents", filter=Q(amount_cents__isnull=False))
    )
    deliveries = Delivery.objects.select_related("submission__submitted_by").exclude(
        status=DeliveryStatus.PUSHED
    )[:12]
    daily_cash_base = _daily_cash_queryset().filter(
        submission__status=SubmissionStatus.APPROVED,
        counted_cash_cents__isnull=False,
    )
    daily_cash_waiting_count = daily_cash_base.filter(owner_collected_cents__isnull=True).count()
    daily_cash_issue_count = daily_cash_base.filter(
        Q(collection_variance_cents__lt=0) | Q(collection_variance_cents__gt=0)
    ).count()
    daily_cash_attention = daily_cash_base.filter(
        Q(owner_collected_cents__isnull=True)
        | Q(collection_variance_cents__lt=0)
        | Q(collection_variance_cents__gt=0)
    ).order_by("submission__business_day", "submission__created_at")[:8]
    recent_days = Submission.objects.select_related("submitted_by", "approved_by")[:20]
    return render(
        request,
        "reconcile/owner_dashboard.html",
        {
            "review_queue": review_queue,
            "pending_payouts": pending_payouts[:20],
            "payout_totals": payout_totals,
            "deliveries": deliveries,
            "daily_cash_waiting_count": daily_cash_waiting_count,
            "daily_cash_issue_count": daily_cash_issue_count,
            "daily_cash_attention": [_daily_cash_row(item) for item in daily_cash_attention],
            "recent_days": recent_days,
        },
    )


@owner_required
def daily_cash(request):
    week_start = _selected_week(request)
    week_end = week_start + dt.timedelta(days=6)
    selected = (
        _daily_cash_queryset()
        .exclude(submission__status=SubmissionStatus.REJECTED)
        .filter(submission__business_day__range=(week_start, week_end))
        .order_by("submission__business_day", "submission__created_at")
    )
    carryover = (
        _daily_cash_queryset()
        .filter(
            submission__status=SubmissionStatus.APPROVED,
            submission__business_day__lt=week_start,
            counted_cash_cents__isnull=False,
        )
        .filter(
            Q(owner_collected_cents__isnull=True)
            | Q(collection_variance_cents__lt=0)
            | Q(collection_variance_cents__gt=0)
        )
        .order_by("submission__business_day", "submission__created_at")
    )
    rows = [_daily_cash_row(item) for item in selected]
    carryover_rows = [_daily_cash_row(item) for item in carryover]
    eligible_rows = [row for row in rows if row["can_count"]]
    counted_rows = [row for row in eligible_rows if row["counted_cents"] is not None]
    expected_total = sum(row["expected_cents"] or 0 for row in eligible_rows)
    counted_total = sum(row["counted_cents"] or 0 for row in counted_rows)
    counted_expected_total = sum(row["expected_cents"] or 0 for row in counted_rows)
    return render(
        request,
        "reconcile/daily_cash.html",
        {
            "rows": rows,
            "carryover_rows": carryover_rows,
            "week_start": week_start,
            "week_end": week_end,
            "previous_week": week_start - dt.timedelta(days=7),
            "next_week": week_start + dt.timedelta(days=7),
            "drawer_float_cents": DrawerFloat.cents_for(week_start),
            "daily_cash_count": len(eligible_rows),
            "counted_count": len(counted_rows),
            "expected_total": expected_total,
            "counted_total": counted_total,
            "counted_difference": counted_total - counted_expected_total,
            "issue_count": sum(row["state"] == "issue" for row in eligible_rows),
        },
    )


def _approval_blocker(submission):
    if submission.status != SubmissionStatus.READY:
        return "Resolve the review items before approving this submission."
    if submission.documents.exclude(status__in=["EXTRACTED", "REVIEWED"]).exists():
        return "Every photo must be successfully extracted or human-reviewed."
    if submission.kind == SubmissionKind.INVENTORY:
        delivery = getattr(submission, "delivery", None)
        if not delivery or delivery.status != DeliveryStatus.READY:
            return "Match every included invoice line before approving the delivery."
    if submission.kind == SubmissionKind.PAYOUT:
        payout = getattr(submission, "payout_record", None)
        if not payout or payout.amount_cents is None or payout.amount_confirmed_at is None:
            return "The owner must confirm the payout amount before approval."
        if duplicate := find_payout_duplicate(payout):
            return duplicate.message
    if submission.kind == SubmissionKind.DAILY_REPORT:
        reconciliation = (
            DailyReconciliation.objects.select_for_update()
            .select_related("submission")
            .filter(submission=submission)
            .first()
        )
        if reconciliation is None or reconciliation.status != ReconciliationStatus.MATCHED:
            return "Pull Square and resolve every day-level mismatch before approval."
        if reconciliation.counted_cash_cents is None:
            return "Confirm the ended drawer's counted cash before approving the day."
    return ""


@owner_required
@require_POST
def submission_decision(request, pk):
    decision = request.POST.get("decision")
    note = (request.POST.get("owner_note") or "").strip()

    with transaction.atomic():
        submission = get_object_or_404(
            Submission.objects.select_for_update().prefetch_related("documents"),
            pk=pk,
        )
        if submission.is_terminal:
            messages.error(request, "This submission already has a final decision.")
            return redirect("capture:detail", pk=submission.pk)
        if decision == "approve":
            blocker = _approval_blocker(submission)
            if blocker:
                if submission.kind == SubmissionKind.PAYOUT:
                    payout = getattr(submission, "payout_record", None)
                    duplicate = find_payout_duplicate(payout) if payout else None
                    if duplicate:
                        record_event(
                            request,
                            "payout.duplicate_approval_blocked",
                            payout,
                            duplicate.to_dict(),
                        )
                messages.error(request, blocker)
                return redirect("capture:detail", pk=submission.pk)
            submission.status = SubmissionStatus.APPROVED
            submission.approved_at = timezone.now()
            submission.approved_by = request.user
            submission.owner_note = note
            submission.save(
                update_fields=["status", "approved_at", "approved_by", "owner_note", "updated_at"]
            )
            reconciliation = getattr(submission, "daily_reconciliation", None)
            if reconciliation:
                reconciliation.status = ReconciliationStatus.APPROVED
                if reconciliation.counted_cash_cents is not None:
                    drawer_float = DrawerFloat.cents_for(submission.business_day)
                    reconciliation.drawer_float_cents = drawer_float
                    reconciliation.expected_collection_cents = (
                        reconciliation.counted_cash_cents - drawer_float
                    )
                reconciliation.save(
                    update_fields=[
                        "status",
                        "drawer_float_cents",
                        "expected_collection_cents",
                        "updated_at",
                    ]
                )
            record_event(request, "submission.approved", submission, {"note": note})
            messages.success(
                request, "Submission approved. No Square inventory was posted automatically."
            )
        elif decision == "reject":
            if not note:
                messages.error(
                    request, "Add a reason so the employee knows what to retake or correct."
                )
                return redirect("capture:detail", pk=submission.pk)
            submission.status = SubmissionStatus.REJECTED
            submission.approved_at = timezone.now()
            submission.approved_by = request.user
            submission.owner_note = note
            submission.save(
                update_fields=["status", "approved_at", "approved_by", "owner_note", "updated_at"]
            )
            payout = getattr(submission, "payout_record", None)
            if payout and payout.status == PayoutStatus.PENDING:
                payout.status = PayoutStatus.VOID
                payout.save(update_fields=["status", "updated_at"])
                record_event(
                    request,
                    "payout.voided",
                    payout,
                    {"reason": "submission rejected"},
                )
            record_event(request, "submission.rejected", submission, {"reason": note})
            messages.success(request, "Submission rejected with instructions for the employee.")
        else:
            messages.error(request, "Choose approve or reject.")
    return redirect("capture:detail", pk=submission.pk)


@owner_required
@require_POST
def reimburse_payout(request, pk):
    with transaction.atomic():
        payout = get_object_or_404(
            PayoutRecord.objects.select_for_update().select_related("submission"),
            pk=pk,
        )
        if payout.status != PayoutStatus.PENDING:
            messages.error(request, "This payout is no longer awaiting reimbursement.")
            return redirect("reconcile:owner-dashboard")
        if payout.submission.status != SubmissionStatus.APPROVED:
            messages.error(request, "Approve the payout evidence before recording reimbursement.")
            return redirect("capture:detail", pk=payout.submission_id)
        if payout.amount_cents is None:
            messages.error(request, "Confirm the amount before marking it reimbursed.")
            return redirect("capture:detail", pk=payout.submission_id)
        if duplicate := find_payout_duplicate(payout):
            record_event(
                request,
                "payout.duplicate_reimbursement_blocked",
                payout,
                duplicate.to_dict(),
            )
            messages.error(request, duplicate.message)
            return redirect("capture:detail", pk=payout.submission_id)
        payout.status = PayoutStatus.REIMBURSED
        payout.reimbursed_at = timezone.now()
        payout.reimbursed_by = request.user
        payout.reimbursement_note = (request.POST.get("note") or "")[:300]
        payout.save(
            update_fields=[
                "status",
                "reimbursed_at",
                "reimbursed_by",
                "reimbursement_note",
                "updated_at",
            ]
        )
        record_event(
            request,
            "payout.reimbursed",
            payout,
            {"amount_cents": payout.amount_cents, "note": payout.reimbursement_note},
        )
    messages.success(request, "Payout marked reimbursed.")
    return redirect("reconcile:owner-dashboard")


@owner_required
@require_POST
def confirm_payout_amount(request, pk):
    payout = get_object_or_404(PayoutRecord.objects.select_related("submission"), pk=pk)
    form = PayoutAmountForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a valid payout amount with no more than two decimal places.")
        return redirect("capture:detail", pk=payout.submission_id)

    amount_cents = int(form.cleaned_data["amount"] * Decimal(100))
    with transaction.atomic():
        payout = get_object_or_404(
            PayoutRecord.objects.select_for_update().select_related("submission"),
            pk=pk,
        )
        if payout.submission.is_terminal:
            messages.error(request, "A final submission cannot be changed.")
            return redirect("capture:detail", pk=payout.submission_id)
        if payout.status != PayoutStatus.PENDING:
            messages.error(request, "A reimbursed or void payout amount cannot be changed.")
            return redirect("capture:detail", pk=payout.submission_id)
        previous_cents = payout.amount_cents
        payout.amount_cents = amount_cents
        payout.amount_confirmed_at = timezone.now()
        payout.amount_confirmed_by = request.user
        payout.save(
            update_fields=[
                "amount_cents",
                "amount_confirmed_at",
                "amount_confirmed_by",
                "updated_at",
            ]
        )
        record_event(
            request,
            "payout.amount_confirmed",
            payout,
            {"previous_cents": previous_cents, "amount_cents": amount_cents},
        )
    messages.success(request, "Payout amount confirmed.")
    return redirect("capture:detail", pk=payout.submission_id)


@owner_required
@require_POST
def sync_square_day(request, pk):
    submission = get_object_or_404(
        Submission.objects.select_related("daily_reconciliation"),
        pk=pk,
        kind=SubmissionKind.DAILY_REPORT,
    )
    if submission.is_terminal:
        messages.error(request, "A final submission cannot be resynchronized.")
        return redirect("capture:detail", pk=submission.pk)
    if submission.status not in {SubmissionStatus.READY, SubmissionStatus.NEEDS_REVIEW}:
        messages.error(request, "Wait for photo reading to finish before pulling the Square day.")
        return redirect("capture:detail", pk=submission.pk)

    def audit_sync(target, result):
        record_event(
            request,
            "reconciliation.square_synced",
            target,
            {
                "status": result.status,
                "problems": list(result.problems),
                "comparison_count": len(result.comparisons),
            },
        )

    try:
        result = sync_daily_reconciliation(
            submission.daily_reconciliation,
            on_persist=audit_sync,
        )
    except Exception:
        messages.error(
            request,
            "Square data could not be read. Check the connection and try again; no Square data was changed.",
        )
        return redirect("capture:detail", pk=submission.pk)

    if result.status == ReconciliationStatus.MATCHED:
        messages.success(
            request, "Square drawer and payment totals match the photographed reports."
        )
    elif result.status == ReconciliationStatus.MISMATCH:
        messages.warning(
            request, "Square was pulled, but at least one amount does not match the photos."
        )
    else:
        messages.warning(
            request,
            "Square was pulled, but the day is incomplete or ambiguous. Review the details below.",
        )
    return redirect("capture:detail", pk=submission.pk)


@owner_required
@require_POST
def record_daily_cash_count(request, pk):
    form = DailyCashCountForm(request.POST)
    week_start = None
    with transaction.atomic():
        submission = get_object_or_404(
            Submission.objects.select_for_update(of=("self",)),
            pk=pk,
            kind=SubmissionKind.DAILY_REPORT,
        )
        week_start = _week_start(submission.business_day)
        reconciliation = get_object_or_404(
            DailyReconciliation.objects.select_for_update().select_related("submission"),
            submission=submission,
        )
        if submission.status != SubmissionStatus.APPROVED:
            messages.error(
                request,
                "Approve this day's evidence before entering its daily cash count.",
            )
            return redirect("capture:detail", pk=submission.pk)
        if not form.is_valid():
            messages.error(
                request,
                "Enter a valid daily cash amount with no more than two decimal places.",
            )
            return redirect(
                f"{reverse('reconcile:daily-cash')}?week={week_start.isoformat()}"
                f"#daily-cash-{reconciliation.pk}"
            )
        counted_cents = int(form.cleaned_data["amount"] * Decimal(100))
        if reconciliation.counted_cash_cents is None:
            messages.error(
                request,
                "The ended register count is unavailable, so daily cash cannot be checked yet.",
            )
            return redirect("capture:detail", pk=submission.pk)

        drawer_float = reconciliation.drawer_float_cents
        expected = reconciliation.expected_collection_cents
        if drawer_float is None or expected is None:
            drawer_float = DrawerFloat.cents_for(submission.business_day)
            expected = reconciliation.counted_cash_cents - drawer_float
        variance = counted_cents - expected
        note = form.cleaned_data["note"].strip()
        evidence_hash = reconciliation.current_collection_evidence_hash()
        previous = reconciliation.daily_cash_counts.order_by("-created_at", "-pk").first()
        if (
            previous is not None
            and previous.counted_cents == counted_cents
            and previous.note == note
            and previous.expected_cents == expected
            and previous.evidence_hash == evidence_hash
        ):
            messages.success(request, "That daily cash count is already the current value.")
            return redirect(
                f"{reverse('reconcile:daily-cash')}?week={week_start.isoformat()}"
                f"#daily-cash-{reconciliation.pk}"
            )

        revision = DailyCashCount(
            reconciliation=reconciliation,
            correction_of=previous,
            drawer_float_cents=drawer_float,
            expected_cents=expected,
            counted_cents=counted_cents,
            variance_cents=variance,
            evidence_hash=evidence_hash,
            note=note,
            entered_by=request.user,
        )
        revision.save()
        previous_snapshot = None
        if previous is not None:
            previous_snapshot = {
                "revision_id": str(previous.pk),
                "counted_cents": previous.counted_cents,
                "expected_cents": previous.expected_cents,
                "variance_cents": previous.variance_cents,
                "note": previous.note,
                "recorded_at": previous.created_at.isoformat(),
            }

        reconciliation.drawer_float_cents = drawer_float
        reconciliation.expected_collection_cents = expected
        reconciliation.owner_collected_cents = counted_cents
        reconciliation.collection_variance_cents = variance
        reconciliation.collection_note = note
        reconciliation.collection_evidence_hash = evidence_hash
        reconciliation.collection_recorded_at = revision.created_at
        reconciliation.collection_recorded_by = request.user
        reconciliation.save(
            update_fields=[
                "drawer_float_cents",
                "expected_collection_cents",
                "owner_collected_cents",
                "collection_variance_cents",
                "collection_note",
                "collection_evidence_hash",
                "collection_recorded_at",
                "collection_recorded_by",
                "updated_at",
            ]
        )
        action = "cash.daily_corrected" if previous is not None else "cash.daily_counted"
        record_event(
            request,
            action,
            revision,
            {
                "business_day": submission.business_day.isoformat(),
                "previous": previous_snapshot,
                "new": {
                    "revision_id": str(revision.pk),
                    "drawer_float_cents": drawer_float,
                    "expected_cents": expected,
                    "counted_cents": counted_cents,
                    "variance_cents": variance,
                    "note": note,
                    "evidence_hash": evidence_hash,
                },
            },
        )
    if variance:
        messages.warning(
            request,
            f"Daily cash saved with an issue: {_format_difference(variance)}. Count it again and correct the value if needed.",
        )
    else:
        messages.success(request, "Daily cash matches the expected amount exactly.")
    return redirect(
        f"{reverse('reconcile:daily-cash')}?week={week_start.isoformat()}"
        f"#daily-cash-{reconciliation.pk}"
    )
