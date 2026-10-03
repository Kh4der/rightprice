"""Lifecycle for owner-reviewed Square selling-price updates."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.capture.models import SubmissionStatus

from .models import Delivery, DeliveryPricingPlan, PricingPlanStatus
from .pricing import (
    PricingIssue,
    PricingPreviewResult,
    build_frozen_pricing_payload,
    frozen_pricing_payload_hash,
    preview_delivery_pricing,
    pricing_idempotency_key,
)
from .pricing_gateway import CatalogPriceWriteResult, send_square_catalog_price_updates


class PricingPlanError(RuntimeError):
    """The requested pricing transition is unsafe or incomplete."""


@dataclass(frozen=True)
class PricingPreparationResult:
    plan: DeliveryPricingPlan
    preview: PricingPreviewResult
    blocking_issues: tuple[PricingIssue, ...]

    @property
    def can_push(self) -> bool:
        return bool(self.preview.updates) and not self.blocking_issues


# These are visible, intentional skips rather than corrupt or stale inputs.
_NON_BLOCKING_ISSUE_CODES = {"markup_rule_missing", "variable_pricing"}


def prepare_delivery_pricing(
    delivery: Delivery,
    *,
    actor: object,
    default_markup_percent: Decimal | None,
    category_rules: Mapping[str, object],
    product_overrides: Mapping[str, object],
    category_assignments: Mapping[str, object],
) -> PricingPreparationResult:
    """Save editable rules and freeze the exact preview the owner can approve."""

    if not getattr(actor, "is_owner", False):
        raise PricingPlanError("Only an owner can prepare Square selling prices.")

    with transaction.atomic():
        locked_delivery = Delivery.objects.select_for_update().get(pk=delivery.pk)
        plan, created = DeliveryPricingPlan.objects.select_for_update().get_or_create(
            delivery=locked_delivery
        )
        if plan.status in {PricingPlanStatus.PUSHING, PricingPlanStatus.PUSHED}:
            raise PricingPlanError("These Square prices are already being updated or were updated.")

        preview = preview_delivery_pricing(
            locked_delivery,
            default_markup_percent=default_markup_percent,
            category_rules=category_rules,
            product_overrides=product_overrides,
            category_assignments=category_assignments,
        )
        blocking = tuple(
            issue for issue in preview.issues if issue.code not in _NON_BLOCKING_ISSUE_CODES
        )
        next_revision = plan.revision if created else plan.revision + 1
        payload = build_frozen_pricing_payload(
            preview,
            revision=next_revision,
            delivery_revision=locked_delivery.spreadsheet_revision,
            location_id=str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip(),
        )
        payload_hash = frozen_pricing_payload_hash(payload)

        plan.default_markup_percent = default_markup_percent
        plan.category_rules = dict(category_rules)
        plan.product_overrides = dict(product_overrides)
        plan.category_assignments = dict(category_assignments)
        plan.preview_lines = [dict(line) for line in preview.preview_lines]
        plan.issues = [issue.to_dict() for issue in preview.issues]
        plan.revision = next_revision
        plan.square_result = {}
        plan.square_error = ""
        plan.pushed_by = None
        plan.pushed_at = None
        plan.prepared_by = actor
        plan.prepared_at = timezone.now()

        if preview.updates and not blocking:
            plan.status = PricingPlanStatus.PREVIEWED
            plan.frozen_payload = payload
            plan.frozen_payload_hash = payload_hash
            plan.idempotency_key = pricing_idempotency_key(
                delivery_id=str(locked_delivery.pk),
                revision=next_revision,
                payload_hash=payload_hash,
            )
        else:
            plan.status = PricingPlanStatus.DRAFT
            plan.frozen_payload = {}
            plan.frozen_payload_hash = ""
            plan.idempotency_key = ""
        plan.save()

    return PricingPreparationResult(plan=plan, preview=preview, blocking_issues=blocking)


def push_delivery_pricing(
    delivery: Delivery,
    *,
    actor: object,
    client: object | None = None,
) -> CatalogPriceWriteResult:
    """Claim, send and record one immutable catalog-price request."""

    if not getattr(actor, "is_owner", False):
        raise PricingPlanError("Only an owner can update Square selling prices.")

    with transaction.atomic():
        plan = (
            DeliveryPricingPlan.objects.select_for_update()
            .select_related("delivery__submission")
            .get(delivery_id=delivery.pk)
        )
        if plan.status == PricingPlanStatus.PUSHED:
            raise PricingPlanError("These Square prices were already updated.")
        if plan.delivery.submission.status != SubmissionStatus.APPROVED:
            raise PricingPlanError("Approve the invoice evidence before updating Square prices.")
        frozen_delivery_revision = plan.frozen_payload.get("delivery_revision")
        if frozen_delivery_revision != plan.delivery.spreadsheet_revision:
            raise PricingPlanError(
                "The invoice changed after this price preview. Preview the prices again."
            )
        if plan.status not in {
            PricingPlanStatus.PREVIEWED,
            PricingPlanStatus.FAILED,
            PricingPlanStatus.PUSHING,
        }:
            raise PricingPlanError("Preview the new selling prices before updating Square.")
        stale_seconds = max(
            60,
            int(getattr(settings, "SQUARE_CATALOG_PRICE_PUSH_STALE_SECONDS", 900)),
        )
        if (
            plan.status == PricingPlanStatus.PUSHING
            and plan.updated_at > timezone.now() - dt.timedelta(seconds=stale_seconds)
        ):
            raise PricingPlanError(
                "The Square price update is still running. Wait before trying it again."
            )
        if not plan.frozen_payload.get("updates"):
            raise PricingPlanError("This preview has no Square price changes.")
        plan.status = PricingPlanStatus.PUSHING
        plan.square_error = ""
        plan.save(update_fields=["status", "square_error", "updated_at"])

    try:
        result = send_square_catalog_price_updates(plan, actor=actor, client=client)
    except Exception as exc:
        DeliveryPricingPlan.objects.filter(pk=plan.pk).update(
            status=PricingPlanStatus.FAILED,
            square_error=str(exc)[:4000],
            updated_at=timezone.now(),
        )
        raise

    with transaction.atomic():
        locked = DeliveryPricingPlan.objects.select_for_update().get(pk=plan.pk)
        locked.status = PricingPlanStatus.PUSHED
        locked.square_result = result.to_dict()
        locked.square_error = ""
        locked.pushed_by = actor
        locked.pushed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "square_result",
                "square_error",
                "pushed_by",
                "pushed_at",
                "updated_at",
            ]
        )
    return result
