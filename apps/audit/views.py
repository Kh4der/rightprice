from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils.dateparse import parse_date

from apps.accounts.access import owner_required

from .models import AuditEvent, FieldCorrection


@owner_required
def audit_log(request):
    events = AuditEvent.objects.select_related("actor")
    day = request.GET.get("date")
    action = request.GET.get("action")
    if day:
        try:
            parsed_day = parse_date(day)
        except ValueError:
            parsed_day = None
        if parsed_day:
            events = events.filter(created_at__date=parsed_day)
        else:
            day = ""
    if action:
        events = events.filter(action=action)
    page = Paginator(events, 50).get_page(request.GET.get("page"))
    actions = AuditEvent.objects.order_by("action").values_list("action", flat=True).distinct()
    corrections = FieldCorrection.objects.select_related("document", "corrected_by")[:12]
    return render(
        request,
        "audit/log.html",
        {
            "page": page,
            "actions": actions,
            "corrections": corrections,
            "selected_date": day or "",
            "selected_action": action or "",
        },
    )
