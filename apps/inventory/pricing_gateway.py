"""Guarded, drift-aware Square Catalog retail-price writes.

The pricing-plan service owns database lifecycle and audit persistence.  This
module deliberately does not change plan status: it fetches the current full
Square variation objects, proves they still match the approved preview, builds
one atomic full-object replacement, and sends it behind an independent gate.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import asdict, dataclass
from typing import Any

from django.conf import settings

from apps.squareapi.client import get_client

from .boundaries import is_demo_inventory_request
from .models import PricingPlanStatus
from .pricing import frozen_pricing_payload_hash

MAX_CATALOG_PRICE_OBJECTS = 1000
FIXED_PRICING = "FIXED_PRICING"
GLOBAL_PRICE_SCOPE = "GLOBAL"
LOCATION_OVERRIDE_PRICE_SCOPE = "LOCATION_OVERRIDE"


class CatalogPriceWriteError(RuntimeError):
    """Base class for a retail-price request that must not be sent."""


class CatalogPriceWritesDisabled(CatalogPriceWriteError):
    """The independent price-write gate or practice boundary stopped a write."""


class PricingPlanNotReady(CatalogPriceWriteError):
    """The saved plan is not an immutable, approved Square request."""


class PricingPlanDrift(CatalogPriceWriteError):
    """Square changed after preview, so the owner must review a fresh preview."""

    def __init__(self, message: str, *, details: tuple[dict[str, object], ...] = ()):
        super().__init__(message)
        self.details = details

    def to_dict(self) -> dict[str, object]:
        return {"message": str(self), "details": list(self.details)}


@dataclass(frozen=True)
class CatalogPriceWriteRequest:
    plan_id: str
    location_id: str
    idempotency_key: str
    objects: tuple[dict[str, object], ...]
    variation_ids: tuple[str, ...]

    @property
    def payload(self) -> dict[str, object]:
        """Return the exact body shape sent to Square's atomic batch endpoint."""

        return {"batches": [{"objects": list(self.objects)}]}

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "location_id": self.location_id,
            "idempotency_key": self.idempotency_key,
            "variation_ids": list(self.variation_ids),
            "batches": self.payload["batches"],
        }


@dataclass(frozen=True)
class CatalogPriceWriteResult:
    plan_id: str
    updated_count: int
    variation_ids: tuple[str, ...]
    idempotency_key: str
    response: dict[str, object]
    already_applied: bool = False

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["variation_ids"] = list(self.variation_ids)
        return result


def send_square_catalog_price_updates(
    plan: object,
    *,
    actor: object,
    client: object | None = None,
) -> CatalogPriceWriteResult:
    """Validate, re-read, compare and atomically send one approved price plan.

    The caller persists success/failure status.  Keeping that concern outside
    this function makes a retry use the plan's same durable idempotency key and
    lets the service record ambiguous network outcomes without this low-level
    transport guessing what happened.
    """

    if not getattr(actor, "is_owner", False):
        raise PricingPlanNotReady("Only an owner can update Square retail prices.")

    delivery = getattr(plan, "delivery", None)
    if delivery is None:
        raise PricingPlanNotReady("The price plan is not attached to a delivery.")
    if is_demo_inventory_request(delivery, actor=actor):
        raise CatalogPriceWritesDisabled("Practice mode never changes Square retail prices.")
    if not getattr(settings, "SQUARE_CATALOG_PRICE_WRITES_ENABLED", False):
        raise CatalogPriceWritesDisabled(
            "Square retail price writes are disabled. Enable the separate "
            "SQUARE_CATALOG_PRICE_WRITES_ENABLED gate only after sandbox validation."
        )

    updates = _approved_updates(plan)
    location_id = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    if not location_id:
        raise PricingPlanNotReady("SQUARE_LOCATION_ID is not configured.")

    variation_ids = tuple(str(entry["variation_id"]) for entry in updates)
    square_client = client or get_client()
    response = square_client.catalog.batch_get(
        object_ids=list(variation_ids),
        include_related_objects=False,
    )
    live_objects = _response_objects(response)
    request_or_result = build_catalog_price_write(
        plan,
        live_objects=live_objects,
        location_id=location_id,
    )
    if isinstance(request_or_result, CatalogPriceWriteResult):
        return request_or_result

    write_response = square_client.catalog.batch_upsert(
        idempotency_key=request_or_result.idempotency_key,
        **request_or_result.payload,
    )
    if _value(write_response, "errors", None):
        raise CatalogPriceWriteError("Square refused the approved retail-price update.")

    return CatalogPriceWriteResult(
        plan_id=request_or_result.plan_id,
        updated_count=len(request_or_result.objects),
        variation_ids=request_or_result.variation_ids,
        idempotency_key=request_or_result.idempotency_key,
        response=_json_response(write_response),
    )


