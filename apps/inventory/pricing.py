"""Pure price-preview rules for owner-reviewed delivery invoices.

The functions in this module never write to Square and never mutate a model.
They turn reviewed invoice costs plus a read-only Square catalogue snapshot into
an auditable list of proposed prices.  Keeping this calculation pure makes the
preview shown to an owner the same input that is later frozen for a safe retry.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .models import (
    Delivery,
    DeliveryLine,
    DeliveryPricingPlan,
    LineMatchStatus,
    SquareCatalogVariation,
)

LIQUOR_PRICING_CATEGORIES = (
    "Beer",
    "Wine",
    "Vodka",
    "Gin",
    "Rum",
    "Tequila / Mezcal",
    "Whiskey / Bourbon / Scotch",
    "Brandy / Cognac",
    "Liqueur / Cordial",
    "RTD / Seltzer",
    "Mixers / Non-alcohol",
    "Other",
)

_CANONICAL_CATEGORY_BY_CASEFOLD = {
    category.casefold(): category for category in LIQUOR_PRICING_CATEGORIES
}
_NON_WORDS = re.compile(r"[^a-z0-9]+")
_ONE_CENT = Decimal("1")


@dataclass(frozen=True)
class PricingIssue:
    code: str
    message: str
    variation_id: str = ""
    line_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["line_ids"] = list(self.line_ids)
        return result


@dataclass(frozen=True)
class PricingPreviewResult:
    delivery_id: str
    preview_lines: tuple[dict[str, object], ...]
    issues: tuple[PricingIssue, ...]

    @property
    def lines(self) -> tuple[dict[str, object], ...]:
        """Short alias used by presentation code."""

        return self.preview_lines

    @property
    def updates(self) -> tuple[dict[str, object], ...]:
        """Rows that would actually change Square, excluding safe no-ops."""

        return tuple(line for line in self.preview_lines if line["price_changed"])

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "preview_lines": [dict(line) for line in self.preview_lines],
            "issues": [issue.to_dict() for issue in self.issues],
        }


def normalize_liquor_category(*values: object) -> str:
    """Map clear Square category labels to the small owner-facing category set.

    Unknown Square labels intentionally return an empty string.  In particular,
    Square's generic ``Other`` category is not inferred as our ``Other`` bucket;
    that bucket must be an explicit owner assignment.
    """

    text = " ".join(_category_text_parts(values))
    normalized = f" {_NON_WORDS.sub(' ', text.casefold()).strip()} "
    if not normalized.strip():
        return ""

    # Specific phrases go before single spirit names so, for example, ginger
    # beer does not become Beer and a canned gin cocktail does not become Gin.
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "Mixers / Non-alcohol",
            (
                " non alcoholic ",
                " non alcohol ",
                " alcohol free ",
                " mixer ",
                " mixers ",
                " cocktail mix ",
                " tonic water ",
                " soda water ",
                " ginger beer ",
                " ginger ale ",
                " simple syrup ",
                " grenadine ",
            ),
        ),
        (
            "RTD / Seltzer",
            (
                " rtd ",
                " ready to drink ",
                " hard seltzer ",
                " seltzer ",
                " canned cocktail ",
                " premixed cocktail ",
                " pre mixed cocktail ",
                " wine cooler ",
            ),
        ),
        ("Tequila / Mezcal", (" tequila ", " mezcal ")),
        (
            "Whiskey / Bourbon / Scotch",
            (" whiskey ", " whisky ", " bourbon ", " scotch ", " rye whiskey "),
        ),
        ("Brandy / Cognac", (" brandy ", " cognac ", " armagnac ")),
        (
            "Liqueur / Cordial",
            (
                " liqueur ",
                " cordial ",
                " schnapps ",
                " amaretto ",
                " triple sec ",
            ),
        ),
        ("Vodka", (" vodka ",)),
        ("Gin", (" gin ",)),
        ("Rum", (" rum ",)),
        (
            "Wine",
            (
                " wine ",
                " champagne ",
                " prosecco ",
                " sparkling wine ",
                " cabernet ",
                " chardonnay ",
                " merlot ",
                " pinot ",
                " riesling ",
                " sauvignon ",
                " sangria ",
            ),
        ),
        (
            "Beer",
            (
                " beer ",
                " ale ",
                " lager ",
                " stout ",
                " pilsner ",
                " porter ",
                " hard cider ",
            ),
        ),
    )
    for category, phrases in patterns:
        if any(phrase in normalized for phrase in phrases):
            return category
    return ""


def calculate_pricing_preview(
    delivery_lines: Iterable[DeliveryLine],
    catalog_variations: Mapping[str, SquareCatalogVariation] | Iterable[SquareCatalogVariation],
    *,
    delivery_id: str = "",
    default_markup_percent: Decimal | int | str | None = None,
    category_rules: Mapping[str, object] | None = None,
    product_overrides: Mapping[str, object] | None = None,
    category_assignments: Mapping[str, object] | None = None,
) -> PricingPreviewResult:
    """Calculate a deduplicated and auditable retail-price preview.

    Rule precedence is product override (including an explicit zero), category,
    then the global rate.  Proposed prices are based on invoice unit cost and
    are clamped to the current Square price, so this workflow can never lower a
    price.
    """

    variations = _variation_map(catalog_variations)
    rules = category_rules if isinstance(category_rules, Mapping) else {}
    overrides = product_overrides if isinstance(product_overrides, Mapping) else {}
    assignments = category_assignments if isinstance(category_assignments, Mapping) else {}
    issues: list[PricingIssue] = []
    grouped: dict[str, list[DeliveryLine]] = {}

    if category_rules is not None and not isinstance(category_rules, Mapping):
        issues.append(PricingIssue("invalid_category_rules", "Category rules are not valid."))
    if product_overrides is not None and not isinstance(product_overrides, Mapping):
        issues.append(PricingIssue("invalid_product_overrides", "Product rules are not valid."))
    if category_assignments is not None and not isinstance(category_assignments, Mapping):
        issues.append(
            PricingIssue("invalid_category_assignments", "Category assignments are not valid.")
        )

    for line in delivery_lines:
        if not bool(getattr(line, "included", True)):
            continue
        status = str(getattr(line, "match_status", "") or "")
        if status == LineMatchStatus.EXCLUDED:
            continue
        line_id = _line_id(line)
        variation_id = str(getattr(line, "square_catalog_variation_id", "") or "").strip()
        if status != LineMatchStatus.MATCHED or not variation_id:
            issues.append(
                PricingIssue(
                    "item_unmatched",
                    "Choose a reviewed Square item before setting its price.",
                    variation_id=variation_id,
                    line_ids=(line_id,) if line_id else (),
                )
            )
            continue
        grouped.setdefault(variation_id, []).append(line)

    preview_lines: list[dict[str, object]] = []
    for variation_id, lines in grouped.items():
        line_ids = tuple(filter(None, (_line_id(line) for line in lines)))
        usable_costs: set[int] = set()
        for line in lines:
            raw_cost = getattr(line, "unit_cost_cents", None)
            if isinstance(raw_cost, bool) or not isinstance(raw_cost, int) or raw_cost <= 0:
                issues.append(
                    PricingIssue(
                        "invoice_cost_missing",
                        "Enter a positive invoice unit cost for this product.",
                        variation_id=variation_id,
                        line_ids=(_line_id(line),) if _line_id(line) else (),
                    )
                )
                continue
            usable_costs.add(raw_cost)
        if not usable_costs:
            continue
        if len(usable_costs) > 1:
            issues.append(
                PricingIssue(
                    "conflicting_invoice_costs",
                    "The same Square product has different invoice costs. Review those lines first.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue
        invoice_cost_cents = next(iter(usable_costs))

        variation = variations.get(variation_id)
        if variation is None:
            issues.append(
                PricingIssue(
                    "catalog_product_missing",
                    "Refresh Square products before setting this price.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue

        pricing_type = str(getattr(variation, "pricing_type", "") or "").upper()
        if pricing_type == "VARIABLE_PRICING":
            issues.append(
                PricingIssue(
                    "variable_pricing",
                    "This Square product uses variable pricing and was skipped.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue
        if pricing_type != "FIXED_PRICING":
            issues.append(
                PricingIssue(
                    "pricing_type_missing",
                    "Refresh this Square product so its pricing type can be verified.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue

        current_price_cents = getattr(variation, "current_price_cents", None)
        if (
            isinstance(current_price_cents, bool)
            or not isinstance(current_price_cents, int)
            or current_price_cents < 0
        ):
            issues.append(
                PricingIssue(
                    "current_price_missing",
                    "Refresh or enter the current Square price before applying a markup.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue

        category, category_source, category_issue = _category_for_variation(
            variation,
            assignments,
        )
        if category_issue:
            issues.append(
                PricingIssue(
                    "invalid_category_assignment",
                    category_issue,
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )

        raw_rate, rule_source, has_rule = _rate_for_variation(
            variation_id=variation_id,
            category=category,
            default_markup_percent=default_markup_percent,
            category_rules=rules,
            product_overrides=overrides,
        )
        if not has_rule:
            issues.append(
                PricingIssue(
                    "markup_rule_missing",
                    "Set a product, category, or all-products percentage for this item.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue
        rate = _valid_markup_percent(raw_rate)
        if rate is None:
            issues.append(
                PricingIssue(
                    "invalid_markup_percent",
                    "The markup percentage must be zero or greater.",
                    variation_id=variation_id,
                    line_ids=line_ids,
                )
            )
            continue

        calculated_price = (
            Decimal(invoice_cost_cents) * (Decimal("1") + rate / Decimal("100"))
        ).quantize(_ONE_CENT, rounding=ROUND_HALF_UP)
        target_price_cents = max(current_price_cents, int(calculated_price))
        price_changed = target_price_cents > current_price_cents
        catalog_version = getattr(variation, "catalog_version", None)
        snapshot_version = (
            catalog_version
            if isinstance(catalog_version, int) and not isinstance(catalog_version, bool)
            else 0
        )
        preview_lines.append(
            {
                "variation_id": variation_id,
                "item_id": str(getattr(variation, "item_id", "") or ""),
                "item_name": str(getattr(variation, "item_name", "") or ""),
                "variation_name": str(getattr(variation, "variation_name", "") or ""),
                "category": category,
                "category_source": category_source,
                "rule_source": rule_source,
                "markup_percent": _decimal_string(rate),
                "invoice_unit_cost_cents": invoice_cost_cents,
                "current_price_cents": current_price_cents,
                "target_price_cents": target_price_cents,
                "price_changed": price_changed,
                "kept_existing_price": not price_changed
                and int(calculated_price) < current_price_cents,
                "price_currency": str(
                    getattr(variation, "current_price_currency", "") or ""
                ).upper()[:3],
                "snapshot_version": snapshot_version,
                "snapshot_pricing_type": "FIXED_PRICING",
                "snapshot_price_cents": current_price_cents,
                "snapshot_price_scope": (
                    "LOCATION_OVERRIDE"
                    if bool(getattr(variation, "price_from_location_override", False))
                    else "GLOBAL"
                ),
                "line_ids": list(line_ids),
            }
        )

    return PricingPreviewResult(
        delivery_id=str(delivery_id or ""),
        preview_lines=tuple(preview_lines),
        issues=tuple(issues),
    )


def preview_delivery_pricing(
    delivery: Delivery,
    *,
    default_markup_percent: Decimal | int | str | None = None,
    category_rules: Mapping[str, object] | None = None,
    product_overrides: Mapping[str, object] | None = None,
    category_assignments: Mapping[str, object] | None = None,
    catalog_variations: Mapping[str, SquareCatalogVariation]
    | Iterable[SquareCatalogVariation]
    | None = None,
) -> PricingPreviewResult:
    """Read a delivery and its catalogue snapshots, then run the pure preview."""

    lines = list(delivery.lines.order_by("position", "id"))
    if catalog_variations is None:
        variation_ids = {
            str(line.square_catalog_variation_id)
            for line in lines
            if line.square_catalog_variation_id
        }
        catalog_variations = SquareCatalogVariation.objects.in_bulk(variation_ids)
    return calculate_pricing_preview(
        lines,
        catalog_variations,
        delivery_id=str(delivery.pk),
        default_markup_percent=default_markup_percent,
        category_rules=category_rules,
        product_overrides=product_overrides,
        category_assignments=category_assignments,
    )


def preview_pricing_plan(
    plan: DeliveryPricingPlan,
    *,
    catalog_variations: Mapping[str, SquareCatalogVariation]
    | Iterable[SquareCatalogVariation]
    | None = None,
) -> PricingPreviewResult:
    """Calculate a preview directly from one plan's currently editable rules."""

    return preview_delivery_pricing(
        plan.delivery,
        default_markup_percent=plan.default_markup_percent,
        category_rules=plan.category_rules,
        product_overrides=plan.product_overrides,
        category_assignments=plan.category_assignments,
        catalog_variations=catalog_variations,
    )


