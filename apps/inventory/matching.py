"""Conservative normalization and catalogue matching for delivery lines.

Automatic matching is intentionally limited to identifiers that are exact and
already reviewed.  Description similarity is useful for search UI, but it is
not strong enough to decide which Square variation receives real inventory.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from django.db import transaction
from django.db.models import Q

from .models import (
    CatalogMapping,
    Delivery,
    DeliveryLine,
    LineMatchStatus,
    SquareCatalogVariation,
)
from .packs import MAX_UNITS_PER_CASE, PackError, is_non_stock_line, parse_pack


def normalize_upc(value: object) -> str:
    """Return the digits in a UPC, preserving meaningful leading zeroes."""

    if value is None:
        return ""
    return "".join(character for character in str(value).strip() if character.isdigit())


def normalize_vendor_sku(value: object) -> str:
    """Vendor SKUs are exact identifiers; trim surrounding OCR whitespace only."""

    if value is None:
        return ""
    return str(value).strip()


def normalize_description(value: object) -> str:
    """Collapse OCR whitespace without changing punctuation or product wording."""

    if value is None:
        return ""
    return " ".join(str(value).split())


_SIZE_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(ml|cl|l|oz)\b", re.IGNORECASE)


def nearest_catalog_matches(line: DeliveryLine, *, limit: int = 5) -> list[SquareCatalogVariation]:
    """Rank review suggestions without ever authorizing an inventory match."""

    description = normalize_description(line.description).casefold()
    if not description or limit <= 0:
        return []
    source_tokens = _name_tokens(description)
    # Distributor invoices often put the bottle size only in a separate pack
    # column (for example ``12/750ML``).  Include that evidence when filtering
    # suggestions, but keep pack counts out of the name-similarity score.
    source_sizes = _sizes_in_ml(
        f"{description} {normalize_description(line.pack_text).casefold()}"
    )
    ranked: list[tuple[float, str, str, str, SquareCatalogVariation]] = []
    for variation in _usable_catalog().order_by("variation_id"):
        label = normalize_description(
            " ".join(part for part in (variation.item_name, variation.variation_name) if part)
        ).casefold()
        candidate_sizes = _sizes_in_ml(label)
        # A 750 ml invoice line must not suggest the otherwise-similar 1.75 L
        # variation. Missing size is allowed, but an explicit conflict is not.
        if source_sizes and candidate_sizes and source_sizes.isdisjoint(candidate_sizes):
            continue

        reason = "Similar product name"
        if line.upc and line.upc in {variation.upc, variation.gtin}:
            score = 1.0
            reason = "Exact barcode"
        elif line.vendor_sku and line.vendor_sku == variation.sku:
            score = 0.98
            reason = "Exact SKU"
        else:
            candidate_tokens = _name_tokens(label)
            union = source_tokens | candidate_tokens
            overlap = len(source_tokens & candidate_tokens) / len(union) if union else 0.0
            ratio = SequenceMatcher(None, description, label).ratio()
            score = (0.55 * ratio) + (0.45 * overlap)
            if source_sizes and source_sizes == candidate_sizes:
                score = min(0.96, score + 0.08)
                reason = "Similar name and same bottle size"
        if score < 0.25:
            continue
        ranked.append((score, label, variation.variation_id, reason, variation))

    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    suggestions: list[SquareCatalogVariation] = []
    for score, _label, _variation_id, reason, variation in ranked[:limit]:
        # These are transient display-only attributes, never persisted.
        variation.suggestion_score = round(score * 100)
        variation.suggestion_reason = reason
        suggestions.append(variation)
    return suggestions


def _name_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in {"ml", "cl", "l", "oz", "bottle", "case"}
    }


def _sizes_in_ml(value: str) -> set[int]:
    units = {"ml": Decimal(1), "cl": Decimal(10), "l": Decimal(1000), "oz": Decimal("29.5735")}
    return {
        int((Decimal(amount) * units[unit.casefold()]).quantize(Decimal("1")))
        for amount, unit in _SIZE_PATTERN.findall(value)
    }


@dataclass(frozen=True)
class LineIssue:
    line_id: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MatchingResult:
    delivery_id: str
    matched: int
    excluded: int
    unresolved: int
    issues: tuple[LineIssue, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["issues"] = [issue.to_dict() for issue in self.issues]
        return result


def line_readiness_issues(line: DeliveryLine) -> list[LineIssue]:
    """Explain every reason a stock line cannot safely be sent to Square."""

    if not line.included or line.match_status == LineMatchStatus.EXCLUDED:
        return []

    issues: list[LineIssue] = []
    line_id = str(line.id)
    if line.match_status != LineMatchStatus.MATCHED:
        issues.append(LineIssue(line_id, "item_unmatched", "Choose a reviewed Square item."))
    if not line.square_catalog_variation_id:
        issues.append(LineIssue(line_id, "variation_missing", "Square variation ID is required."))
    if line.units_per_case is None or line.units_per_case <= 0:
        issues.append(
            LineIssue(line_id, "pack_missing", "Enter a verified number of units per case.")
        )
    if line.received_units is None:
        issues.append(LineIssue(line_id, "quantity_missing", "Received units are required."))
    elif line.received_units <= 0:
        issues.append(
            LineIssue(
                line_id,
                "quantity_not_positive",
                "A delivery adjustment must add more than zero units.",
            )
        )
    elif line.received_units != line.received_units.to_integral_value():
        issues.append(
            LineIssue(
                line_id,
                "fractional_units",
                "Sellable liquor inventory must be a whole number of units.",
            )
        )
    if line.square_count_before is None or line.projected_count_after is None:
        issues.append(
            LineIssue(
                line_id,
                "square_count_missing",
                "Refresh the current Square count before approving this delivery.",
            )
        )
    elif (
        not line.square_count_snapshot_at
        or line.square_count_variation_id != line.square_catalog_variation_id
    ):
        issues.append(
            LineIssue(
                line_id,
                "square_count_stale",
                "The Square count snapshot is for an older item selection; refresh it.",
            )
        )

    if (
        line.cases is not None
        and line.units_per_case is not None
        and line.received_units is not None
        and line.cases * line.units_per_case != line.received_units
    ):
        issues.append(
            LineIssue(
                line_id,
                "unit_math_mismatch",
                "Received units must equal cases multiplied by units per case.",
            )
        )
    return issues


def _usable_catalog() -> Any:
    return SquareCatalogVariation.objects.filter(
        track_inventory=True,
        present_at_location=True,
    )


def _one_catalog_match(
    queryset: Any,
    *,
    line: DeliveryLine,
    duplicate_code: str,
    duplicate_message: str,
) -> tuple[SquareCatalogVariation | None, list[LineIssue]]:
    matches = list(queryset.order_by("variation_id")[:2])
    if len(matches) > 1:
        return None, [LineIssue(str(line.id), duplicate_code, duplicate_message)]
    return (matches[0] if matches else None), []


def _identifier_match(
    line: DeliveryLine,
) -> tuple[SquareCatalogVariation | None, str | None, list[LineIssue]]:
    """Match in safety order: UPC, reviewed vendor SKU, then Square SKU."""

    line_id = str(line.id)
    if line.upc:
        candidate, issues = _one_catalog_match(
            _usable_catalog().filter(Q(upc=line.upc) | Q(gtin=line.upc)),
            line=line,
            duplicate_code="duplicate_square_upc",
            duplicate_message=f"UPC {line.upc} matches more than one Square variation.",
        )
        if issues or candidate:
            return candidate, "square_upc" if candidate else None, issues

    if line.delivery.vendor_id and line.vendor_sku:
        mappings = list(
            CatalogMapping.objects.filter(
                vendor_id=line.delivery.vendor_id,
                vendor_sku=line.vendor_sku,
            ).order_by("pk")[:2]
        )
        if len(mappings) > 1:
            return (
                None,
                None,
                [
                    LineIssue(
                        line_id,
                        "duplicate_vendor_sku_mapping",
                        f"Vendor SKU {line.vendor_sku} has more than one reviewed mapping.",
                    )
                ],
            )
        if mappings:
            try:
                candidate = _usable_catalog().get(
                    variation_id=mappings[0].square_catalog_variation_id
                )
            except SquareCatalogVariation.DoesNotExist:
                return (
                    None,
                    None,
                    [
                        LineIssue(
                            line_id,
                            "reviewed_mapping_not_in_catalog",
                            "The reviewed vendor mapping is not a tracked item at this location.",
                        )
                    ],
                )
            return candidate, "reviewed_vendor_sku", []

    if line.vendor_sku:
        candidate, issues = _one_catalog_match(
            _usable_catalog().filter(sku=line.vendor_sku),
            line=line,
            duplicate_code="duplicate_square_sku",
            duplicate_message=(
                f"Square SKU {line.vendor_sku} appears on more than one tracked variation."
            ),
        )
        if issues or candidate:
            return candidate, "square_sku" if candidate else None, issues

    return None, None, []


def _catalog_by_id(
    line: DeliveryLine,
) -> tuple[SquareCatalogVariation | None, list[LineIssue]]:
    if not line.square_catalog_variation_id:
        return None, []
    try:
        return (
            _usable_catalog().get(variation_id=line.square_catalog_variation_id),
            [],
        )
    except SquareCatalogVariation.DoesNotExist:
        return None, [
            LineIssue(
                str(line.id),
                "variation_not_in_catalog",
                "The selected variation is not a tracked Square item at this location.",
            )
        ]


def _name_suggestion(line: DeliveryLine) -> SquareCatalogVariation | None:
    description = normalize_description(line.description).casefold()
    if not description:
        return None
    matches: list[SquareCatalogVariation] = []
    for variation in _usable_catalog().only(
        "variation_id", "item_name", "variation_name", "sku", "upc", "gtin"
    ):
        labels = {
            normalize_description(variation.item_name).casefold(),
            normalize_description(variation.variation_name).casefold(),
            normalize_description(
                " ".join(part for part in (variation.item_name, variation.variation_name) if part)
            ).casefold(),
        }
        if description in labels:
            matches.append(variation)
            if len(matches) > 1:
                return None
    return matches[0] if matches else None


def _catalog_label(variation: SquareCatalogVariation) -> str:
    return " - ".join(part for part in (variation.item_name, variation.variation_name) if part)


def _set_auto_note(line: DeliveryLine, message: str | None) -> None:
    prefix = "[automatic review] "
    if message:
        if not line.review_note or line.review_note.startswith(prefix):
            line.review_note = f"{prefix}{message}"[:300]
    elif line.review_note.startswith(prefix):
        line.review_note = ""


def _clear_square_cost_snapshot(line: DeliveryLine) -> None:
    line.square_unit_cost_cents = None
    line.square_unit_cost_currency = ""
    line.square_unit_cost_source = ""


def _apply_square_cost_snapshot(
    line: DeliveryLine,
    variation: SquareCatalogVariation,
) -> None:
    vendor_id = line.delivery.vendor.square_vendor_id if line.delivery.vendor_id else ""
    amount, currency, source = variation.cost_snapshot_for_vendor(vendor_id)
    line.square_unit_cost_cents = amount
    line.square_unit_cost_currency = currency
    line.square_unit_cost_source = source


def _normalize_one(line: DeliveryLine) -> list[LineIssue]:
    line.vendor_sku = normalize_vendor_sku(line.vendor_sku)
    line.upc = normalize_upc(line.upc)
    line.description = normalize_description(line.description)
    line.pack_text = line.pack_text.strip()
    line.square_catalog_variation_id = line.square_catalog_variation_id.strip()
    line.square_item_name = normalize_description(line.square_item_name)
    _clear_square_cost_snapshot(line)

    if is_non_stock_line(line.description) or not line.included:
        line.included = False
        line.match_status = LineMatchStatus.EXCLUDED
        line.square_catalog_variation_id = ""
        line.square_item_name = ""
        _set_auto_note(line, None)
        return []

    candidate, source, identifier_issues = _identifier_match(line)
    if identifier_issues:
        line.match_status = LineMatchStatus.UNMATCHED
        _set_auto_note(line, identifier_issues[0].message)
        return identifier_issues

    # Exact Square identifiers and reviewed vendor mappings may match
    # automatically. A name can only suggest; the workbook reviewer must change
    # SUGGESTED to MATCHED before it becomes postable.
    if (
        candidate
        and line.square_catalog_variation_id
        and candidate.variation_id != line.square_catalog_variation_id
    ):
        issue = LineIssue(
            str(line.id),
            "manual_match_conflict",
            "The selected variation conflicts with an exact Square identifier.",
        )
        line.match_status = LineMatchStatus.UNMATCHED
        _set_auto_note(line, issue.message)
        return [issue]
    if candidate:
        line.square_catalog_variation_id = candidate.variation_id
        line.square_item_name = _catalog_label(candidate)
        line.match_status = LineMatchStatus.MATCHED
        if source == "reviewed_vendor_sku":
            reviewed = CatalogMapping.objects.get(
                vendor_id=line.delivery.vendor_id,
                vendor_sku=line.vendor_sku,
            )
            line.units_per_case = reviewed.units_per_case
    elif line.square_catalog_variation_id:
        selected, selected_issues = _catalog_by_id(line)
        if selected_issues:
            line.match_status = LineMatchStatus.UNMATCHED
            _set_auto_note(line, selected_issues[0].message)
            return selected_issues
        if line.match_status == LineMatchStatus.MATCHED:
            line.square_item_name = _catalog_label(selected)
        elif line.match_status != LineMatchStatus.SUGGESTED:
            line.match_status = LineMatchStatus.UNMATCHED
    else:
        suggestion = _name_suggestion(line)
        if suggestion:
            line.square_catalog_variation_id = suggestion.variation_id
            line.square_item_name = _catalog_label(suggestion)
            line.match_status = LineMatchStatus.SUGGESTED

    issues: list[LineIssue] = []
    if line.units_per_case is None:
        try:
            line.units_per_case = parse_pack(line.pack_text).units_per_case
        except PackError as exc:
            issues.append(LineIssue(str(line.id), "pack_refused", str(exc)))
    elif not 1 <= line.units_per_case <= MAX_UNITS_PER_CASE:
        issues.append(
            LineIssue(
                str(line.id),
                "pack_out_of_range",
                f"Units per case must be between 1 and {MAX_UNITS_PER_CASE}.",
            )
        )

    if line.cases is not None and line.units_per_case is not None:
        calculated = line.cases * Decimal(line.units_per_case)
        if line.received_units is None:
            line.received_units = calculated
        elif line.received_units != calculated:
            issues.append(
                LineIssue(
                    str(line.id),
                    "unit_math_mismatch",
                    "Received units do not equal cases multiplied by units per case.",
                )
            )

    if line.match_status == LineMatchStatus.SUGGESTED:
        issues.append(
            LineIssue(
                str(line.id),
                "name_match_needs_review",
                "A name-only Square match was suggested; approve or replace it.",
            )
        )
    elif line.match_status != LineMatchStatus.MATCHED:
        line.match_status = LineMatchStatus.UNMATCHED
        issues.append(
            LineIssue(str(line.id), "item_unmatched", "No exact stored Square match was found.")
        )

    if line.match_status == LineMatchStatus.MATCHED:
        matched_variation = candidate
        if matched_variation is None and line.square_catalog_variation_id:
            matched_variation, _ = _catalog_by_id(line)
        if matched_variation is not None:
            _apply_square_cost_snapshot(line, matched_variation)

    _set_auto_note(line, issues[0].message if issues else None)
    return issues


@transaction.atomic
def normalize_delivery_lines(delivery: Delivery) -> MatchingResult:
    """Normalize and exactly match every line, returning reviewable refusals."""

    lines = list(delivery.lines.select_related("delivery__vendor").order_by("position", "id"))
    issues: list[LineIssue] = []
    matched = excluded = unresolved = 0
    update_fields = [
        "vendor_sku",
        "upc",
        "description",
        "pack_text",
        "units_per_case",
        "received_units",
        "square_catalog_variation_id",
        "square_item_name",
        "match_status",
        "included",
        "review_note",
        "square_unit_cost_cents",
        "square_unit_cost_currency",
        "square_unit_cost_source",
        "square_count_before",
        "square_count_variation_id",
        "projected_count_after",
        "square_count_after",
        "square_count_drift",
        "square_count_snapshot_at",
        "square_count_verified_at",
        "updated_at",
    ]
    for line in lines:
        line_issues = _normalize_one(line)
        issues.extend(line_issues)
        line.save(update_fields=update_fields)
        if not line.included or line.match_status == LineMatchStatus.EXCLUDED:
            excluded += 1
        elif line.match_status == LineMatchStatus.MATCHED and not line_issues:
            matched += 1
        else:
            unresolved += 1

    return MatchingResult(
        delivery_id=str(delivery.id),
        matched=matched,
        excluded=excluded,
        unresolved=unresolved,
        issues=tuple(issues),
    )
