from __future__ import annotations

import datetime as dt
import hashlib
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.access import owner_required
from apps.audit.services import record_event
from apps.inventory.models import Delivery
from apps.reconcile.forms import DailyCashCountForm, PayoutAmountForm
from apps.reconcile.models import DailyReconciliation, PayoutRecord

from .forms import (
    SAFE_IMAGE_MEDIA_TYPES,
    DailyReportForm,
    InventoryCaptureForm,
    PayoutCaptureForm,
    validate_image,
)
from .models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from .staging import (
    PendingUploadError,
    consume_pending_upload,
    lock_pending_uploads,
    stage_upload,
)


def _visible_submissions(user):
    queryset = Submission.objects.select_related("submitted_by", "approved_by").prefetch_related(
        "documents"
    )
    return queryset if user.is_owner else queryset.filter(submitted_by=user)


def _save_document(submission, uploaded, requested_type):
    raw = uploaded.read()
    digest = hashlib.sha256(raw).hexdigest()
    document = Document(
        submission=submission,
        original_name=(uploaded.name or "photo")[:255],
        media_type=(getattr(uploaded, "content_type", "") or "application/octet-stream")[:100],
        size_bytes=len(raw),
        sha256=digest,
        requested_type=requested_type,
    )
    document.file.save(document.original_name, ContentFile(raw), save=False)
    document.save()
    return document


def _save_capture_field(submission, form, staged, field_name, requested_type):
    """Persist normal multipart files and consume any one-time staged files."""

    uploaded = form.cleaned_data.get(field_name)
    files = uploaded if isinstance(uploaded, list) else ([uploaded] if uploaded else [])
    for item in files:
        _save_document(submission, item, requested_type)
    for pending in staged.get(field_name, []):
        consume_pending_upload(
            pending=pending,
            submission=submission,
            requested_type=requested_type,
        )


def _capture_context(form, *, title, step_hint, submission_kind):
    return {
        "form": form,
        "title": title,
        "step_hint": step_hint,
        "submission_kind": submission_kind,
        # Local and VPS deployments keep the original single multipart POST.
        # Vercel stages each photo so the aggregate never meets its body cap.
        "staged_uploads_enabled": settings.IS_VERCEL,
    }


def _queue(submission, *, force=False):
    if submission.submitted_by.is_demo:
        raise PermissionDenied("Practice mode never sends photos to an AI service.")
    from apps.extraction.tasks import process_submission

    submission.status = SubmissionStatus.QUEUED
    submission.submitted_at = timezone.now()
    submission.save(update_fields=["status", "submitted_at", "updated_at"])

    def send():
        try:
            if (
                submission.kind == SubmissionKind.INVENTORY
                and settings.CLAUDE_INVENTORY_SANDBOX_ENABLED
                and settings.CLAUDE_INVENTORY_SANDBOX_PRIMARY
            ):
                from apps.inventory.tasks import dispatch_claude_inventory_job

                dispatch_claude_inventory_job.delay(
                    str(submission.delivery.pk), str(submission.submitted_by_id)
                )
            else:
                process_submission.delay(str(submission.pk), force=force)
        except Exception as exc:  # broker outage must not lose uploaded evidence
            Submission.objects.filter(pk=submission.pk).update(
                status=SubmissionStatus.NEEDS_REVIEW,
                processing_error=f"Photos saved, but automatic reading could not be queued: {exc}",
            )

    transaction.on_commit(send)


@login_required
def submission_list(request):
    submissions = _visible_submissions(request.user)[:100]
    return render(request, "capture/submission_list.html", {"submissions": submissions})


@login_required
def submission_detail(request, pk):
    submission = get_object_or_404(_visible_submissions(request.user), pk=pk)
    context = {"submission": submission}
    context["delivery"] = getattr(submission, "delivery", None)
    context["reconciliation"] = getattr(submission, "daily_reconciliation", None)
    context["payout"] = getattr(submission, "payout_record", None)
    payout = context["payout"]
    if payout and payout.amount_cents is not None:
        context["payout_amount_initial"] = PayoutAmountForm.initial_from_cents(payout.amount_cents)
    reconciliation = context["reconciliation"]
    if reconciliation:
        context["daily_cash_history"] = reconciliation.daily_cash_counts.select_related(
            "entered_by", "correction_of"
        )
        if reconciliation.owner_collected_cents is not None:
            context["daily_cash_amount_initial"] = DailyCashCountForm.initial_from_cents(
                reconciliation.owner_collected_cents
            )
        context["daily_cash_week_start"] = submission.business_day - dt.timedelta(
            days=submission.business_day.weekday()
        )
    return render(request, "capture/submission_detail.html", context)