def build_frozen_pricing_payload(
    preview: PricingPreviewResult,
    *,
    revision: int,
    delivery_revision: int,
    location_id: str,
) -> dict[str, object]:
    """Return the canonical payload shape consumed by the Square writer."""

    return {
        "schema_version": 1,
        "delivery_id": preview.delivery_id,
        "plan_revision": int(revision),
        "delivery_revision": int(delivery_revision),
        "location_id": str(location_id),
        "updates": [dict(line) for line in preview.updates],
    }


def frozen_pricing_payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pricing_idempotency_key(*, delivery_id: str, revision: int, payload_hash: str) -> str:
    """Create a stable, Square-safe key for retries of one frozen preview."""

    digest = hashlib.sha256(f"{delivery_id}:{int(revision)}:{payload_hash}".encode()).hexdigest()
    return f"price-{digest[:48]}"


def _variation_map(
    variations: Mapping[str, SquareCatalogVariation] | Iterable[SquareCatalogVariation],
) -> dict[str, SquareCatalogVariation]:
    if isinstance(variations, Mapping):
        return {str(key): value for key, value in variations.items()}
    return {
        str(getattr(variation, "variation_id", "")): variation
        for variation in variations
        if getattr(variation, "variation_id", "")
    }


def _category_text_parts(values: Iterable[object]) -> Iterable[str]:
    for value in values:
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for key in ("name", "category_name"):
                if value.get(key):
                    yield str(value[key])
        elif isinstance(value, Iterable):
            yield from _category_text_parts(value)


