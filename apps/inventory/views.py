"""Inventory review screens and explicit Square synchronization actions."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.access import owner_required
from apps.audit.models import FieldCorrection
from apps.audit.services import record_event
from apps.capture.models import SubmissionStatus

from .catalog import CatalogRefreshError
from .catalog_creation import (
    CatalogCreationError,
    CatalogWritesDisabled,
    create_square_catalog_item,
)
from .forms import (
    CatalogSearchForm,
    DeliveryHeaderForm,
    DeliveryLineForm,
    NewSquareItemForm,
    SquareMatchForm,
    SquarePushConfirmationForm,
)
from .matching import nearest_catalog_matches
from .models import (
    CatalogMapping,
    Delivery,
    DeliveryLine,
    DeliveryStatus,
    LineMatchStatus,
    SquareCatalogVariation,
    Vendor,
)
from .services import (
    DeliveryNotReady,
    InventoryWritesDisabled,
    export_delivery_xlsx,
    push_delivery_to_square,
    refresh_delivery_readiness,
    refresh_square_catalog,
    refresh_square_counts,
)

TERMINAL_DELIVERY_STATUSES = {
    DeliveryStatus.FAILED,
    DeliveryStatus.PUSHING,
    DeliveryStatus.PUSHED,
    DeliveryStatus.PUSHED_WITH_DRIFT,
    DeliveryStatus.PUSHED_UNVERIFIED,
}

FINALIZED_EXPORT_STATUSES = {
    DeliveryStatus.READY,
    DeliveryStatus.FAILED,
    DeliveryStatus.PUSHING,
    DeliveryStatus.PUSHED,
    DeliveryStatus.PUSHED_WITH_DRIFT,
    DeliveryStatus.PUSHED_UNVERIFIED,
}

LINE_AUDIT_FIELDS = (
    "description",
    "vendor_sku",
    "upc",
    "pack_text",
    "cases",
    "units_per_case",
    "received_units",
    "unit_cost_cents",
    "line_total_cents",
    "included",
    "review_note",
)


def _visible_delivery(request, pk) -> Delivery:
    queryset = Delivery.objects.select_related(
        "submission__submitted_by", "submission__approved_by", "vendor", "pushed_by"
    ).prefetch_related("lines")
    delivery = get_object_or_404(queryset, pk=pk)
    if not request.user.is_owner and delivery.submission.submitted_by_id != request.user.pk:
        raise Http404
    return delivery


def _editable_delivery(request, pk) -> Delivery:
    delivery = _visible_delivery(request, pk)
    if delivery.status in TERMINAL_DELIVERY_STATUSES or delivery.submission.is_terminal:
        raise PermissionDenied("This delivery is no longer editable.")
    return delivery


def _count_refreshable_delivery(request, pk) -> Delivery:
    """Allow a read-only count refresh after approval, but never after a write."""

    delivery = _visible_delivery(request, pk)
    if (
        delivery.status in TERMINAL_DELIVERY_STATUSES
        or delivery.submission.status == SubmissionStatus.REJECTED
    ):
        raise PermissionDenied("Square counts can no longer be refreshed for this delivery.")
    return delivery


@login_required
def delivery_detail(request, pk):
    delivery = _visible_delivery(request, pk)
    readiness = None
    if delivery.status not in TERMINAL_DELIVERY_STATUSES:
        readiness = refresh_delivery_readiness(delivery)
    lines = list(delivery.lines.order_by("position", "id"))
    grouped_deltas: dict[str, Decimal] = {}
    grouped_counts: dict[str, int] = {}
    for line in lines:
        variation_id = line.square_catalog_variation_id
        if (
            variation_id
            and line.included
            and line.match_status == LineMatchStatus.MATCHED
            and line.received_units is not None
        ):
            grouped_deltas[variation_id] = (
                grouped_deltas.get(variation_id, Decimal(0)) + line.received_units
            )
            grouped_counts[variation_id] = grouped_counts.get(variation_id, 0) + 1
    for line in lines:
        variation_id = line.square_catalog_variation_id
        line.comparison_delta = grouped_deltas.get(variation_id, line.received_units)
        line.comparison_line_count = grouped_counts.get(variation_id, 1)
    line_forms = [
        (line, DeliveryLineForm(instance=line, prefix=f"line-{line.pk}")) for line in lines
    ]
    catalog_count = SquareCatalogVariation.objects.filter(
        track_inventory=True, present_at_location=True
    ).count()
    latest_catalog_sync = (
        SquareCatalogVariation.objects.order_by("-synced_at")
        .values_list("synced_at", flat=True)
        .first()
    )
    latest_sandbox_job = delivery.sandbox_jobs.order_by("-created_at").first()
    push_stale_seconds = max(
        60,
        int(getattr(settings, "SQUARE_PUSH_STALE_SECONDS", 900)),
    )
    push_is_stale = bool(
        delivery.status == DeliveryStatus.PUSHING
        and delivery.square_batch_keys
        and delivery.updated_at <= timezone.now() - dt.timedelta(seconds=push_stale_seconds)
    )
    can_edit = bool(
        request.user.is_owner
        and delivery.status not in TERMINAL_DELIVERY_STATUSES
        and not delivery.submission.is_terminal
    )
    can_refresh_counts = bool(
        request.user.is_owner
        and delivery.status not in TERMINAL_DELIVERY_STATUSES
        and delivery.submission.status != SubmissionStatus.REJECTED
    )
    return render(
        request,
        "inventory/delivery_detail.html",
        {
            "delivery": delivery,
            "lines": lines,
            "line_forms": line_forms,
            "new_line_form": DeliveryLineForm(prefix="new"),
            "readiness": readiness,
            "catalog_count": catalog_count,
            "latest_catalog_sync": latest_catalog_sync,
            "latest_sandbox_job": latest_sandbox_job,
            "claude_sandbox_enabled": settings.CLAUDE_INVENTORY_SANDBOX_ENABLED,
            "header_form": DeliveryHeaderForm(instance=delivery),
            "push_form": SquarePushConfirmationForm(),
            "writes_enabled": settings.SQUARE_INVENTORY_WRITES_ENABLED,
            "push_is_stale": push_is_stale,
            "can_resume_push": bool(
                delivery.square_batch_keys
                and (delivery.status == DeliveryStatus.FAILED or push_is_stale)
            ),
            "show_post_panel": bool(delivery.status != DeliveryStatus.PUSHING or push_is_stale),
            "can_edit": can_edit,
            "can_refresh_counts": can_refresh_counts,
            "show_square_toolbar": can_edit or can_refresh_counts,
            "can_export_final_workbook": bool(
                request.user.is_owner
                and delivery.submission.status == SubmissionStatus.APPROVED
                and delivery.status in FINALIZED_EXPORT_STATUSES
            ),
        },
    )


@owner_required
@require_POST
def header_update(request, pk):
    delivery = _editable_delivery(request, pk)
    form = DeliveryHeaderForm(request.POST, instance=delivery)
    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("inventory:delivery-detail", pk=delivery.pk)

    before = {
        "vendor_name_raw": delivery.vendor_name_raw,
        "invoice_number": delivery.invoice_number,
        "invoice_date": _json_value(delivery.invoice_date),
        "invoice_total_cents": delivery.invoice_total_cents,
    }
    with transaction.atomic():
        delivery = form.save(commit=False)
        total = form.cleaned_data.get("invoice_total")
        delivery.invoice_total_cents = int(total * 100) if total is not None else None
        vendor_name = delivery.vendor_name_raw.strip()
        delivery.vendor = None
        if vendor_name:
            delivery.vendor, _ = Vendor.objects.get_or_create(name=vendor_name)
        delivery.spreadsheet_revision += 1
        delivery.save()
        readiness = refresh_delivery_readiness(delivery)
        after = {
            "vendor_name_raw": delivery.vendor_name_raw,
            "invoice_number": delivery.invoice_number,
            "invoice_date": _json_value(delivery.invoice_date),
            "invoice_total_cents": delivery.invoice_total_cents,
        }
        changes = {
            field: {"before": before[field], "after": after[field]}
            for field in before
            if before[field] != after[field]
        }
        record_event(
            request,
            "inventory.header_corrected",
            delivery,
            {"changes": changes},
        )
    if readiness.ready:
        messages.success(request, "Invoice details saved. The delivery is ready for approval.")
    else:
        messages.success(request, "Invoice details saved. Continue with the line review.")
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
@require_POST
def line_update(request, pk, line_pk):
    delivery = _editable_delivery(request, pk)
    line = get_object_or_404(DeliveryLine, delivery=delivery, pk=line_pk)
    form = DeliveryLineForm(request.POST, instance=line, prefix=f"line-{line.pk}")
    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("inventory:delivery-detail", pk=delivery.pk)

    before = _line_edit_snapshot(line)
    with transaction.atomic():
        line = form.save(commit=False)
        line.square_count_variation_id = ""
        for field in (
            "square_count_before",
            "projected_count_after",
            "square_count_after",
            "square_count_drift",
            "square_count_snapshot_at",
            "square_count_verified_at",
        ):
            setattr(line, field, None)
        line.save()
        delivery.spreadsheet_revision += 1
        delivery.save(update_fields=["spreadsheet_revision", "updated_at"])
        readiness = refresh_delivery_readiness(delivery)
        after = _line_edit_snapshot(line)
        changes = {
            field: {"before": before[field], "after": after[field]}
            for field in before
            if before[field] != after[field]
        }
        record_event(
            request,
            "inventory.line_corrected",
            line,
            {"changes": changes, "delivery_id": str(delivery.pk)},
        )
        _record_field_corrections(request, delivery, line, changes)
    if readiness.ready:
        messages.success(request, "Line saved. Refresh Square counts before approval.")
    else:
        messages.success(request, "Line saved. Remaining review items are shown below.")
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
@require_POST
def line_create(request, pk):
    delivery = _editable_delivery(request, pk)
    draft = DeliveryLine(delivery=delivery, position=1)
    form = DeliveryLineForm(request.POST, instance=draft, prefix="new")
    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("inventory:delivery-detail", pk=delivery.pk)

    with transaction.atomic():
        locked = Delivery.objects.select_for_update().get(pk=delivery.pk)
        next_position = (locked.lines.aggregate(value=Max("position"))["value"] or 0) + 1
        line = form.save(commit=False)
        line.delivery = locked
        line.position = next_position
        line.match_status = LineMatchStatus.UNMATCHED
        line.square_catalog_variation_id = ""
        line.square_item_name = ""
        line.save()
        locked.spreadsheet_revision += 1
        locked.save(update_fields=["spreadsheet_revision", "updated_at"])
        refresh_delivery_readiness(locked)
        record_event(
            request,
            "inventory.line_added_by_owner",
            line,
            {"delivery_id": str(locked.pk), "position": next_position},
        )
    messages.success(request, f"Invoice line {next_position} added. Match it to Square next.")
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
def export_delivery(request, pk):
    delivery = _visible_delivery(request, pk)
    if delivery.submission.status != SubmissionStatus.APPROVED:
        raise PermissionDenied(
            "Approve the reviewed invoice evidence before downloading the final workbook."
        )
    if delivery.status not in TERMINAL_DELIVERY_STATUSES:
        refresh_delivery_readiness(delivery)
    if delivery.status not in FINALIZED_EXPORT_STATUSES:
        raise PermissionDenied(
            "Finish every line match and capture current Square counts before downloading the final workbook."
        )
    content = export_delivery_xlsx(delivery)
    filename = f"delivery-{delivery.invoice_number or str(delivery.pk)[:8]}-final.xlsx"
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    record_event(request, "inventory.final_workbook_exported", delivery)
    return response


@owner_required
@require_POST
def sync_catalog(request, pk):
    delivery = _editable_delivery(request, pk)
    try:
        result = refresh_square_catalog(delivery, request.user)
        readiness = refresh_delivery_readiness(delivery)
    except Exception as exc:
        messages.error(request, _safe_square_error(exc, "Square catalog could not be refreshed."))
    else:
        record_event(request, "inventory.square_catalog_refreshed", delivery, result.to_dict())
        suffix = (
            " All invoice lines matched." if readiness.ready else " Review unmatched lines below."
        )
        messages.success(request, f"Pulled {result.seen} Square item variations.{suffix}")
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
def match_line(request, pk, line_pk):
    delivery = _editable_delivery(request, pk)
    line = get_object_or_404(DeliveryLine, delivery=delivery, pk=line_pk)
    search_form = CatalogSearchForm(request.GET or None)
    query = ""
    candidates = []
    if search_form.is_valid():
        query = search_form.cleaned_data["q"].strip()
        if query:
            digits = "".join(character for character in query if character.isdigit())
            search = (
                Q(item_name__icontains=query)
                | Q(variation_name__icontains=query)
                | Q(sku__iexact=query)
            )
            if digits:
                search |= Q(upc__iexact=digits) | Q(gtin__iexact=digits)
            candidates = list(
                SquareCatalogVariation.objects.filter(
                    track_inventory=True,
                    present_at_location=True,
                ).filter(search)[:30]
            )
            for candidate in candidates:
                candidate.suggestion_reason = "Square catalog search result"
                candidate.suggestion_score = None
        else:
            candidates = nearest_catalog_matches(line)
    vendor_id = delivery.vendor.square_vendor_id if delivery.vendor_id else ""
    for candidate in candidates:
        amount, currency, source = candidate.cost_snapshot_for_vendor(vendor_id)
        candidate.cost_preview_cents = amount
        candidate.cost_preview_currency = currency
        candidate.cost_preview_source = source
    return render(
        request,
        "inventory/match_line.html",
        {
            "delivery": delivery,
            "line": line,
            "search_form": search_form,
            "query": query,
            "candidates": candidates,
            "showing_suggestions": not query,
            "new_item_form": NewSquareItemForm(
                initial={
                    "item_name": line.description,
                    "variation_name": line.pack_text or "Regular",
                    "sku": line.vendor_sku,
                    "upc": line.upc,
                }
            ),
            "catalog_writes_enabled": settings.SQUARE_CATALOG_WRITES_ENABLED,
        },
    )


@owner_required
@require_POST
def choose_match(request, pk, line_pk):
    delivery = _editable_delivery(request, pk)
    line = get_object_or_404(DeliveryLine, delivery=delivery, pk=line_pk)
    form = SquareMatchForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a valid Square item.")
        return redirect("inventory:match-line", pk=delivery.pk, line_pk=line.pk)
    variation = get_object_or_404(
        SquareCatalogVariation,
        variation_id=form.cleaned_data["variation_id"],
        track_inventory=True,
        present_at_location=True,
    )
    before = line.square_catalog_variation_id
    with transaction.atomic():
        line.square_catalog_variation_id = variation.variation_id
        line.square_item_name = str(variation)
        line.match_status = LineMatchStatus.MATCHED
        (
            line.square_unit_cost_cents,
            line.square_unit_cost_currency,
            line.square_unit_cost_source,
        ) = variation.cost_snapshot_for_vendor(
            delivery.vendor.square_vendor_id if delivery.vendor_id else ""
        )
        line.square_count_variation_id = ""
        line.square_count_before = None
        line.projected_count_after = None
        line.square_count_after = None
        line.square_count_drift = None
        line.square_count_snapshot_at = None
        line.square_count_verified_at = None
        line.save()
        if (
            form.cleaned_data["remember_mapping"]
            and delivery.vendor_id
            and line.vendor_sku
            and line.units_per_case
        ):
            CatalogMapping.objects.update_or_create(
                vendor=delivery.vendor,
                vendor_sku=line.vendor_sku,
                defaults={
                    "upc": line.upc,
                    "description": line.description,
                    "square_catalog_variation_id": variation.variation_id,
                    "square_item_name": str(variation),
                    "units_per_case": line.units_per_case,
                    "last_verified_at": timezone.now(),
                    "verified_by": request.user,
                },
            )
        delivery.spreadsheet_revision += 1
        delivery.save(update_fields=["spreadsheet_revision", "updated_at"])
        refresh_delivery_readiness(delivery)
        record_event(
            request,
            "inventory.square_item_matched",
            line,
            {
                "delivery_id": str(delivery.pk),
                "before": before,
                "after": variation.variation_id,
                "remembered": form.cleaned_data["remember_mapping"],
            },
        )
    messages.success(request, f"Matched line {line.position} to {variation}.")
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
@require_POST
def create_catalog_item(request, pk, line_pk):
    delivery = _editable_delivery(request, pk)
    line = get_object_or_404(DeliveryLine, delivery=delivery, pk=line_pk)
    form = NewSquareItemForm(request.POST)
    if not form.is_valid():
        messages.error(request, _form_error_message(form))
        return redirect("inventory:match-line", pk=delivery.pk, line_pk=line.pk)
    sale_price = form.cleaned_data.get("sale_price")
    try:
        result = create_square_catalog_item(
            line,
            actor=request.user,
            item_name=form.cleaned_data["item_name"],
            variation_name=form.cleaned_data["variation_name"],
            sku=form.cleaned_data["sku"],
            upc=form.cleaned_data["upc"],
            sale_price_cents=int(sale_price * 100) if sale_price is not None else None,
            variable_price=form.cleaned_data["variable_price"],
        )
    except (CatalogCreationError, CatalogWritesDisabled) as exc:
        messages.error(request, str(exc))
        return redirect("inventory:match-line", pk=delivery.pk, line_pk=line.pk)
    except Exception as exc:
        messages.error(request, _safe_square_error(exc, "The Square item could not be created."))
        return redirect("inventory:match-line", pk=delivery.pk, line_pk=line.pk)

    if result.already_created:
        messages.success(request, "The protected Square item request was already completed.")
    else:
        messages.success(
            request,
            "New Square item created and matched. Compare live stock before updating inventory.",
        )
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
@require_POST
def refresh_counts(request, pk):
    delivery = _count_refreshable_delivery(request, pk)
    try:
        result = refresh_square_counts(delivery, actor=request.user)
    except Exception as exc:
        messages.error(request, _safe_square_error(exc, "Current Square counts could not be read."))
    else:
        messages.success(
            request,
            f"Current Square stock captured for {len(result.snapshots)} reviewed line(s).",
        )
    return redirect("inventory:delivery-detail", pk=delivery.pk)


@owner_required
@require_POST
def push_delivery(request, pk):
    delivery = _visible_delivery(request, pk)
    if delivery.submission.status != SubmissionStatus.APPROVED:
        messages.error(request, "Approve the invoice evidence before updating inventory in Square.")
        return redirect("inventory:delivery-detail", pk=delivery.pk)
    form = SquarePushConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Confirm the reviewed matches and projected counts first.")
        return redirect("inventory:delivery-detail", pk=delivery.pk)
    try:
        result = push_delivery_to_square(delivery, actor=request.user)
    except DeliveryNotReady as exc:
        record_event(
            request,
            "inventory.square_push_blocked",
            delivery,
            {"reason": str(exc)[:1000]},
        )
        messages.error(request, str(exc))
    except InventoryWritesDisabled as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, _safe_square_error(exc, "Square inventory could not be posted."))
    else:
        if result.verified:
            messages.success(
                request, "Inventory posted and the resulting Square counts were verified."
            )
        elif result.drift_lines:
            messages.warning(
                request,
                "Inventory posted, but at least one live Square count changed from the projection. Review the drift below.",
            )
        else:
            messages.warning(
                request,
                "Inventory posted, but Square count verification was unavailable. Do not post it again.",
            )
    return redirect("inventory:delivery-detail", pk=delivery.pk)


def _record_field_corrections(request, delivery, line, changes) -> None:
    document = delivery.submission.documents.order_by("created_at").first()
    if document is None:
        return
    FieldCorrection.objects.bulk_create(
        [
            FieldCorrection(
                document=document,
                field_path=f"delivery.lines[{line.position}].{field}",
                previous_value=values["before"],
                corrected_value=values["after"],
                reason="Invoice review correction",
                corrected_by=request.user,
            )
            for field, values in changes.items()
        ]
    )


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _line_edit_snapshot(line: DeliveryLine) -> dict[str, object]:
    return {field: _json_value(getattr(line, field)) for field in LINE_AUDIT_FIELDS}


def _form_error_message(form) -> str:
    errors = []
    for field, field_errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else "Form"
        errors.extend(f"{label}: {error}" for error in field_errors)
    return " ".join(errors)[:1000] or "Review the highlighted fields and try again."


def _safe_square_error(exc: Exception, fallback: str) -> str:
    if isinstance(exc, (CatalogRefreshError, DeliveryNotReady, InventoryWritesDisabled)):
        return str(exc)
    return f"{fallback} Check the Square connection and try again."