@login_required
def daily_create(request):
    form = DailyReportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                staged = lock_pending_uploads(
                    user=request.user,
                    submission_kind=SubmissionKind.DAILY_REPORT,
                    staged=form.cleaned_data["staged_uploads"],
                )
                submission = Submission.objects.create(
                    kind=SubmissionKind.DAILY_REPORT,
                    business_day=form.cleaned_data["business_day"],
                    submitted_by=request.user,
                    employee_note=form.cleaned_data["note"],
                )
                slots = [
                    ("sales_report", DocumentType.SQUARE_SALES_REPORT),
                    ("drawer", DocumentType.SQUARE_DRAWER_SCREEN),
                    ("lottery_daily", DocumentType.LOTTERY_DAILY_SALES),
                    ("ticket_balance", DocumentType.LOTTERY_TICKET_BALANCE),
                ]
                for field, doc_type in slots:
                    _save_capture_field(submission, form, staged, field, doc_type)
                DailyReconciliation.objects.create(submission=submission)
                record_event(request, "submission.created", submission, {"kind": submission.kind})
                _queue(submission)
        except PendingUploadError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Daily report saved. The photos are being read now.")
            return redirect("capture:detail", pk=submission.pk)
    return render(
        request,
        "capture/capture_form.html",
        _capture_context(
            form,
            title="Daily close",
            step_hint="Add each report in its matching slot.",
            submission_kind=SubmissionKind.DAILY_REPORT,
        ),
    )