def build_catalog_price_write(
    plan: object,
    *,
    live_objects: object,
    location_id: str | None = None,
) -> CatalogPriceWriteRequest | CatalogPriceWriteResult:
    """Build a full-object replacement after comparing Square to the preview.

    This is the pure part of the gateway: it does no network I/O and mutates
    neither the supplied Square objects nor the plan.
    """

    updates = _approved_updates(plan)
    resolved_location_id = str(
        location_id or getattr(settings, "SQUARE_LOCATION_ID", "") or ""
    ).strip()
    if not resolved_location_id:
        raise PricingPlanNotReady("SQUARE_LOCATION_ID is not configured.")

    idempotency_key = str(getattr(plan, "idempotency_key", "") or "").strip()
    if not idempotency_key:
        raise PricingPlanNotReady("The approved price plan has no saved idempotency key.")
    if len(idempotency_key) > 128:
        raise PricingPlanNotReady("The saved Square idempotency key is too long.")

    live_by_id: dict[str, dict[str, object]] = {}
    duplicates: set[str] = set()
    for live_object in _coerce_object_list(live_objects):
        plain = _plain_dict(live_object)
        variation_id = str(plain.get("id") or "")
        if not variation_id:
            continue
        if variation_id in live_by_id:
            duplicates.add(variation_id)
        live_by_id[variation_id] = plain

    if duplicates:
        raise PricingPlanDrift(
            "Square returned duplicate catalog objects; create a new preview.",
            details=tuple(
                {"variation_id": variation_id, "reason": "duplicate_live_object"}
                for variation_id in sorted(duplicates)
            ),
        )

    states: list[str] = []
    replacement_objects: list[dict[str, object]] = []
    drift: list[dict[str, object]] = []

    for entry in updates:
        variation_id = str(entry["variation_id"])
        live = live_by_id.get(variation_id)
        if live is None:
            drift.append({"variation_id": variation_id, "reason": "missing_from_square"})
            continue

        try:
            live_state = _effective_price_state(
                live,
                location_id=resolved_location_id,
            )
        except PricingPlanDrift as exc:
            drift.extend(exc.details)
            continue

        target_price = _required_nonnegative_int(entry, "target_price_cents")
        snapshot_version = _required_nonnegative_int(entry, "snapshot_version")
        snapshot_price = _required_nonnegative_int(entry, "snapshot_price_cents")
        snapshot_type = str(entry.get("snapshot_pricing_type") or "")
        snapshot_scope = str(entry.get("snapshot_price_scope") or "")

        if snapshot_type != FIXED_PRICING:
            raise PricingPlanNotReady(f"{variation_id} was not previewed with fixed pricing.")
        if snapshot_scope not in {GLOBAL_PRICE_SCOPE, LOCATION_OVERRIDE_PRICE_SCOPE}:
            raise PricingPlanNotReady(f"{variation_id} has an invalid preview price scope.")

        if (
            live_state["price_cents"] == target_price
            and live_state["pricing_type"] == FIXED_PRICING
            and live_state["scope"] == LOCATION_OVERRIDE_PRICE_SCOPE
        ):
            states.append("TARGET")
            replacement_objects.append(live)
            continue

        if (
            live_state["version"] != snapshot_version
            or live_state["price_cents"] != snapshot_price
            or live_state["pricing_type"] != snapshot_type
            or live_state["scope"] != snapshot_scope
        ):
            drift.append(
                {
                    "variation_id": variation_id,
                    "reason": "changed_since_preview",
                    "preview": {
                        "version": snapshot_version,
                        "price_cents": snapshot_price,
                        "pricing_type": snapshot_type,
                        "scope": snapshot_scope,
                    },
                    "current": live_state,
                }
            )
            continue

        states.append("SNAPSHOT")
        replacement_objects.append(
            _with_target_price(
                live,
                target_price_cents=target_price,
                location_id=resolved_location_id,
            )
        )

    if drift:
        raise PricingPlanDrift(
            "Square catalog data changed after this preview. Review a fresh preview before updating.",
            details=tuple(drift),
        )

    plan_id = str(getattr(plan, "pk", None) or getattr(plan, "id", ""))
    variation_ids = tuple(str(entry["variation_id"]) for entry in updates)
    if states and all(state == "TARGET" for state in states):
        return CatalogPriceWriteResult(
            plan_id=plan_id,
            updated_count=0,
            variation_ids=variation_ids,
            idempotency_key=idempotency_key,
            response={},
            already_applied=True,
        )

    # A Square catalog batch is atomic. Seeing only part of that exact batch at
    # target means an outside writer intervened (or the approved payload is no
    # longer the one represented by this preview), so never manufacture a
    # different retry body under the saved idempotency key.
    if any(state == "TARGET" for state in states):
        raise PricingPlanDrift(
            "Only part of this atomic price plan is already applied. Review a fresh preview.",
            details=tuple(
                {
                    "variation_id": variation_id,
                    "reason": "partial_target_state",
                    "state": state.lower(),
                }
                for variation_id, state in zip(variation_ids, states, strict=True)
            ),
        )

    return CatalogPriceWriteRequest(
        plan_id=plan_id,
        location_id=resolved_location_id,
        idempotency_key=idempotency_key,
        objects=tuple(replacement_objects),
        variation_ids=variation_ids,
    )


