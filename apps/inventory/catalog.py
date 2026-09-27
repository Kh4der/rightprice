"""Read-only synchronization of the Square item-variation catalogue."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.squareapi.client import get_client

from .boundaries import is_demo_inventory_request
from .matching import normalize_description, normalize_upc, normalize_vendor_sku
from .models import Delivery, SquareCatalogVariation


class CatalogRefreshError(RuntimeError):
    """Square catalogue data was unavailable or structurally incomplete."""


@dataclass(frozen=True)
class CatalogRefreshResult:
    location_id: str
    seen: int
    created: int
    updated: int
    unavailable: int
    synced_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def refresh_catalog(
    delivery: Delivery,
    *,
    actor: object,
    client: object | None = None,
) -> CatalogRefreshResult:
    """Refresh the local cache using Square's read-only Catalog list API."""

    if is_demo_inventory_request(delivery, actor=actor):
        raise CatalogRefreshError("Practice mode never reads the live Square catalog.")
    location_id = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    if not location_id:
        raise CatalogRefreshError("SQUARE_LOCATION_ID is not configured.")
    square_client = client or get_client()
    objects = list(square_client.catalog.list(types="ITEM,ITEM_VARIATION"))
    records = _variation_records(objects, location_id=location_id)
    synced_at = timezone.now()
    created = updated = 0

    with transaction.atomic():
        # A full list is authoritative for the configured location. Keep missing
        # rows for audit/search history, but make them ineligible for matching.
        SquareCatalogVariation.objects.exclude(variation_id__in=records).update(
            present_at_location=False,
            synced_at=synced_at,
        )
        for variation_id, values in records.items():
            _, was_created = SquareCatalogVariation.objects.update_or_create(
                variation_id=variation_id,
                defaults={**values, "synced_at": synced_at},
            )
            if was_created:
                created += 1
            else:
                updated += 1

    unavailable = sum(
        1
        for values in records.values()
        if not values["present_at_location"] or not values["track_inventory"]
    )
    return CatalogRefreshResult(
        location_id=location_id,
        seen=len(records),
        created=created,
        updated=updated,
        unavailable=unavailable,
        synced_at=synced_at.isoformat(),
    )


def _variation_records(
    objects: Iterable[object],
    *,
    location_id: str,
) -> dict[str, dict[str, object]]:
    objects = list(objects)
    item_names: dict[str, str] = {}
    items: dict[str, object] = {}
    variations: dict[str, object] = {}

    for catalog_object in objects:
        object_type = str(_value(catalog_object, "type", ""))
        if object_type == "ITEM":
            item_id = str(_value(catalog_object, "id", "") or "")
            item_data = _value(catalog_object, "item_data", {})
            item_names[item_id] = normalize_description(_value(item_data, "name", ""))
            items[item_id] = catalog_object
            for nested in _value(item_data, "variations", []) or []:
                variation_id = str(_value(nested, "id", "") or "")
                if variation_id:
                    variations[variation_id] = nested
        elif object_type == "ITEM_VARIATION":
            variation_id = str(_value(catalog_object, "id", "") or "")
            if variation_id:
                variations[variation_id] = catalog_object

    records: dict[str, dict[str, object]] = {}
    for variation_id, catalog_object in variations.items():
        data = _value(catalog_object, "item_variation_data", {})
        item_id = str(_value(data, "item_id", "") or "")
        identifier = normalize_upc(_value(data, "upc", ""))
        gtin = identifier if len(identifier) == 14 else ""
        vendor_costs = _vendor_costs(data)
        default_cost = vendor_costs[0] if vendor_costs else {}
        records[variation_id] = {
            "item_id": item_id,
            "item_name": item_names.get(item_id, ""),
            "variation_name": normalize_description(_value(data, "name", "")),
            "sku": normalize_vendor_sku(_value(data, "sku", "")),
            "upc": identifier,
            "gtin": gtin,
            "track_inventory": _tracks_inventory(data, location_id=location_id),
            "present_at_location": (
                _present_at_location(catalog_object, location_id=location_id)
                and _present_at_location(items[item_id], location_id=location_id)
                if item_id in items
                else _present_at_location(catalog_object, location_id=location_id)
            ),
            "location_id": location_id,
            "default_unit_cost_cents": default_cost.get("amount"),
            "default_unit_cost_currency": default_cost.get("currency", ""),
            "default_unit_cost_vendor_id": default_cost.get("vendor_id", ""),
            "vendor_costs": vendor_costs,
        }
    return records


def _vendor_costs(data: object) -> list[dict[str, object]]:
    """Normalize Square's ordered vendor-information list for safe local use."""

    result: list[dict[str, object]] = []
    for information in _value(data, "vendor_information", []) or []:
        money = _value(information, "unit_cost_money", {}) or {}
        raw_amount = _value(money, "amount", None)
        try:
            amount = (
                int(raw_amount)
                if raw_amount is not None and not isinstance(raw_amount, bool)
                else None
            )
        except (TypeError, ValueError):
            amount = None
        if amount is not None and amount < 0:
            amount = None
        currency = _value(money, "currency", "") or ""
        currency_value = getattr(currency, "value", currency)
        result.append(
            {
                "vendor_id": str(_value(information, "vendor_id", "") or ""),
                "vendor_code": str(_value(information, "vendor_code", "") or ""),
                "amount": amount,
                "currency": str(currency_value).upper()[:3],
            }
        )
    return result


def _present_at_location(catalog_object: object, *, location_id: str) -> bool:
    # Square defines this field's default as true. The location lists have
    # different meanings in the two modes and the unused list must be ignored.
    global_mode = _value(catalog_object, "present_at_all_locations", None)
    if global_mode is None:
        global_mode = True
    if bool(global_mode):
        absent = set(_value(catalog_object, "absent_at_location_ids", []) or [])
        return location_id not in absent
    present = set(_value(catalog_object, "present_at_location_ids", []) or [])
    return location_id in present


def _tracks_inventory(data: object, *, location_id: str) -> bool:
    result = bool(_value(data, "track_inventory", False))
    for override in _value(data, "location_overrides", []) or []:
        if _value(override, "location_id", None) != location_id:
            continue
        override_value = _value(override, "track_inventory", None)
        if override_value is not None:
            result = bool(override_value)
    return result


def _value(value: object, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)
