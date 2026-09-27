"""Public inventory workflow services used by views and background tasks."""

from __future__ import annotations

import datetime as dt
import hmac
import uuid
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.capture.models import SubmissionStatus
from apps.squareapi.client import get_client

from .boundaries import is_demo_inventory_request
from .catalog import CatalogRefreshResult, refresh_catalog
from .matching import (
    LineIssue,
    line_readiness_issues,
    normalize_delivery_lines,
    normalize_description,
    normalize_upc,
    normalize_vendor_sku,
)
from .models import Delivery, DeliveryLine, DeliveryStatus
from .packs import MAX_UNITS_PER_CASE
from .square_gateway import (
    DeliveryNotReady,
    InventoryWritesDisabled,
    SquarePushResult,
    build_square_batches,
    deterministic_change_id,
    send_square_batches,
)
from .workbooks import (
    DeliveryWorkbook,
    WorkbookValidationError,
    export_workbook,
    read_workbook,
    restore_excel_text,
    workbook_signature,
)


@dataclass(frozen=True)
class ReadinessResult:
    delivery_id: str
    ready: bool
    status: str
    included_lines: int
    excluded_lines: int
    issues: tuple[LineIssue, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["issues"] = [issue.to_dict() for issue in self.issues]
        return result


@dataclass(frozen=True)
class LineChange:
    line_id: str
    position: int
    changes: dict[str, dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ImportResult:
    delivery_id: str
    revision_before: int
    revision_after: int
    changed_lines: tuple[LineChange, ...]
    readiness: ReadinessResult

    @property
    def changed_line_count(self) -> int:
        return len(self.changed_lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "changed_line_count": self.changed_line_count,
            "changed_lines": [change.to_dict() for change in self.changed_lines],
            "readiness": self.readiness.to_dict(),
        }


@dataclass(frozen=True)
class CountSnapshot:
    line_id: str
    variation_id: str
    before: str
    proposed_delta: str
    projected_after: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CountRefreshResult:
    delivery_id: str
    captured_at: str
    snapshots: tuple[CountSnapshot, ...]
    readiness: ReadinessResult

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "captured_at": self.captured_at,
            "snapshots": [snapshot.to_dict() for snapshot in self.snapshots],
            "readiness": self.readiness.to_dict(),
        }


@dataclass(frozen=True)
class LineVerification:
    line_id: str
    actual: Decimal
    drift: Decimal


@dataclass(frozen=True)
class VerificationReading:
    verified: bool
    lines: tuple[LineVerification, ...]
    drift_lines: tuple[dict[str, object], ...]


EDITABLE_FIELDS = (
    "vendor_sku",
    "upc",
    "description",
    "pack_text",
    "cases",
    "units_per_case",
    "received_units",
    "square_catalog_variation_id",
    "square_item_name",
    "included",
    "review_note",
    "unit_cost_cents",
    "line_total_cents",
    "match_status",
)

CHANGE_FIELDS = (
    *EDITABLE_FIELDS,
    "square_count_before",
    "square_count_variation_id",
    "projected_count_after",
    "square_count_after",
    "square_count_drift",
    "square_count_snapshot_at",
    "square_count_verified_at",
)


def export_delivery_xlsx(delivery: Delivery) -> bytes:
    """Return a safe, revision-bound final snapshot for one delivery."""

    if delivery.pk is None:
        raise ValueError("Delivery must be saved before it can be exported.")
    return export_workbook(delivery)


def import_delivery_xlsx(
    delivery: Delivery,
    file: object,
    actor: object | None = None,
) -> ImportResult:
    """Validate and atomically apply one exported review workbook."""

    workbook = read_workbook(file)
    if workbook.delivery_id != delivery.id:
        raise WorkbookValidationError("This workbook belongs to a different delivery.")

    with transaction.atomic():
        locked = (
            Delivery.objects.select_for_update(of=("self",))
            .select_related("vendor", "submission__submitted_by")
            .get(pk=delivery.pk)
        )
        if is_demo_inventory_request(locked, actor=actor):
            raise DeliveryNotReady("Practice mode never edits operational inventory.")
        if locked.status in {
            DeliveryStatus.PUSHING,
            DeliveryStatus.PUSHED,
            DeliveryStatus.PUSHED_WITH_DRIFT,
            DeliveryStatus.PUSHED_UNVERIFIED,
        } or (locked.status == DeliveryStatus.FAILED and locked.square_batch_keys):
            raise WorkbookValidationError(
                "A delivery cannot be edited while or after it is posted to Square."
            )
        _validate_workbook_identity(locked, workbook)

        lines = {
            str(line.id): line
            for line in locked.lines.select_for_update().order_by("position", "id")
        }
        parsed_rows = _parse_rows(workbook, lines)
        before = {line_id: _line_snapshot(line) for line_id, line in lines.items()}

        for line_id, values in parsed_rows.items():
            line = lines[line_id]
            identifiers_changed = (
                values["vendor_sku"] != line.vendor_sku or values["upc"] != line.upc
            )
            old_variation = line.square_catalog_variation_id
            for field, value in values.items():
                setattr(line, field, value)
            # If an identifier was corrected but the old Square ID was merely
            # carried along unchanged, force a fresh exact match.  An explicit
            # new Square ID remains a deliberate human match.
            if identifiers_changed and values["square_catalog_variation_id"] == old_variation:
                line.square_catalog_variation_id = ""
                line.square_item_name = ""
            line.save(update_fields=[*EDITABLE_FIELDS, "updated_at"])

        # Counts are evidence for one exact reviewed revision. Even a harmless
        # edit creates a new revision, so require a fresh Square snapshot rather
        # than carrying old evidence forward.
        locked.lines.update(
            square_count_before=None,
            square_count_variation_id="",
            projected_count_after=None,
            square_count_after=None,
            square_count_drift=None,
            square_count_snapshot_at=None,
            square_count_verified_at=None,
        )

        revision_before = locked.spreadsheet_revision
        locked.spreadsheet_revision += 1
        locked.save(update_fields=["spreadsheet_revision", "updated_at"])

        normalize_delivery_lines(locked)
        readiness = _refresh_status_from_current_lines(locked)

        refreshed_lines = {str(line.id): line for line in locked.lines.order_by("position", "id")}
        changes = _changes(before, refreshed_lines)
        result = ImportResult(
            delivery_id=str(locked.id),
            revision_before=revision_before,
            revision_after=locked.spreadsheet_revision,
            changed_lines=tuple(changes),
            readiness=readiness,
        )
        _audit(
            actor=actor,
            action="inventory.delivery_spreadsheet_imported",
            delivery=locked,
            detail=result.to_dict(),
        )

    delivery.refresh_from_db()
    return result


def refresh_delivery_readiness(delivery: Delivery) -> ReadinessResult:
    """Normalize exact matches, validate line arithmetic and update status."""

    if delivery.pk is None:
        raise ValueError("Delivery must be saved before readiness can be checked.")
    with transaction.atomic():
        locked = Delivery.objects.select_for_update().get(pk=delivery.pk)
        normalize_delivery_lines(locked)
        result = _refresh_status_from_current_lines(locked)
    delivery.refresh_from_db()
    return result


def refresh_square_catalog(
    delivery: Delivery,
    actor: object,
    client: object | None = None,
) -> CatalogRefreshResult:
    """Refresh the read-only local Square variation cache."""

    return refresh_catalog(delivery, actor=actor, client=client)


def refresh_square_counts(
    delivery: Delivery,
    client: object | None = None,
    actor: object | None = None,
) -> CountRefreshResult:
    """Snapshot current Square counts and calculate the reviewed projection."""

    if is_demo_inventory_request(delivery, actor=actor):
        raise DeliveryNotReady("Practice mode never contacts Square.")
    square_client = client or get_client()
    with transaction.atomic():
        locked = (
            Delivery.objects.select_for_update(of=("self",))
            .select_related("vendor", "submission")
            .get(pk=delivery.pk)
        )
        if locked.status in {
            DeliveryStatus.PUSHING,
            DeliveryStatus.PUSHED,
            DeliveryStatus.PUSHED_WITH_DRIFT,
            DeliveryStatus.PUSHED_UNVERIFIED,
        } or (locked.status == DeliveryStatus.FAILED and locked.square_batch_keys):
            raise DeliveryNotReady("Counts cannot be replaced after posting has started.")
        normalize_delivery_lines(locked)
        lines = list(
            locked.lines.filter(included=True, match_status="MATCHED").order_by("position", "id")
        )
        if not lines:
            raise DeliveryNotReady("No reviewed Square item matches are ready to count.")

        variation_ids = sorted({line.square_catalog_variation_id for line in lines})
        counts = _fetch_square_counts(square_client, variation_ids)
        deltas: dict[str, Decimal] = {}
        for line in lines:
            if line.received_units is None:
                raise DeliveryNotReady(f"Line {line.position} has no received quantity.")
            variation_id = line.square_catalog_variation_id
            deltas[variation_id] = deltas.get(variation_id, Decimal(0)) + line.received_units

        captured_at = timezone.now()
        snapshots: list[CountSnapshot] = []
        for line in lines:
            variation_id = line.square_catalog_variation_id
            before = counts[variation_id]
            projected = before + deltas[variation_id]
            line.square_count_before = before
            line.square_count_variation_id = variation_id
            line.projected_count_after = projected
            line.square_count_after = None
            line.square_count_drift = None
            line.square_count_snapshot_at = captured_at
            line.square_count_verified_at = None
            line.save(
                update_fields=[
                    "square_count_before",
                    "square_count_variation_id",
                    "projected_count_after",
                    "square_count_after",
                    "square_count_drift",
                    "square_count_snapshot_at",
                    "square_count_verified_at",
                    "updated_at",
                ]
            )
            snapshots.append(
                CountSnapshot(
                    line_id=str(line.id),
                    variation_id=variation_id,
                    before=_decimal_string(before),
                    proposed_delta=_decimal_string(line.received_units),
                    projected_after=_decimal_string(projected),
                )
            )
        readiness = _refresh_status_from_current_lines(locked)
        result = CountRefreshResult(
            delivery_id=str(locked.id),
            captured_at=captured_at.isoformat(),
            snapshots=tuple(snapshots),
            readiness=readiness,
        )
        if actor is not None:
            _audit(
                actor=actor,
                action="inventory.square_counts_refreshed",
                delivery=locked,
                detail=result.to_dict(),
            )

    delivery.refresh_from_db()
    return result


def push_delivery_to_square(
    delivery: Delivery,
    actor: object,
    client: object | None = None,
) -> SquarePushResult:
    """Post through a committed, crash-recoverable idempotent Square request."""

    if is_demo_inventory_request(delivery, actor=actor):
        raise InventoryWritesDisabled("Practice mode never changes Square inventory.")
    if not getattr(settings, "SQUARE_INVENTORY_WRITES_ENABLED", False):
        raise InventoryWritesDisabled(
            "Square inventory writes are disabled. Enable "
            "SQUARE_INVENTORY_WRITES_ENABLED only after sandbox validation."
        )
    square_client = client or get_client()

    claimed, retrying_same_push, terminal = _claim_square_push(delivery, actor)
    if terminal is not None:
        delivery.refresh_from_db()
        return terminal

    # The claim and deterministic keys are committed before either the read
    # preflight or the first Square write. A process death can therefore resume
    # the exact request instead of silently creating a new inventory receipt.
    if not retrying_same_push:
        try:
            _assert_snapshot_is_current(claimed, square_client)
        except Exception as exc:
            _release_unsent_push_claim(claimed, actor=actor, error=exc)
            delivery.refresh_from_db()
            raise

    claimed = (
        Delivery.objects.select_related("vendor", "submission", "pushed_by")
        .prefetch_related("lines")
        .get(pk=delivery.pk)
    )
    payload_actor = claimed.pushed_by or actor
    batches = build_square_batches(claimed, actor=payload_actor)
    if list(claimed.square_batch_keys) != [batch.idempotency_key for batch in batches]:
        raise DeliveryNotReady(
            "The protected Square request no longer matches its stored keys; manual review is required."
        )

    try:
        result = send_square_batches(claimed, actor=payload_actor, client=square_client)
    except Exception as exc:
        terminal = _finalize_square_push_failure(claimed, actor=actor, error=exc)
        delivery.refresh_from_db()
        if terminal is not None:
            return terminal
        raise

    verification: VerificationReading | None
    try:
        verification = _read_after_push(claimed, square_client)
    except Exception as exc:
        verification = None
        result = replace(
            result,
            verified=False,
            verification_error=str(exc)[:1000],
        )
    else:
        result = replace(
            result,
            verified=verification.verified,
            drift_lines=verification.drift_lines,
        )

    result = _finalize_square_push_success(
        claimed,
        actor=actor,
        result=result,
        verification=verification,
    )
    delivery.refresh_from_db()
    return result


def _claim_square_push(
    delivery: Delivery,
    actor: object,
) -> tuple[Delivery, bool, SquarePushResult | None]:
    """Commit an immutable request identity before making a Square network call."""

    with transaction.atomic():
        locked = (
            Delivery.objects.select_for_update(of=("self",))
            .select_related("vendor", "submission__submitted_by", "pushed_by")
            .get(pk=delivery.pk)
        )
        if locked.submission.submitted_by.is_demo:
            raise InventoryWritesDisabled("Practice mode never changes Square inventory.")
        if not getattr(actor, "is_owner", False):
            raise DeliveryNotReady("Only an owner can update Square inventory.")
        if locked.submission.status != SubmissionStatus.APPROVED:
            raise DeliveryNotReady("Approve the invoice evidence before updating Square inventory.")
        if locked.status in {
            DeliveryStatus.PUSHED,
            DeliveryStatus.PUSHED_WITH_DRIFT,
            DeliveryStatus.PUSHED_UNVERIFIED,
        }:
            return locked, True, _stored_push_result(locked)

        retrying_same_push = locked.status == DeliveryStatus.FAILED and bool(
            locked.square_batch_keys
        )
        if locked.status == DeliveryStatus.PUSHING and locked.square_batch_keys:
            stale_seconds = max(
                60,
                int(getattr(settings, "SQUARE_PUSH_STALE_SECONDS", 900)),
            )
            age = timezone.now() - locked.updated_at
            if age < dt.timedelta(seconds=stale_seconds):
                raise DeliveryNotReady(
                    "This protected Square request is still in progress. Wait before retrying."
                )
            retrying_same_push = True

        normalize_delivery_lines(locked)
        readiness = _refresh_status_from_current_lines(locked)
        if not readiness.ready:
            messages = "; ".join(issue.message for issue in readiness.issues[:5])
            raise DeliveryNotReady(messages or "Delivery is not ready to post.")

        payload_actor = locked.pushed_by if retrying_same_push and locked.pushed_by else actor
        batches = build_square_batches(locked, actor=payload_actor)
        new_batch_keys = [batch.idempotency_key for batch in batches]
        if retrying_same_push and list(locked.square_batch_keys) != new_batch_keys:
            raise DeliveryNotReady(
                "The failed push no longer matches its idempotency keys; manual review is required."
            )

        locked.square_batch_keys = new_batch_keys
        locked.status = DeliveryStatus.PUSHING
        locked.push_error = ""
        if not locked.pushed_by_id and getattr(actor, "pk", None):
            locked.pushed_by = actor
        locked.save(
            update_fields=[
                "square_batch_keys",
                "status",
                "push_error",
                "pushed_by",
                "updated_at",
            ]
        )
        for line in locked.lines.filter(included=True).exclude(match_status="EXCLUDED"):
            line.square_change_id = deterministic_change_id(locked, line)
            line.save(update_fields=["square_change_id", "updated_at"])
        _audit(
            actor=actor,
            action=(
                "inventory.square_push_resumed"
                if retrying_same_push
                else "inventory.square_push_claimed"
            ),
            delivery=locked,
            detail={
                "batch_keys": new_batch_keys,
                "spreadsheet_revision": locked.spreadsheet_revision,
                "payload_team_member_id": str(
                    getattr(payload_actor, "square_team_member_id", "") or ""
                ),
            },
        )
        return locked, retrying_same_push, None


def _release_unsent_push_claim(
    delivery: Delivery,
    *,
    actor: object,
    error: Exception,
) -> None:
    """Release a claim only when this process knows no Square write was attempted."""

    expected_keys = list(delivery.square_batch_keys)
    with transaction.atomic():
        locked = Delivery.objects.select_for_update(of=("self",)).get(pk=delivery.pk)
        if (
            locked.status != DeliveryStatus.PUSHING
            or list(locked.square_batch_keys) != expected_keys
        ):
            return
        locked.status = DeliveryStatus.NEEDS_REVIEW
        locked.square_batch_keys = []
        locked.pushed_by = None
        locked.push_error = str(error)[:4000]
        locked.save(
            update_fields=[
                "status",
                "square_batch_keys",
                "pushed_by",
                "push_error",
                "updated_at",
            ]
        )
        locked.lines.update(
            square_change_id="",
            square_count_before=None,
            square_count_variation_id="",
            projected_count_after=None,
            square_count_after=None,
            square_count_drift=None,
            square_count_snapshot_at=None,
            square_count_verified_at=None,
        )
        _audit(
            actor=actor,
            action="inventory.square_push_preflight_blocked",
            delivery=locked,
            detail={
                "batch_keys": expected_keys,
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
            },
        )


def _finalize_square_push_failure(
    delivery: Delivery,
    *,
    actor: object,
    error: Exception,
) -> SquarePushResult | None:
    expected_keys = list(delivery.square_batch_keys)
    with transaction.atomic():
        locked = Delivery.objects.select_for_update(of=("self",)).get(pk=delivery.pk)
        if locked.status in {
            DeliveryStatus.PUSHED,
            DeliveryStatus.PUSHED_WITH_DRIFT,
            DeliveryStatus.PUSHED_UNVERIFIED,
        }:
            return _stored_push_result(locked)
        _require_push_keys(locked, expected_keys)
        locked.status = DeliveryStatus.FAILED
        locked.push_error = str(error)[:4000]
        locked.save(update_fields=["status", "push_error", "updated_at"])
        _audit(
            actor=actor,
            action="inventory.square_push_failed",
            delivery=locked,
            detail={
                "batch_keys": expected_keys,
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
            },
        )
    return None


def _finalize_square_push_success(
    delivery: Delivery,
    *,
    actor: object,
    result: SquarePushResult,
    verification: VerificationReading | None,
) -> SquarePushResult:
    expected_keys = list(delivery.square_batch_keys)
    with transaction.atomic():
        locked = (
            Delivery.objects.select_for_update(of=("self",))
            .select_related("pushed_by")
            .get(pk=delivery.pk)
        )
        if locked.status in {
            DeliveryStatus.PUSHED,
            DeliveryStatus.PUSHED_WITH_DRIFT,
            DeliveryStatus.PUSHED_UNVERIFIED,
        }:
            return _stored_push_result(locked)
        _require_push_keys(locked, expected_keys)

        if verification is None:
            locked.status = DeliveryStatus.PUSHED_UNVERIFIED
            locked.push_error = (
                "Inventory posted, but the follow-up Square count could not be read: "
                f"{result.verification_error}"
            )[:4000]
        else:
            line_map = {
                str(line.id): line
                for line in locked.lines.select_for_update().filter(
                    included=True,
                    match_status="MATCHED",
                )
            }
            verified_at = timezone.now()
            for reading in verification.lines:
                line = line_map.get(reading.line_id)
                if line is None:
                    raise DeliveryNotReady(
                        "A posted delivery line changed before verification could be saved."
                    )
                line.square_count_after = reading.actual
                line.square_count_drift = reading.drift
                line.square_count_verified_at = verified_at
                line.save(
                    update_fields=[
                        "square_count_after",
                        "square_count_drift",
                        "square_count_verified_at",
                        "updated_at",
                    ]
                )
            if verification.verified:
                locked.status = DeliveryStatus.PUSHED
                locked.push_error = ""
            else:
                locked.status = DeliveryStatus.PUSHED_WITH_DRIFT
                locked.push_error = (
                    "Inventory posted, but the verified Square count differs "
                    "from the pre-push projection."
                )

        locked.square_result = result.to_dict()
        locked.pushed_at = timezone.now()
        if not locked.pushed_by_id and getattr(actor, "pk", None):
            locked.pushed_by = actor
        locked.save(
            update_fields=[
                "status",
                "square_result",
                "pushed_at",
                "pushed_by",
                "push_error",
                "updated_at",
            ]
        )
        _audit(
            actor=actor,
            action="inventory.square_push_succeeded",
            delivery=locked,
            detail=result.to_dict(),
        )
    return result


def _require_push_keys(delivery: Delivery, expected_keys: list[str]) -> None:
    if list(delivery.square_batch_keys) != expected_keys:
        raise DeliveryNotReady(
            "The stored Square request changed while it was running; manual review is required."
        )


def _stored_push_result(delivery: Delivery) -> SquarePushResult:
    stored = delivery.square_result or {}
    return SquarePushResult(
        delivery_id=str(delivery.id),
        posted_lines=int(
            stored.get(
                "posted_lines",
                delivery.lines.filter(included=True, match_status="MATCHED").count(),
            )
        ),
        batch_keys=tuple(delivery.square_batch_keys or ()),
        responses=tuple(stored.get("responses", ())),
        already_pushed=True,
        verified=stored.get("verified"),
        drift_lines=tuple(stored.get("drift_lines", ())),
        verification_error=stored.get("verification_error", ""),
    )


def _validate_workbook_identity(delivery: Delivery, workbook: DeliveryWorkbook) -> None:
    if workbook.revision != delivery.spreadsheet_revision:
        raise WorkbookValidationError(
            "This workbook is stale. Export a fresh copy before making more changes."
        )
    line_ids = [
        str(value)
        for value in delivery.lines.order_by("position", "id").values_list("id", flat=True)
    ]
    expected_signature = workbook_signature(
        delivery_id=delivery.id,
        revision=delivery.spreadsheet_revision,
        line_ids=line_ids,
    )
    if not hmac.compare_digest(workbook.signature, expected_signature):
        raise WorkbookValidationError(
            "Workbook metadata or delivery line identifiers were changed. Export a fresh copy."
        )


def _parse_rows(
    workbook: DeliveryWorkbook,
    lines: dict[str, DeliveryLine],
) -> dict[str, dict[str, object]]:
    parsed: dict[str, dict[str, object]] = {}
    for workbook_line in workbook.lines:
        values = workbook_line.values
        row = workbook_line.row_number
        try:
            line_id = str(uuid.UUID(_text(values["Line ID"], "Line ID", row=row)))
        except ValueError as exc:
            raise WorkbookValidationError("Line ID must be a UUID.", row=row) from exc
        if line_id in parsed:
            raise WorkbookValidationError("A delivery line appears more than once.", row=row)
        if line_id not in lines:
            raise WorkbookValidationError(
                "The workbook contains an unknown delivery line.", row=row
            )

        position = _integer(values["Position"], "Position", row=row, minimum=0)
        if position != lines[line_id].position:
            raise WorkbookValidationError("Line Position cannot be changed.", row=row)

        parsed[line_id] = {
            "vendor_sku": normalize_vendor_sku(
                _text(values["Vendor SKU"], "Vendor SKU", row=row, maximum=100)
            ),
            "upc": normalize_upc(_text(values["UPC"], "UPC", row=row, maximum=40)),
            "description": normalize_description(
                _text(
                    values["Description"],
                    "Description",
                    row=row,
                    maximum=300,
                    required=True,
                )
            ),
            "pack_text": _text(values["Pack"], "Pack", row=row, maximum=80),
            "cases": _decimal(values["Cases"], "Cases", row=row, max_digits=12, decimal_places=3),
            "units_per_case": _integer(
                values["Units per case"],
                "Units per case",
                row=row,
                minimum=1,
                maximum=MAX_UNITS_PER_CASE,
                optional=True,
            ),
            "received_units": _decimal(
                values["Received units"],
                "Received units",
                row=row,
                max_digits=14,
                decimal_places=3,
            ),
            "square_catalog_variation_id": _text(
                values["Square variation ID"],
                "Square variation ID",
                row=row,
                maximum=64,
            ),
            "square_item_name": _text(
                values["Square item name"], "Square item name", row=row, maximum=300
            ),
            "included": _boolean(values["Include"], "Include", row=row),
            "match_status": _match_status(values["Match status"], row=row),
            "review_note": _text(values["Review note"], "Review note", row=row, maximum=300),
            "unit_cost_cents": _money_cents(
                values["Invoice unit cost"], "Invoice unit cost", row=row
            ),
            "line_total_cents": _money_cents(
                values["Invoice line total"], "Invoice line total", row=row
            ),
        }
        if len(parsed[line_id]["upc"]) > 14:
            raise WorkbookValidationError("UPC cannot exceed 14 digits.", field="UPC", row=row)

    if set(parsed) != set(lines):
        missing = len(set(lines) - set(parsed))
        raise WorkbookValidationError(f"The workbook is missing {missing} delivery line(s).")
    return parsed


def _text(
    value: object,
    field: str,
    *,
    row: int,
    maximum: int | None = None,
    required: bool = False,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif isinstance(value, Decimal):
        text = format(value, "f")
        if text.endswith(".0"):
            text = text[:-2]
    else:
        text = restore_excel_text(value)
    text = text.strip()
    if required and not text:
        raise WorkbookValidationError(f"{field} is required.", field=field, row=row)
    if maximum is not None and len(text) > maximum:
        raise WorkbookValidationError(
            f"{field} cannot exceed {maximum} characters.", field=field, row=row
        )
    return text


def _integer(
    value: object,
    field: str,
    *,
    row: int,
    minimum: int | None = None,
    maximum: int | None = None,
    optional: bool = False,
) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise WorkbookValidationError(f"{field} is required.", field=field, row=row)
    if isinstance(value, bool):
        raise WorkbookValidationError(f"{field} must be a whole number.", field=field, row=row)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise WorkbookValidationError(
            f"{field} must be a whole number.", field=field, row=row
        ) from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise WorkbookValidationError(f"{field} must be a whole number.", field=field, row=row)
    result = int(number)
    if minimum is not None and result < minimum:
        raise WorkbookValidationError(f"{field} must be at least {minimum}.", field=field, row=row)
    if maximum is not None and result > maximum:
        raise WorkbookValidationError(f"{field} cannot exceed {maximum}.", field=field, row=row)
    return result


def _decimal(
    value: object,
    field: str,
    *,
    row: int,
    max_digits: int,
    decimal_places: int,
) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise WorkbookValidationError(f"{field} must be numeric.", field=field, row=row)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise WorkbookValidationError(f"{field} must be numeric.", field=field, row=row) from exc
    if not number.is_finite():
        raise WorkbookValidationError(f"{field} must be finite.", field=field, row=row)

    _, digits, exponent = number.as_tuple()
    decimals = max(-exponent, 0)
    total_digits = len(digits) + max(exponent, 0)
    whole_digits = max(total_digits - decimals, 0)
    if decimals > decimal_places or total_digits > max_digits:
        raise WorkbookValidationError(
            f"{field} allows at most {max_digits} digits and {decimal_places} decimal places.",
            field=field,
            row=row,
        )
    if whole_digits > max_digits - decimal_places:
        raise WorkbookValidationError(
            f"{field} has too many digits before the decimal point.", field=field, row=row
        )
    return number


def _money_cents(value: object, field: str, *, row: int) -> int | None:
    amount = _decimal(value, field, row=row, max_digits=12, decimal_places=2)
    return int(amount * 100) if amount is not None else None


def _boolean(value: object, field: str, *, row: int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal) and value in {Decimal(0), Decimal(1)}:
        return bool(int(value))
    normalized = str(value or "").strip().upper()
    if normalized in {"TRUE", "YES", "Y", "1"}:
        return True
    if normalized in {"FALSE", "NO", "N", "0"}:
        return False
    raise WorkbookValidationError(f"{field} must be TRUE or FALSE.", field=field, row=row)


def _match_status(value: object, *, row: int) -> str:
    normalized = str(value or "").strip().upper()
    allowed = {"UNMATCHED", "SUGGESTED", "MATCHED", "EXCLUDED"}
    if normalized not in allowed:
        raise WorkbookValidationError(
            "Match status must be UNMATCHED, SUGGESTED, MATCHED, or EXCLUDED.",
            field="Match status",
            row=row,
        )
    return normalized


def _fetch_square_counts(
    client: object,
    variation_ids: list[str],
) -> dict[str, Decimal]:
    location_id = str(getattr(settings, "SQUARE_LOCATION_ID", "") or "").strip()
    if not location_id:
        raise DeliveryNotReady("SQUARE_LOCATION_ID is not configured.")
    counts: dict[str, Decimal] = {}
    for offset in range(0, len(variation_ids), 100):
        requested = variation_ids[offset : offset + 100]
        response = client.inventory.batch_get_counts(
            catalog_object_ids=requested,
            location_ids=[location_id],
            states=["IN_STOCK"],
        )
        response_errors = _object_value(response, "errors", None)
        if response_errors:
            raise DeliveryNotReady("Square returned an error while reading inventory counts.")
        if isinstance(response, dict):
            iterable = response.get("counts", [])
        elif hasattr(response, "counts") and not hasattr(response, "__next__"):
            iterable = getattr(response, "counts", None) or []
        else:
            # The Square SDK normally returns a SyncPager here.
            iterable = response
        for count in iterable:
            variation_id = str(_object_value(count, "catalog_object_id", "") or "")
            if variation_id not in requested:
                continue
            state = str(_object_value(count, "state", "") or "")
            count_location = str(_object_value(count, "location_id", "") or "")
            if state != "IN_STOCK":
                continue
            if count_location != location_id:
                continue
            raw_quantity = _object_value(count, "quantity", None)
            if raw_quantity is None:
                raise DeliveryNotReady(
                    f"Square returned a count without a quantity for variation {variation_id}."
                )
            try:
                quantity = Decimal(str(raw_quantity))
            except InvalidOperation as exc:
                raise DeliveryNotReady(
                    f"Square returned an invalid count for variation {variation_id}."
                ) from exc
            if not quantity.is_finite():
                raise DeliveryNotReady(
                    f"Square returned a non-finite count for variation {variation_id}."
                )
            previous = counts.get(variation_id)
            if previous is not None and previous != quantity:
                raise DeliveryNotReady(
                    f"Square returned conflicting counts for variation {variation_id}."
                )
            counts[variation_id] = quantity
        # Square documents an absent count as a variation that has never
        # interacted with this state/location; inventory starts at zero.
        for variation_id in requested:
            counts.setdefault(variation_id, Decimal(0))
    return counts


def _read_after_push(
    delivery: Delivery,
    client: object,
) -> VerificationReading:
    lines = list(
        delivery.lines.filter(included=True, match_status="MATCHED").order_by("position", "id")
    )
    variation_ids = sorted({line.square_catalog_variation_id for line in lines})
    counts = _fetch_square_counts(client, variation_ids)
    drift_lines: list[dict[str, object]] = []
    line_results: list[LineVerification] = []
    for line in lines:
        actual = counts[line.square_catalog_variation_id]
        if line.projected_count_after is None:
            raise DeliveryNotReady(f"Line {line.position} has no pre-push projected Square count.")
        drift = actual - line.projected_count_after
        line_results.append(
            LineVerification(
                line_id=str(line.id),
                actual=actual,
                drift=drift,
            )
        )
        if drift:
            drift_lines.append(
                {
                    "line_id": str(line.id),
                    "variation_id": line.square_catalog_variation_id,
                    "expected": _decimal_string(line.projected_count_after),
                    "actual": _decimal_string(actual),
                    "drift": _decimal_string(drift),
                }
            )
    return VerificationReading(
        verified=not drift_lines,
        lines=tuple(line_results),
        drift_lines=tuple(drift_lines),
    )


def _assert_snapshot_is_current(delivery: Delivery, client: object) -> None:
    lines = list(
        delivery.lines.filter(included=True, match_status="MATCHED").order_by("position", "id")
    )
    variation_ids = sorted({line.square_catalog_variation_id for line in lines})
    live_counts = _fetch_square_counts(client, variation_ids)
    for line in lines:
        live = live_counts[line.square_catalog_variation_id]
        if line.square_count_before != live:
            raise DeliveryNotReady(
                "Square inventory changed after review. Refresh counts and review the "
                "new projection before posting."
            )


def _object_value(value: object, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _refresh_status_from_current_lines(delivery: Delivery) -> ReadinessResult:
    lines = list(delivery.lines.order_by("position", "id"))
    included = [line for line in lines if line.included and line.match_status != "EXCLUDED"]
    excluded_count = len(lines) - len(included)
    issues = [issue for line in included for issue in line_readiness_issues(line)]
    # Real photographed deliveries need enough cross-page identity for the
    # eventual stock adjustment to be traceable back to its source invoice.
    if delivery.submission.documents.exists():
        if not (delivery.vendor_id or delivery.vendor_name_raw.strip()):
            issues.append(
                LineIssue(str(delivery.id), "vendor_missing", "Confirm the invoice vendor.")
            )
        if not delivery.invoice_number.strip():
            issues.append(
                LineIssue(
                    str(delivery.id),
                    "invoice_number_missing",
                    "Confirm the invoice number from one of the photographed pages.",
                )
            )
        if delivery.invoice_date is None:
            issues.append(
                LineIssue(
                    str(delivery.id),
                    "invoice_date_missing",
                    "Confirm the invoice date from one of the photographed pages.",
                )
            )
    issues.extend(_duplicate_delivery_issues(delivery))
    issues.extend(_projection_issues(included))
    if not included:
        issues.append(
            LineIssue(str(delivery.id), "no_stock_lines", "Include at least one stock line.")
        )
    ready = not issues
    if delivery.status not in {
        DeliveryStatus.PUSHING,
        DeliveryStatus.PUSHED,
        DeliveryStatus.PUSHED_WITH_DRIFT,
        DeliveryStatus.PUSHED_UNVERIFIED,
    } and not (delivery.status == DeliveryStatus.FAILED and delivery.square_batch_keys):
        new_status = DeliveryStatus.READY if ready else DeliveryStatus.NEEDS_REVIEW
        if delivery.status != new_status:
            delivery.status = new_status
            delivery.save(update_fields=["status", "updated_at"])
    return ReadinessResult(
        delivery_id=str(delivery.id),
        ready=ready,
        status=delivery.status,
        included_lines=len(included),
        excluded_lines=excluded_count,
        issues=tuple(issues),
    )


def _duplicate_delivery_issues(delivery: Delivery) -> list[LineIssue]:
    """Block a second stock receipt for the same invoice or source evidence."""

    candidates = (
        Delivery.objects.select_related("vendor", "submission")
        .exclude(pk=delivery.pk)
        .exclude(submission__status=SubmissionStatus.REJECTED)
    )
    issues: list[LineIssue] = []

    invoice_number = delivery.invoice_number.strip().casefold()
    vendor_names = _delivery_vendor_names(delivery)
    if invoice_number and delivery.invoice_date is not None and vendor_names:
        possible_invoice_duplicates = candidates.filter(invoice_date=delivery.invoice_date)
        for other in possible_invoice_duplicates:
            if other.invoice_number.strip().casefold() != invoice_number:
                continue
            same_vendor_id = bool(
                delivery.vendor_id and other.vendor_id and delivery.vendor_id == other.vendor_id
            )
            if not same_vendor_id and not (vendor_names & _delivery_vendor_names(other)):
                continue
            issues.append(
                LineIssue(
                    str(delivery.id),
                    "duplicate_vendor_invoice",
                    (
                        f"Invoice {delivery.invoice_number.strip()} for this vendor and "
                        f"date already appears in submission {str(other.submission_id)[:8]}. "
                        "Reject the duplicate submission before posting stock."
                    ),
                )
            )
            break

    source_hashes = tuple(
        delivery.submission.documents.exclude(sha256="").values_list("sha256", flat=True).distinct()
    )
    if source_hashes:
        other = (
            candidates.filter(submission__documents__sha256__in=source_hashes)
            .order_by("created_at", "pk")
            .first()
        )
        if other is not None:
            issues.append(
                LineIssue(
                    str(delivery.id),
                    "duplicate_source_photo",
                    (
                        "This invoice uses a photo already attached to another "
                        "non-rejected delivery. Reject the duplicate submission before "
                        "posting stock."
                    ),
                )
            )

    return issues


def _delivery_vendor_names(delivery: Delivery) -> set[str]:
    names = {delivery.vendor_name_raw.strip().casefold()}
    if delivery.vendor_id and delivery.vendor:
        names.add(delivery.vendor.name.strip().casefold())
    names.discard("")
    return names


def _projection_issues(lines: list[DeliveryLine]) -> list[LineIssue]:
    grouped: dict[str, list[DeliveryLine]] = {}
    for line in lines:
        if line.square_catalog_variation_id:
            grouped.setdefault(line.square_catalog_variation_id, []).append(line)

    issues: list[LineIssue] = []
    for variation_id, variation_lines in grouped.items():
        if any(line.received_units is None for line in variation_lines):
            continue
        before_values = {line.square_count_before for line in variation_lines}
        if None in before_values or len(before_values) != 1:
            continue
        before = next(iter(before_values))
        total_delta = sum(
            (line.received_units for line in variation_lines),
            start=Decimal(0),
        )
        expected = before + total_delta
        for line in variation_lines:
            if line.projected_count_after != expected:
                issues.append(
                    LineIssue(
                        str(line.id),
                        "square_projection_stale",
                        f"The Square projection for {variation_id} is stale; refresh counts.",
                    )
                )
    return issues


def _line_snapshot(line: DeliveryLine) -> dict[str, object]:
    return {field: _audit_value(getattr(line, field)) for field in CHANGE_FIELDS}


def _changes(
    before: dict[str, dict[str, object]],
    after: dict[str, DeliveryLine],
) -> list[LineChange]:
    result: list[LineChange] = []
    for line_id, line in sorted(after.items(), key=lambda item: item[1].position):
        after_values = _line_snapshot(line)
        changed = {
            field: {"before": before[line_id][field], "after": after_values[field]}
            for field in CHANGE_FIELDS
            if before[line_id][field] != after_values[field]
        }
        if changed:
            result.append(LineChange(line_id=line_id, position=line.position, changes=changed))
    return result


def _audit_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _audit(
    *,
    actor: object | None,
    action: str,
    delivery: Delivery,
    detail: dict[str, object],
) -> None:
    AuditEvent.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        target_type="inventory.Delivery",
        target_id=str(delivery.id),
        detail=detail,
    )


__all__ = [
    "CatalogRefreshResult",
    "CountRefreshResult",
    "DeliveryNotReady",
    "ImportResult",
    "InventoryWritesDisabled",
    "ReadinessResult",
    "SquarePushResult",
    "WorkbookValidationError",
    "export_delivery_xlsx",
    "import_delivery_xlsx",
    "push_delivery_to_square",
    "refresh_delivery_readiness",
    "refresh_square_catalog",
    "refresh_square_counts",
]