def _approved_updates(plan: object) -> tuple[dict[str, object], ...]:
    allowed_statuses = {
        PricingPlanStatus.PREVIEWED,
        PricingPlanStatus.PUSHING,
        PricingPlanStatus.FAILED,
        PricingPlanStatus.PUSHED,
    }
    if str(getattr(plan, "status", "")) not in allowed_statuses:
        raise PricingPlanNotReady("Only an approved frozen price plan can update Square.")

    frozen_payload = getattr(plan, "frozen_payload", None)
    if not isinstance(frozen_payload, dict):
        raise PricingPlanNotReady("The approved price plan has no frozen payload.")
    if frozen_payload.get("schema_version") != 1:
        raise PricingPlanNotReady("The approved price plan uses an unsupported payload version.")

    delivery = getattr(plan, "delivery", None)
    delivery_id = str(getattr(delivery, "pk", None) or getattr(delivery, "id", ""))
    if str(frozen_payload.get("delivery_id") or "") != delivery_id:
        raise PricingPlanNotReady("The frozen price plan belongs to a different delivery.")
    plan_revision = getattr(plan, "revision", None)
    if (
        isinstance(plan_revision, bool)
        or not isinstance(plan_revision, int)
        or frozen_payload.get("plan_revision") != plan_revision
    ):
        raise PricingPlanNotReady("The price plan changed after this payload was approved.")

    delivery_revision = _persisted_delivery_revision(delivery)
    if frozen_payload.get("delivery_revision") != delivery_revision:
        raise PricingPlanNotReady(
            "The invoice changed after this price preview. Create and approve a new preview."
        )

    frozen_location = str(frozen_payload.get("location_id") or "").strip()
    configured_location = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    if not frozen_location or frozen_location != configured_location:
        raise PricingPlanNotReady(
            "The Square store location changed after this price preview. Create a new preview."
        )

    expected_hash = str(getattr(plan, "frozen_payload_hash", "") or "").strip()
    if not expected_hash or expected_hash != frozen_pricing_payload_hash(frozen_payload):
        raise PricingPlanNotReady("The frozen price payload failed its integrity check.")

    raw_updates = frozen_payload.get("updates")
    if not isinstance(raw_updates, list):
        raise PricingPlanNotReady("The approved price plan has no frozen update entries.")

    updates: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_entry in raw_updates:
        if not isinstance(raw_entry, dict):
            raise PricingPlanNotReady("The frozen price plan contains an invalid update entry.")
        if raw_entry.get("price_changed") is not True:
            continue
        variation_id = str(raw_entry.get("variation_id") or "").strip()
        if not variation_id:
            raise PricingPlanNotReady("A frozen price update is missing its Square variation ID.")
        if variation_id in seen:
            raise PricingPlanNotReady(
                f"The frozen price plan repeats Square variation {variation_id}."
            )
        seen.add(variation_id)
        updates.append(raw_entry)

    if not updates:
        raise PricingPlanNotReady("The approved price plan has no changed prices to update.")
    if len(updates) > MAX_CATALOG_PRICE_OBJECTS:
        raise PricingPlanNotReady(
            f"A price plan can update at most {MAX_CATALOG_PRICE_OBJECTS} products at once."
        )
    return tuple(updates)


