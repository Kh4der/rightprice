"""Read-only synchronization of the Square item-variation catalogue."""

from __future__ import annotations

import json
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
    objects = list(square_client.catalog.list(types="CATEGORY,ITEM,ITEM_VARIATION"))
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
    item_data_by_id: dict[str, object] = {}
    category_objects: dict[str, object] = {}
    nested_variations: dict[str, object] = {}
    top_level_variations: dict[str, object] = {}

    for catalog_object in objects:
        object_type = str(_value(catalog_object, "type", ""))
        if object_type == "CATEGORY":
            category_id = str(_value(catalog_object, "id", "") or "")
            if category_id:
                category_objects[category_id] = catalog_object
        elif object_type == "ITEM":
            item_id = str(_value(catalog_object, "id", "") or "")
            item_data = _value(catalog_object, "item_data", {})
            item_names[item_id] = normalize_description(_value(item_data, "name", ""))
            items[item_id] = catalog_object
            item_data_by_id[item_id] = item_data
            for nested in _value(item_data, "variations", []) or []:
                variation_id = str(_value(nested, "id", "") or "")
                if variation_id:
                    nested_variations[variation_id] = nested
        elif object_type == "ITEM_VARIATION":
            variation_id = str(_value(catalog_object, "id", "") or "")
            if variation_id:
                top_level_variations[variation_id] = catalog_object

    # Square can return variations both nested below their item and as
    # top-level objects. The top-level copy carries the authoritative object
    # version needed for a safe future upsert, so it wins regardless of list
    # order.
    variations = {**nested_variations, **top_level_variations}

    records: dict[str, dict[str, object]] = {}
    for variation_id, catalog_object in variations.items():
        data = _value(catalog_object, "item_variation_data", {})
        item_id = str(_value(data, "item_id", "") or "")
        item_data = item_data_by_id.get(item_id, {})
        identifier = normalize_upc(_value(data, "upc", ""))
        gtin = identifier if len(identifier) == 14 else ""
        vendor_costs = _vendor_costs(data)
        default_cost = vendor_costs[0] if vendor_costs else {}
        price_cents, price_currency, pricing_type, from_location_override = _effective_pricing(
            data, location_id=location_id
        )
        reporting_category_id, reporting_category_name, category_path = _item_categories(
            item_data,
            category_objects=category_objects,
        )
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
            "current_price_cents": price_cents,
            "current_price_currency": price_currency,
            "pricing_type": pricing_type,
            "catalog_version": _safe_integer(_value(catalog_object, "version", None)),
            "reporting_category_id": reporting_category_id,
            "reporting_category_name": reporting_category_name,
            "category_path": category_path,
            "price_from_location_override": from_location_override,
            "catalog_object_snapshot": _json_safe_object(catalog_object),
        }
    return records


def _effective_pricing(
    data: object,
    *,
    location_id: str,
) -> tuple[int | None, str, str, bool]:
    """Return the configured location's effective retail pricing fields."""

    money = _value(data, "price_money", None)
    pricing_type = _enum_text(_value(data, "pricing_type", ""))
    from_location_override = False

    for override in _value(data, "location_overrides", []) or []:
        if str(_value(override, "location_id", "") or "") != location_id:
            continue
        override_money = _value(override, "price_money", None)
        override_pricing_type = _value(override, "pricing_type", None)
        if override_money is not None:
            money = override_money
            from_location_override = True
        if override_pricing_type is not None:
            pricing_type = _enum_text(override_pricing_type)
        break

    amount = _safe_integer(_value(money, "amount", None))
    if amount is not None and amount < 0:
        amount = None
    currency = _enum_text(_value(money, "currency", "")).upper()[:3]
    return amount, currency, pricing_type, from_location_override


