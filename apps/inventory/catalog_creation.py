"""Owner-confirmed, idempotent creation of genuinely new Square catalog items."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.capture.models import SubmissionStatus
from apps.squareapi.client import get_client

from .boundaries import is_demo_inventory_request
from .catalog import refresh_catalog
from .matching import normalize_description, normalize_upc, normalize_vendor_sku
from .models import (
    CatalogCreationIntent,
    CatalogCreationStatus,
    DeliveryLine,
    DeliveryStatus,
    LineMatchStatus,
    SquareCatalogVariation,
)

CATALOG_CREATE_NAMESPACE = uuid.UUID("aad79c41-2d94-4db7-99a3-901418a3910f")
TERMINAL_DELIVERY_STATUSES = {
    DeliveryStatus.PUSHING,
    DeliveryStatus.PUSHED,
    DeliveryStatus.PUSHED_WITH_DRIFT,
    DeliveryStatus.PUSHED_UNVERIFIED,
}


class CatalogCreationError(RuntimeError):
    """A new catalog item could not be created safely."""


class CatalogWritesDisabled(CatalogCreationError):
    """The independent catalog-write gate is disabled."""


@dataclass(frozen=True)
class CatalogCreationResult:
    line_id: str
    item_id: str
    variation_id: str
    idempotency_key: str
    already_created: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def create_square_catalog_item(
    line: DeliveryLine,
    *,
    actor: object,
    item_name: str,
    variation_name: str,
    sku: str = "",
    upc: str = "",
    sale_price_cents: int | None,
    variable_price: bool,
    client: object | None = None,
) -> CatalogCreationResult:
    """Create, cache and select one Square item using a durable request identity."""

    if is_demo_inventory_request(line.delivery, actor=actor):
        raise CatalogWritesDisabled("Practice mode never creates products in Square.")
    if not getattr(settings, "SQUARE_CATALOG_WRITES_ENABLED", False):
        raise CatalogWritesDisabled(
            "Creating Square items is disabled. Enable it only after sandbox validation."
        )
    if not getattr(actor, "is_owner", False):
        raise CatalogCreationError("Only an owner can create a Square catalog item.")
    location_id = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    if not location_id:
        raise CatalogCreationError("SQUARE_LOCATION_ID is not configured.")

    item_name = normalize_description(item_name)
    variation_name = normalize_description(variation_name) or "Regular"
    sku = normalize_vendor_sku(sku)
    upc = normalize_upc(upc)
    if not item_name:
        raise CatalogCreationError("Enter the product name printed on the bottle or invoice.")
    if variable_price:
        sale_price_cents = None
    elif sale_price_cents is None or sale_price_cents < 0:
        raise CatalogCreationError("Enter the Square retail sale price or choose variable price.")

    payload = _catalog_payload(
        line=line,
        item_name=item_name,
        variation_name=variation_name,
        sku=sku,
        upc=upc,
        sale_price_cents=sale_price_cents,
        variable_price=variable_price,
        location_id=location_id,
    )
    payload_hash = _payload_hash(payload)
    square_client = client or get_client()

    existing = CatalogCreationIntent.objects.filter(line_id=line.pk).first()
    if existing is None:
        # A fresh, complete read prevents creating an item that already exists
        # elsewhere in this seller's Square catalog. The durable DB claim below
        # serializes competing requests made through this application.
        refresh_catalog(line.delivery, actor=actor, client=square_client)

    intent, already_created = _claim_creation(
        line=line,
        actor=actor,
        payload=payload,
        payload_hash=payload_hash,
        sku=sku,
        upc=upc,
    )
    if already_created:
        return _stored_result(intent, already_created=True)

    try:
        response = square_client.catalog.object.upsert(
            idempotency_key=intent.idempotency_key,
            object=payload,
        )
        errors = _value(response, "errors", None)
        if errors:
            raise CatalogCreationError("Square refused the new catalog item.")
        item_id, variation_id = _created_ids(response)
        if not item_id or not variation_id:
            raise CatalogCreationError(
                "Square returned no item variation ID; the protected request can be retried."
            )
    except Exception as exc:
        _mark_failed(intent, actor=actor, error=exc)
        raise

    result = _finalize_creation(
        intent,
        actor=actor,
        response=response,
        item_id=item_id,
        variation_id=variation_id,
        item_name=item_name,
        variation_name=variation_name,
        sku=sku,
        upc=upc,
        location_id=location_id,
    )
    line.refresh_from_db()
    return result


def _claim_creation(
    *,
    line: DeliveryLine,
    actor: object,
    payload: dict[str, object],
    payload_hash: str,
    sku: str,
    upc: str,
) -> tuple[CatalogCreationIntent, bool]:
    with transaction.atomic():
        locked_line = (
            DeliveryLine.objects.select_for_update()
            .select_related("delivery__submission__submitted_by")
            .get(pk=line.pk)
        )
        delivery = locked_line.delivery
        if delivery.submission.submitted_by.is_demo:
            raise CatalogWritesDisabled("Practice mode never creates products in Square.")
        if delivery.status in TERMINAL_DELIVERY_STATUSES or delivery.square_batch_keys:
            raise CatalogCreationError("A new item cannot be created after Square posting starts.")
        if delivery.submission.status == SubmissionStatus.REJECTED:
            raise CatalogCreationError("A rejected invoice cannot create a Square item.")
        if not locked_line.included or locked_line.match_status == LineMatchStatus.EXCLUDED:
            raise CatalogCreationError("This invoice line is excluded from inventory.")

        intent = CatalogCreationIntent.objects.select_for_update().filter(line=locked_line).first()
        if intent is not None:
            if intent.payload_hash != payload_hash:
                raise CatalogCreationError(
                    "This line already has a different protected creation request. "
                    "Resolve it before changing the new item."
                )
            if intent.status == CatalogCreationStatus.SUCCEEDED:
                return intent, True
            stale_seconds = max(
                60, int(getattr(settings, "SQUARE_CATALOG_CREATE_STALE_SECONDS", 900))
            )
            if (
                intent.status == CatalogCreationStatus.CLAIMED
                and intent.updated_at > timezone.now() - dt.timedelta(seconds=stale_seconds)
            ):
                raise CatalogCreationError(
                    "This protected Square item request is still in progress. Wait before retrying."
                )
            intent.status = CatalogCreationStatus.CLAIMED
            intent.error_message = ""
            intent.last_attempt_at = timezone.now()
            intent.save(
                update_fields=["status", "error_message", "last_attempt_at", "updated_at"]
            )
            _audit(
                actor,
                "inventory.square_catalog_create_resumed",
                intent,
                {"line_id": str(locked_line.pk), "payload_hash": payload_hash},
            )
            return intent, False

        _assert_no_cached_duplicate(sku=sku, upc=upc)
        key = str(uuid.uuid5(CATALOG_CREATE_NAMESPACE, f"line:{locked_line.pk}:catalog-item"))
        try:
            intent = CatalogCreationIntent.objects.create(
                line=locked_line,
                created_by=actor,
                status=CatalogCreationStatus.CLAIMED,
                idempotency_key=key,
                payload_hash=payload_hash,
                payload=payload,
                normalized_sku=sku.casefold(),
                normalized_upc=upc,
                last_attempt_at=timezone.now(),
            )
        except IntegrityError as exc:
            raise CatalogCreationError(
                "Another protected request already uses this SKU or barcode."
            ) from exc
        _audit(
            actor,
            "inventory.square_catalog_create_claimed",
            intent,
            {"line_id": str(locked_line.pk), "payload_hash": payload_hash},
        )
        return intent, False


def _finalize_creation(
    intent: CatalogCreationIntent,
    *,
    actor: object,
    response: object,
    item_id: str,
    variation_id: str,
    item_name: str,
    variation_name: str,
    sku: str,
    upc: str,
    location_id: str,
) -> CatalogCreationResult:
    with transaction.atomic():
        locked = CatalogCreationIntent.objects.select_for_update().select_related("line").get(
            pk=intent.pk
        )
        if locked.status == CatalogCreationStatus.SUCCEEDED:
            return _stored_result(locked, already_created=True)
        if locked.payload_hash != intent.payload_hash:
            raise CatalogCreationError("The protected Square item request changed unexpectedly.")

        variation, _ = SquareCatalogVariation.objects.update_or_create(
            variation_id=variation_id,
            defaults={
                "item_id": item_id,
                "item_name": item_name,
                "variation_name": variation_name,
                "sku": sku,
                "upc": upc,
                "gtin": upc if len(upc) == 14 else "",
                "track_inventory": True,
                "present_at_location": True,
                "location_id": location_id,
                "synced_at": timezone.now(),
            },
        )
        line = DeliveryLine.objects.select_for_update().get(pk=locked.line_id)
        line.square_catalog_variation_id = variation_id
        line.square_item_name = str(variation)
        line.match_status = LineMatchStatus.MATCHED
        line.square_count_variation_id = ""
        line.square_count_before = None
        line.projected_count_after = None
        line.square_count_after = None
        line.square_count_drift = None
        line.square_count_snapshot_at = None
        line.square_count_verified_at = None
        line.save()
        delivery = line.delivery
        delivery.spreadsheet_revision += 1
        delivery.status = DeliveryStatus.NEEDS_REVIEW
        delivery.save(update_fields=["spreadsheet_revision", "status", "updated_at"])

        locked.status = CatalogCreationStatus.SUCCEEDED
        locked.square_item_id = item_id
        locked.square_variation_id = variation_id
        locked.response = _json_safe(response)
        locked.error_message = ""
        locked.completed_at = timezone.now()
        locked.save(
            update_fields=[
                "status",
                "square_item_id",
                "square_variation_id",
                "response",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )
        result = _stored_result(locked, already_created=False)
        _audit(actor, "inventory.square_catalog_create_succeeded", locked, result.to_dict())
        return result


def _mark_failed(intent: CatalogCreationIntent, *, actor: object, error: Exception) -> None:
    with transaction.atomic():
        locked = CatalogCreationIntent.objects.select_for_update().get(pk=intent.pk)
        if locked.status == CatalogCreationStatus.SUCCEEDED:
            return
        locked.status = CatalogCreationStatus.FAILED
        locked.error_message = str(error)[:2000]
        locked.save(update_fields=["status", "error_message", "updated_at"])
        _audit(
            actor,
            "inventory.square_catalog_create_failed",
            locked,
            {"error": str(error)[:1000], "payload_hash": locked.payload_hash},
        )


def _assert_no_cached_duplicate(*, sku: str, upc: str) -> None:
    if sku and SquareCatalogVariation.objects.filter(sku__iexact=sku).exists():
        raise CatalogCreationError("That SKU already exists in Square. Choose the existing item.")
    if upc and SquareCatalogVariation.objects.filter(upc=upc).exists():
        raise CatalogCreationError("That barcode already exists in Square. Choose the existing item.")
    if upc and SquareCatalogVariation.objects.filter(gtin=upc).exists():
        raise CatalogCreationError("That barcode already exists in Square. Choose the existing item.")


def _catalog_payload(
    *,
    line: DeliveryLine,
    item_name: str,
    variation_name: str,
    sku: str,
    upc: str,
    sale_price_cents: int | None,
    variable_price: bool,
    location_id: str,
) -> dict[str, object]:
    variation_data: dict[str, object] = {
        "name": variation_name,
        "track_inventory": True,
        "pricing_type": "VARIABLE_PRICING" if variable_price else "FIXED_PRICING",
    }
    if sku:
        variation_data["sku"] = sku
    if upc:
        variation_data["upc"] = upc
    if not variable_price:
        variation_data["price_money"] = {
            "amount": sale_price_cents,
            "currency": getattr(settings, "SQUARE_CURRENCY", "USD"),
        }
    return {
        "type": "ITEM",
        "id": f"#item-{line.id.hex[:20]}",
        "present_at_all_locations": False,
        "present_at_location_ids": [location_id],
        "item_data": {
            "name": item_name,
            "variations": [
                {
                    "type": "ITEM_VARIATION",
                    "id": f"#variation-{line.id.hex[:20]}",
                    "present_at_all_locations": False,
                    "present_at_location_ids": [location_id],
                    "item_variation_data": variation_data,
                }
            ],
        },
    }


def _created_ids(response: object) -> tuple[str, str]:
    item_id = variation_id = ""
    for mapping in _value(response, "id_mappings", []) or []:
        client_id = str(_value(mapping, "client_object_id", "") or "")
        object_id = str(_value(mapping, "object_id", "") or "")
        if client_id.startswith("#item-"):
            item_id = object_id
        elif client_id.startswith("#variation-"):
            variation_id = object_id
    catalog_object = _value(response, "catalog_object", {}) or {}
    item_id = item_id or str(_value(catalog_object, "id", "") or "")
    item_data = _value(catalog_object, "item_data", {}) or {}
    variations = _value(item_data, "variations", []) or []
    if variations and not variation_id:
        variation_id = str(_value(variations[0], "id", "") or "")
    return item_id, variation_id


def _stored_result(
    intent: CatalogCreationIntent, *, already_created: bool
) -> CatalogCreationResult:
    return CatalogCreationResult(
        line_id=str(intent.line_id),
        item_id=intent.square_item_id,
        variation_id=intent.square_variation_id,
        idempotency_key=intent.idempotency_key,
        already_created=already_created,
    )


def _payload_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        payload = value
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json", exclude_none=True)
    elif hasattr(value, "dict"):
        payload = value.dict()
    else:
        payload = {"response": str(value)}
    return json.loads(json.dumps(payload, default=str))


def _value(value: object, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _audit(
    actor: object,
    action: str,
    intent: CatalogCreationIntent,
    detail: dict[str, object],
) -> None:
    AuditEvent.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        target_type=intent._meta.label_lower,
        target_id=str(intent.pk),
        detail=detail,
    )