def _explicit_category(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _CANONICAL_CATEGORY_BY_CASEFOLD.get(value.strip().casefold(), "")


def _category_for_variation(
    variation: SquareCatalogVariation,
    assignments: Mapping[str, object],
) -> tuple[str, str, str]:
    lookup_keys = (
        str(getattr(variation, "variation_id", "") or ""),
        str(getattr(variation, "item_id", "") or ""),
        str(getattr(variation, "reporting_category_id", "") or ""),
    )
    for key in lookup_keys:
        if not key or key not in assignments:
            continue
        raw_category = assignments[key]
        if raw_category in (None, ""):
            break
        category = _explicit_category(raw_category)
        if category:
            return category, "OWNER", ""
        return (
            "",
            "OWNER",
            "Choose one of the available liquor categories for this product.",
        )

    category = normalize_liquor_category(
        getattr(variation, "reporting_category_name", ""),
        getattr(variation, "category_path", ()),
    )
    return category, "SQUARE" if category else "", ""


def _rate_for_variation(
    *,
    variation_id: str,
    category: str,
    default_markup_percent: object,
    category_rules: Mapping[str, object],
    product_overrides: Mapping[str, object],
) -> tuple[object, str, bool]:
    if variation_id in product_overrides:
        return product_overrides[variation_id], "PRODUCT", True

    category_key = next(
        (
            key
            for key in category_rules
            if isinstance(key, str) and key.casefold() == category.casefold()
        ),
        None,
    )
    if category and category_key is not None:
        return category_rules[category_key], "CATEGORY", True
    if default_markup_percent is not None:
        return default_markup_percent, "GLOBAL", True
    return None, "", False


def _valid_markup_percent(value: object) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not rate.is_finite() or rate < 0:
        return None
    return rate


def _line_id(line: DeliveryLine) -> str:
    return str(getattr(line, "id", "") or "")


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
