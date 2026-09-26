from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render

from apps.capture.models import Submission, SubmissionStatus
from apps.reconcile.models import PayoutRecord, PayoutStatus


@login_required
def home(request):
    submissions = Submission.objects.select_related("submitted_by")
    if not request.user.is_owner:
        submissions = submissions.filter(submitted_by=request.user)
    recent = submissions[:6]
    pending_count = submissions.filter(
        status__in=[
            SubmissionStatus.QUEUED,
            SubmissionStatus.PROCESSING,
            SubmissionStatus.NEEDS_REVIEW,
            SubmissionStatus.READY,
        ]
    ).count()
    payout_summary = None
    if request.user.is_owner:
        payout_summary = PayoutRecord.objects.filter(
            status=PayoutStatus.PENDING,
            submission__status=SubmissionStatus.APPROVED,
        ).aggregate(
            count=Count("id"), amount=Sum("amount_cents", filter=Q(amount_cents__isnull=False))
        )
    return render(
        request,
        "core/home.html",
        {"recent": recent, "pending_count": pending_count, "payout_summary": payout_summary},
    )