@login_required
def payout_create(request):
    form = PayoutCaptureForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                staged = lock_pending_uploads(
                    user=request.user,
                    submission_kind=SubmissionKind.PAYOUT,
                    staged=form.cleaned_data["staged_uploads"],
                )
                submission = Submission.objects.create(
                    kind=SubmissionKind.PAYOUT,
                    business_day=form.cleaned_data["business_day"],
                    submitted_by=request.user,
                    employee_note=form.cleaned_data["note"],
                )
                _save_capture_field(
                    submission,
                    form,
                    staged,
                    "payout_photo",
                    DocumentType.LOTTERY_PAYOUT,
                )
                dollars = form.cleaned_data.get("amount")
                cents = None
                if dollars is not None:
                    cents = int(
                        (Decimal(dollars) * 100).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                PayoutRecord.objects.create(
                    submission=submission,
                    amount_cents=cents,
                    employee_reported_amount_cents=cents,
                )
                record_event(request, "submission.created", submission, {"kind": submission.kind})
                _queue(submission)
        except PendingUploadError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Payout evidence saved for the owner's reimbursement queue.")
            return redirect("capture:detail", pk=submission.pk)
    return render(
        request,
        "capture/capture_form.html",
        _capture_context(
            form,
            title="Record lottery payout",
            step_hint="Keep the ticket number and amount readable.",
            submission_kind=SubmissionKind.PAYOUT,
        ),
    )


@login_required
def inventory_create(request):
    form = InventoryCaptureForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                staged = lock_pending_uploads(
                    user=request.user,
                    submission_kind=SubmissionKind.INVENTORY,
                    staged=form.cleaned_data["staged_uploads"],
                )
                submission = Submission.objects.create(
                    kind=SubmissionKind.INVENTORY,
                    business_day=form.cleaned_data["business_day"],
                    submitted_by=request.user,
                    employee_note=form.cleaned_data["note"],
                )
                _save_capture_field(
                    submission,
                    form,
                    staged,
                    "invoice_photos",
                    DocumentType.DELIVERY_INVOICE,
                )
                Delivery.objects.create(
                    submission=submission,
                    vendor_name_raw=form.cleaned_data.get("vendor_name", ""),
                )
                record_event(request, "submission.created", submission, {"kind": submission.kind})
                _queue(submission)
        except PendingUploadError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Invoice saved. It is being read in the background.")
            return redirect("capture:detail", pk=submission.pk)
    return render(
        request,
        "capture/capture_form.html",
        _capture_context(
            form,
            title="Receive delivery",
            step_hint="Photograph every invoice page edge to edge.",
            submission_kind=SubmissionKind.INVENTORY,
        ),
    )


@login_required
@require_POST
def stage_document(request):
    """Accept one authenticated photo and return an opaque, one-time ID."""

    files = request.FILES.getlist("photo")
    if len(files) != 1:
        return JsonResponse({"error": "Choose exactly one photo."}, status=400)

    uploaded = files[0]
    try:
        validate_image(uploaded)
        pending = stage_upload(
            user=request.user,
            submission_kind=request.POST.get("submission_kind", ""),
            field_name=request.POST.get("field_name", ""),
            uploaded=uploaded,
        )
    except ValidationError as exc:
        response = JsonResponse({"error": " ".join(exc.messages)}, status=400)
    except PendingUploadError as exc:
        response = JsonResponse({"error": str(exc)}, status=400)
    else:
        response = JsonResponse({"stage_id": str(pending.pk)}, status=201)
    response["Cache-Control"] = "no-store"
    return response


@login_required
def document_file(request, pk):
    document = get_object_or_404(Document.objects.select_related("submission__submitted_by"), pk=pk)
    if not request.user.is_owner and document.submission.submitted_by_id != request.user.id:
        raise Http404
    safe_media_types = set(SAFE_IMAGE_MEDIA_TYPES.values())
    content_type = (
        document.media_type
        if document.media_type in safe_media_types
        else "application/octet-stream"
    )
    response = FileResponse(document.file.open("rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{document.id}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def retry_submission(request, pk):
    with transaction.atomic():
        submission = get_object_or_404(
            _visible_submissions(request.user).select_for_update(of=("self",)),
            pk=pk,
        )
        if submission.status not in {SubmissionStatus.FAILED, SubmissionStatus.NEEDS_REVIEW}:
            messages.error(
                request,
                "Only a failed submission or one needing review can be reprocessed.",
            )
        else:
            record_event(request, "submission.retried", submission)
            _queue(submission, force=True)
            messages.success(request, "The photos were queued again.")
    return redirect("capture:detail", pk=submission.pk)


@owner_required
@require_POST
def review_document(request, pk):
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 5:
        messages.error(
            request, "Explain what you checked or corrected before accepting the reading."
        )
        document = get_object_or_404(Document, pk=pk)
        return redirect("capture:detail", pk=document.submission_id)

    with transaction.atomic():
        document = get_object_or_404(
            Document.objects.select_for_update().select_related("submission"),
            pk=pk,
        )
        if document.submission.is_terminal:
            messages.error(request, "A final submission cannot be changed.")
            return redirect("capture:detail", pk=document.submission_id)
        if document.status != DocumentStatus.NEEDS_REVIEW:
            messages.error(request, "Only a reading that needs review can be accepted this way.")
            return redirect("capture:detail", pk=document.submission_id)

        reviewed_at = timezone.now().isoformat()
        checks = document.check_results if isinstance(document.check_results, list) else []
        reviewed_checks = []
        for check in checks:
            if not isinstance(check, dict) or check.get("passed") is not False:
                reviewed_checks.append(check)
                continue
            reviewed_checks.append(
                {
                    **check,
                    "original_severity": check.get("severity", "hard"),
                    "severity": "reviewed",
                    "human_review": {
                        "reviewed_at": reviewed_at,
                        "reviewed_by": str(request.user.pk),
                        "reason": reason,
                    },
                }
            )
        document.check_results = reviewed_checks
        document.status = DocumentStatus.REVIEWED
        document.save(update_fields=["check_results", "status"])
        record_event(
            request,
            "document.reviewed",
            document,
            {
                "reason": reason,
                "failed_check_count": sum(
                    isinstance(check, dict) and check.get("passed") is False for check in checks
                ),
            },
        )

        from apps.extraction.processing import aggregate_submission_status
        from apps.extraction.workflows import materialize_submission

        aggregate_submission_status(document.submission_id)
        document.submission.refresh_from_db()
        materialize_submission(document.submission)

    messages.success(request, "The reviewed photo reading is now recorded with your reason.")
    return redirect("capture:detail", pk=document.submission_id)
