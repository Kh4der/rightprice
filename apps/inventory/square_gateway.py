"""Build and send idempotent Square inventory adjustments.

No call in this module can write while the explicit settings gate is false.
The payload builder is separate and pure enough for tests to inspect every
field without a network connection.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.squareapi.client import get_client, to_rfc3339

from .matching import line_readiness_issues
from .models import Delivery, DeliveryLine, LineMatchStatus

BATCH_SIZE = 100
IDEMPOTENCY_NAMESPACE = uuid.UUID("730bceab-075c-4d43-b5e0-33dd63099e5a")


class InventoryPushError(RuntimeError):
    """Base class for a delivery that cannot safely be posted."""


class InventoryWritesDisabled(InventoryPushError):
    """The hard Square write gate is off."""


class DeliveryNotReady(InventoryPushError):
    """At least one included line failed a preflight check."""


@dataclass(frozen=True)
class SquareBatch:
    index: int
    idempotency_key: str
    changes: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "idempotency_key": self.idempotency_key,
            "changes": list(self.changes),
        }


@dataclass(frozen=True)
class SquarePushResult:
    delivery_id: str
    posted_lines: int
    batch_keys: tuple[str, ...]
    responses: tuple[dict[str, object], ...]
    already_pushed: bool = False
    verified: bool | None = None
    drift_lines: tuple[dict[str, object], ...] = ()
    verification_error: str = ""

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["batch_keys"] = list(self.batch_keys)
        result["responses"] = list(self.responses)
        result["drift_lines"] = list(self.drift_lines)
        return result


def build_square_batches(
    delivery: Delivery,
    *,
    actor: object,
    location_id: str | None = None,
) -> tuple[SquareBatch, ...]:
    """Create deterministic batches of at most 100 Square adjustments."""

    location_id = (location_id or getattr(settings, "SQUARE_LOCATION_ID", "")).strip()
    if not location_id:
        raise DeliveryNotReady("SQUARE_LOCATION_ID is not configured.")

    team_member_id = str(getattr(actor, "square_team_member_id", "") or "").strip()
    if not team_member_id:
        raise DeliveryNotReady("The posting employee does not have a Square team member ID.")

    lines = list(delivery.lines.order_by("position", "id"))
    included = [
        line for line in lines if line.included and line.match_status != LineMatchStatus.EXCLUDED
    ]
    if not included:
        raise DeliveryNotReady("The delivery has no included stock lines.")

    issues = [issue for line in included for issue in line_readiness_issues(line)]
    if issues:
        summary = "; ".join(f"line {issue.line_id}: {issue.message}" for issue in issues[:5])
        if len(issues) > 5:
            summary += f"; and {len(issues) - 5} more"
        raise DeliveryNotReady(summary)

    occurred_at = _occurred_at(delivery)
    changes = [
        _inventory_change(
            delivery=delivery,
            line=line,
            team_member_id=team_member_id,
            location_id=location_id,
            occurred_at=occurred_at,
        )
        for line in included
    ]

    batches: list[SquareBatch] = []
    for offset in range(0, len(changes), BATCH_SIZE):
        batch_index = offset // BATCH_SIZE
        key = str(
            uuid.uuid5(
                IDEMPOTENCY_NAMESPACE,
                f"delivery:{delivery.id}:revision:{delivery.spreadsheet_revision}:batch:{batch_index}",
            )
        )
        batches.append(
            SquareBatch(
                index=batch_index,
                idempotency_key=key,
                changes=tuple(changes[offset : offset + BATCH_SIZE]),
            )
        )
    return tuple(batches)


def send_square_batches(
    delivery: Delivery,
    *,
    actor: object,
    client: object | None = None,
) -> SquarePushResult:
    """Run the guarded network step.  Callers own status/audit persistence."""

    if not getattr(settings, "SQUARE_INVENTORY_WRITES_ENABLED", False):
        raise InventoryWritesDisabled(
            "Square inventory writes are disabled. Enable "
            "SQUARE_INVENTORY_WRITES_ENABLED only after sandbox validation."
        )

    batches = build_square_batches(delivery, actor=actor)
    square_client = client or get_client()
    responses: list[dict[str, object]] = []
    for batch in batches:
        response = square_client.inventory.batch_create_changes(
            idempotency_key=batch.idempotency_key,
            changes=list(batch.changes),
            ignore_unchanged_counts=False,
        )
        responses.append(_json_response(response))

    return SquarePushResult(
        delivery_id=str(delivery.id),
        posted_lines=sum(len(batch.changes) for batch in batches),
        batch_keys=tuple(batch.idempotency_key for batch in batches),
        responses=tuple(responses),
    )


def _inventory_change(
    *,
    delivery: Delivery,
    line: DeliveryLine,
    team_member_id: str,
    location_id: str,
    occurred_at: str,
) -> dict[str, object]:
    adjustment: dict[str, object] = {
        "id": deterministic_change_id(delivery, line),
        "reference_id": str(line.id),
        "from_state": "NONE",
        "to_state": "IN_STOCK",
        "to_location_id": location_id,
        "catalog_object_id": line.square_catalog_variation_id,
        "catalog_object_type": "ITEM_VARIATION",
        "quantity": _quantity_string(line.received_units),
        "team_member_id": team_member_id,
        "occurred_at": occurred_at,
    }
    if delivery.vendor and delivery.vendor.square_vendor_id:
        adjustment["vendor_id"] = delivery.vendor.square_vendor_id
    total_cost_cents = _validated_total_cost_cents(line)
    if (
        getattr(settings, "SQUARE_INVENTORY_COST_WRITES_ENABLED", False)
        and total_cost_cents is not None
    ):
        adjustment["cost_money"] = {
            # Square's InventoryAdjustment.cost_money is the total paid for
            # every unit in this adjustment, not the per-unit invoice cost.
            "amount": total_cost_cents,
            "currency": getattr(settings, "SQUARE_CURRENCY", "USD"),
        }
    return {"type": "ADJUSTMENT", "adjustment": adjustment}


def _validated_total_cost_cents(line: DeliveryLine) -> int | None:
    """Return an internally consistent receipt total, never a retail price."""

    if line.line_total_cents is None or line.line_total_cents < 0:
        return None
    if line.unit_cost_cents is None or line.received_units is None:
        return None
    if line.unit_cost_cents < 0 or line.received_units <= 0:
        return None
    expected = Decimal(line.unit_cost_cents) * line.received_units
    if expected != expected.to_integral_value() or int(expected) != line.line_total_cents:
        return None
    return line.line_total_cents


def deterministic_change_id(delivery: Delivery, line: DeliveryLine) -> str:
    return str(
        uuid.uuid5(
            IDEMPOTENCY_NAMESPACE,
            f"delivery:{delivery.id}:revision:{delivery.spreadsheet_revision}:line:{line.id}",
        )
    )


def _quantity_string(value: Decimal | None) -> str:
    if value is None:
        raise DeliveryNotReady("Received quantity is missing.")
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _occurred_at(delivery: Delivery) -> str:
    submitted_at = delivery.submission.submitted_at
    moment = submitted_at or delivery.created_at
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return to_rfc3339(moment)


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
    """Round-trip through JSON so the result is always safe for JSONField."""

    return json.loads(json.dumps(value, default=str))