def _persisted_delivery_revision(delivery: object) -> int | None:
    delivery_id = getattr(delivery, "pk", None)
    manager = getattr(delivery.__class__, "objects", None)
    if delivery_id is None or manager is None:
        raise PricingPlanNotReady("The price plan delivery is not saved.")
    revision = manager.filter(pk=delivery_id).values_list("spreadsheet_revision", flat=True).first()
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise PricingPlanNotReady("The price plan delivery could not be verified.")
    return revision


def _effective_price_state(
    catalog_object: dict[str, object],
    *,
    location_id: str,
) -> dict[str, object]:
    variation_id = str(catalog_object.get("id") or "")
    if str(catalog_object.get("type") or "") != "ITEM_VARIATION":
        raise PricingPlanDrift(
            "Square returned a non-variation catalog object.",
            details=({"variation_id": variation_id, "reason": "wrong_catalog_object_type"},),
        )
    if catalog_object.get("is_deleted") is True:
        raise PricingPlanDrift(
            "A Square item variation was deleted after this preview.",
            details=({"variation_id": variation_id, "reason": "deleted_catalog_object"},),
        )
    data = catalog_object.get("item_variation_data")
    if not isinstance(data, dict):
        raise PricingPlanDrift(
            "Square returned an incomplete item variation.",
            details=({"variation_id": variation_id, "reason": "missing_item_variation_data"},),
        )

    pricing_type = str(data.get("pricing_type") or "")
    price_money = data.get("price_money")
    scope = GLOBAL_PRICE_SCOPE
    override = _location_override(data, location_id=location_id)
    if override is not None:
        if override.get("pricing_type") is not None:
            pricing_type = str(override.get("pricing_type") or "")
        if override.get("price_money") is not None:
            price_money = override.get("price_money")
            scope = LOCATION_OVERRIDE_PRICE_SCOPE

    amount = price_money.get("amount") if isinstance(price_money, dict) else None
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        amount = None
    version = catalog_object.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        version = None
    return {
        "version": version,
        "price_cents": amount,
        "pricing_type": pricing_type,
        "scope": scope,
    }