def _item_categories(
    item_data: object,
    *,
    category_objects: dict[str, object],
) -> tuple[str, str, list[str]]:
    """Resolve Square's canonical reporting category and readable hierarchy."""

    reporting_category = _value(item_data, "reporting_category", None)
    category_references = list(_value(item_data, "categories", []) or [])
    legacy_category_id = str(_value(item_data, "category_id", "") or "")

    canonical_reference = reporting_category
    if not _category_id(canonical_reference) and category_references:
        canonical_reference = category_references[0]
    canonical_id = _category_id(canonical_reference) or legacy_category_id
    canonical_name = _category_name(
        canonical_id,
        reference=canonical_reference,
        category_objects=category_objects,
    )
    category_path = _category_path(
        canonical_id,
        fallback_name=canonical_name,
        category_objects=category_objects,
    )

    # Keep every directly assigned Square category visible to the owner even
    # when it is not the reporting category. This also gives older accounts a
    # useful fallback while their catalog still relies on category_id.
    additional_references = [reporting_category, *category_references]
    if legacy_category_id:
        additional_references.append({"id": legacy_category_id})
    for reference in additional_references:
        category_id = _category_id(reference)
        name = _category_name(
            category_id,
            reference=reference,
            category_objects=category_objects,
        )
        if name and name not in category_path:
            category_path.append(name)

    return canonical_id, canonical_name, category_path


def _category_path(
    category_id: str,
    *,
    fallback_name: str,
    category_objects: dict[str, object],
) -> list[str]:
    if not category_id:
        return [fallback_name] if fallback_name else []
    category_object = category_objects.get(category_id)
    if category_object is None:
        return [fallback_name] if fallback_name else []
    category_data = _value(category_object, "category_data", {})
    current_name = normalize_description(_value(category_data, "name", "")) or fallback_name
    explicit_path = _value(category_data, "path_to_root", None)
    if explicit_path is not None:
        # Square orders this list parent -> root. Reverse it for the familiar
        # root -> leaf hierarchy displayed by this app.
        names = []
        for node in reversed(list(explicit_path or [])):
            node_id = str(_value(node, "category_id", "") or "")
            node_name = normalize_description(_value(node, "category_name", ""))
            if not node_name and node_id:
                node_name = _category_name(
                    node_id,
                    reference=None,
                    category_objects=category_objects,
                )
            if node_name and node_name not in names:
                names.append(node_name)
        if current_name and current_name not in names:
            names.append(current_name)
        return names

    # Some Square responses omit path_to_root. Reconstruct it from the parent
    # references in the CATEGORY objects, with cycle protection for bad data.
    names_reversed: list[str] = []
    seen: set[str] = set()
    cursor_id = category_id
    while cursor_id and cursor_id not in seen:
        seen.add(cursor_id)
        cursor = category_objects.get(cursor_id)
        if cursor is None:
            break
        cursor_data = _value(cursor, "category_data", {})
        cursor_name = normalize_description(_value(cursor_data, "name", ""))
        if cursor_name:
            names_reversed.append(cursor_name)
        cursor_id = _category_id(_value(cursor_data, "parent_category", None))
    names = list(reversed(names_reversed))
    if not names and fallback_name:
        names.append(fallback_name)
    return names


def _category_id(reference: object) -> str:
    return str(_value(reference, "id", "") or "") if reference is not None else ""


def _category_name(
    category_id: str,
    *,
    reference: object,
    category_objects: dict[str, object],
) -> str:
    reference_data = _value(reference, "category_data", {}) if reference is not None else {}
    name = normalize_description(_value(reference_data, "name", ""))
    if name:
        return name
    category_object = category_objects.get(category_id)
    category_data = _value(category_object, "category_data", {})
    return normalize_description(_value(category_data, "name", ""))


def _safe_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _json_safe_object(value: object) -> dict[str, object]:
    """Serialize every returned variation field for later lossless updates."""

    if isinstance(value, dict):
        payload: object = value
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    elif hasattr(value, "dict"):
        payload = value.dict(by_alias=True, exclude_none=False)
    else:
        payload = vars(value) if hasattr(value, "__dict__") else {"value": str(value)}
    normalized = json.loads(json.dumps(payload, default=str))
    return normalized if isinstance(normalized, dict) else {"value": normalized}


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