def _with_target_price(
    catalog_object: dict[str, object],
    *,
    target_price_cents: int,
    location_id: str,
) -> dict[str, object]:
    """Set only the configured store's override, never the merchant-wide price."""

    replacement = copy.deepcopy(catalog_object)
    data = replacement["item_variation_data"]
    override = _location_override(data, location_id=location_id)
    money = override.get("price_money") if override is not None else None
    if not isinstance(money, dict):
        # A variation that currently inherits its merchant-wide price needs a
        # new location price. Copy the complete Money value so its currency and
        # any future Square fields are retained, then change only the amount.
        inherited_money = data.get("price_money")
        if not isinstance(inherited_money, dict):
            raise PricingPlanDrift(
                "The inherited fixed price is incomplete; create a new preview.",
                details=(
                    {
                        "variation_id": str(replacement.get("id") or ""),
                        "reason": "missing_inherited_price_money",
                    },
                ),
            )
        money = copy.deepcopy(inherited_money)
        if override is None:
            overrides = data.get("location_overrides")
            if overrides is None:
                overrides = []
                data["location_overrides"] = overrides
            if not isinstance(overrides, list):
                raise PricingPlanDrift(
                    "Square returned invalid location overrides; create a new preview.",
                    details=(
                        {
                            "variation_id": str(replacement.get("id") or ""),
                            "reason": "invalid_location_overrides",
                        },
                    ),
                )
            override = {"location_id": location_id, "price_money": money}
            overrides.append(override)
        else:
            override["price_money"] = money
    money["amount"] = target_price_cents
    return replacement


def _location_override(
    data: dict[str, object],
    *,
    location_id: str,
) -> dict[str, object] | None:
    overrides = data.get("location_overrides")
    if overrides is None:
        return None
    if not isinstance(overrides, list):
        raise PricingPlanDrift(
            "Square returned invalid location overrides.",
            details=({"reason": "invalid_location_overrides", "location_id": location_id},),
        )
    matches = [
        override
        for override in overrides
        if isinstance(override, dict) and str(override.get("location_id") or "") == location_id
    ]
    if len(matches) > 1:
        raise PricingPlanDrift(
            "Square returned duplicate location price overrides.",
            details=({"reason": "duplicate_location_override", "location_id": location_id},),
        )
    return matches[0] if matches else None


def _required_nonnegative_int(entry: dict[str, object], field: str) -> int:
    value = entry.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        variation_id = str(entry.get("variation_id") or "")
        raise PricingPlanNotReady(f"{variation_id} has an invalid {field} value.")
    return value


def _response_objects(response: object) -> list[object]:
    normalized = response
    if not isinstance(response, dict) and hasattr(response, "model_dump"):
        normalized = response.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude_none=False,
        )
    errors = _value(normalized, "errors", None)
    if errors:
        raise CatalogPriceWriteError("Square could not return the current catalog prices.")
    objects = _value(normalized, "objects", None)
    if objects is None:
        raise CatalogPriceWriteError("Square returned no catalog objects for price validation.")
    return _coerce_object_list(objects)


def _coerce_object_list(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise PricingPlanDrift("Square catalog objects have an invalid response shape.") from exc


def _plain_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude_none=False,
        )
    elif dataclasses.is_dataclass(value):
        dumped = dataclasses.asdict(value)
    elif hasattr(value, "dict"):
        dumped = value.dict()
    else:
        raise PricingPlanDrift("Square returned an unsupported catalog object shape.")
    if not isinstance(dumped, dict):
        raise PricingPlanDrift("Square returned an unsupported catalog object shape.")
    return copy.deepcopy(dumped)


def _value(value: object, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _json_response(response: Any) -> dict[str, object]:
    if response is None:
        return {}
    if isinstance(response, dict):
        return _json_safe(response)
    if hasattr(response, "model_dump"):
        return _json_safe(response.model_dump(mode="json", exclude_none=True))
    if dataclasses.is_dataclass(response):
        return _json_safe(dataclasses.asdict(response))
    if hasattr(response, "dict"):
        return _json_safe(response.dict())
    return {"response": str(response)}


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
