from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.accounts.models import Role, User
from apps.capture.models import Submission, SubmissionKind
from apps.inventory.models import (
    Delivery,
    DeliveryPricingPlan,
    LineMatchStatus,
    PricingPlanStatus,
)
from apps.inventory.pricing import (
    LIQUOR_PRICING_CATEGORIES,
    build_frozen_pricing_payload,
    calculate_pricing_preview,
    frozen_pricing_payload_hash,
    normalize_liquor_category,
    pricing_idempotency_key,
)


def _line(
    variation_id: str,
    *,
    cost: int | None,
    line_id: str = "line-1",
    status: str = LineMatchStatus.MATCHED,
    included: bool = True,
):
    return SimpleNamespace(
        id=line_id,
        included=included,
        match_status=status,
        square_catalog_variation_id=variation_id,
        unit_cost_cents=cost,
    )


def _variation(
    variation_id: str,
    *,
    price: int | None = 1500,
    category: str = "",
    pricing_type: str = "FIXED_PRICING",
    version: int = 7,
    location_override: bool = False,
):
    return SimpleNamespace(
        variation_id=variation_id,
        item_id=f"ITEM-{variation_id}",
        item_name=f"Product {variation_id}",
        variation_name="750 mL",
        current_price_cents=price,
        current_price_currency="USD",
        pricing_type=pricing_type,
        catalog_version=version,
        reporting_category_id=f"CAT-{variation_id}",
        reporting_category_name=category,
        category_path=[],
        price_from_location_override=location_override,
    )


def test_liquor_categories_are_stable_and_unknown_square_categories_are_not_other():
    assert LIQUOR_PRICING_CATEGORIES == (
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
    assert normalize_liquor_category("Vodka") == "Vodka"
    assert normalize_liquor_category("Spirits", ["American Whiskey"]) == (
        "Whiskey / Bourbon / Scotch"
    )
    assert normalize_liquor_category("Ginger Beer Mixers") == "Mixers / Non-alcohol"
    assert normalize_liquor_category("Other") == ""
    assert normalize_liquor_category("Seasonal products") == ""


def test_product_override_including_zero_beats_category_and_global_rules():
    variations = {
        "V-PRODUCT": _variation("V-PRODUCT", price=900, category="Vodka"),
        "V-CATEGORY": _variation("V-CATEGORY", price=900, category="Vodka"),
        "V-GLOBAL": _variation("V-GLOBAL", price=900, category="Miscellaneous"),
    }
    result = calculate_pricing_preview(
        [
            _line("V-PRODUCT", cost=1000, line_id="1"),
            _line("V-CATEGORY", cost=1000, line_id="2"),
            _line("V-GLOBAL", cost=1000, line_id="3"),
        ],
        variations,
        delivery_id="delivery-1",
        default_markup_percent="20",
        category_rules={"Vodka": "30"},
        product_overrides={"V-PRODUCT": 0},
    )

    lines = {line["variation_id"]: line for line in result.preview_lines}
    assert lines["V-PRODUCT"]["rule_source"] == "PRODUCT"
    assert lines["V-PRODUCT"]["markup_percent"] == "0"
    assert lines["V-PRODUCT"]["target_price_cents"] == 1000
    assert lines["V-CATEGORY"]["rule_source"] == "CATEGORY"
    assert lines["V-CATEGORY"]["target_price_cents"] == 1300
    assert lines["V-GLOBAL"]["rule_source"] == "GLOBAL"
    assert lines["V-GLOBAL"]["target_price_cents"] == 1200


def test_preview_rounds_half_up_and_never_lowers_square_price():
    result = calculate_pricing_preview(
        [
            _line("ROUND", cost=101, line_id="1"),
            _line("KEEP", cost=1000, line_id="2"),
        ],
        {
            "ROUND": _variation("ROUND", price=100),
            "KEEP": _variation("KEEP", price=2000, location_override=True),
        },
        default_markup_percent=Decimal("12.5"),
    )
    lines = {line["variation_id"]: line for line in result.preview_lines}
    assert lines["ROUND"]["target_price_cents"] == 114
    assert lines["ROUND"]["price_changed"] is True
    assert lines["KEEP"]["target_price_cents"] == 2000
    assert lines["KEEP"]["price_changed"] is False
    assert lines["KEEP"]["kept_existing_price"] is True
    assert lines["KEEP"]["snapshot_price_scope"] == "LOCATION_OVERRIDE"
    assert [line["variation_id"] for line in result.updates] == ["ROUND"]


def test_owner_category_assignment_enables_other_category_rule():
    result = calculate_pricing_preview(
        [_line("V1", cost=1000)],
        {"V1": _variation("V1", price=1000, category="Unsorted")},
        category_rules={"Other": "15"},
        category_assignments={"V1": "Other"},
    )

    assert result.preview_lines[0]["category"] == "Other"
    assert result.preview_lines[0]["category_source"] == "OWNER"
    assert result.preview_lines[0]["target_price_cents"] == 1150


def test_duplicate_variation_is_one_preview_but_conflicting_cost_is_an_issue():
    variation = _variation("V1", price=1000)
    same_cost = calculate_pricing_preview(
        [
            _line("V1", cost=900, line_id="1"),
            _line("V1", cost=900, line_id="2"),
        ],
        {"V1": variation},
        default_markup_percent=20,
    )
    assert len(same_cost.preview_lines) == 1
    assert same_cost.preview_lines[0]["line_ids"] == ["1", "2"]

    conflicting = calculate_pricing_preview(
        [
            _line("V1", cost=900, line_id="1"),
            _line("V1", cost=950, line_id="2"),
        ],
        {"V1": variation},
        default_markup_percent=20,
    )
    assert conflicting.preview_lines == ()
    assert [issue.code for issue in conflicting.issues] == ["conflicting_invoice_costs"]


def test_unsafe_or_incomplete_lines_are_skipped_with_clear_issues():
    result = calculate_pricing_preview(
        [
            _line("VARIABLE", cost=1000, line_id="1"),
            _line("NO-COST", cost=None, line_id="2"),
            _line("UNMATCHED", cost=1000, line_id="3", status=LineMatchStatus.UNMATCHED),
            _line("EXCLUDED", cost=1000, line_id="4", status=LineMatchStatus.EXCLUDED),
        ],
        {
            "VARIABLE": _variation("VARIABLE", pricing_type="VARIABLE_PRICING"),
            "NO-COST": _variation("NO-COST"),
        },
        default_markup_percent=20,
    )

    assert result.preview_lines == ()
    assert {issue.code for issue in result.issues} == {
        "variable_pricing",
        "invoice_cost_missing",
        "item_unmatched",
    }


def test_frozen_payload_contains_revisions_and_only_real_updates():
    preview = calculate_pricing_preview(
        [_line("CHANGE", cost=1000), _line("KEEP", cost=1000, line_id="2")],
        {
            "CHANGE": _variation("CHANGE", price=1000),
            "KEEP": _variation("KEEP", price=2000),
        },
        delivery_id="delivery-1",
        default_markup_percent=20,
    )
    payload = build_frozen_pricing_payload(
        preview,
        revision=3,
        delivery_revision=8,
        location_id="LOCATION-1",
    )
    payload_hash = frozen_pricing_payload_hash(payload)

    assert payload["plan_revision"] == 3
    assert payload["delivery_revision"] == 8
    assert [line["variation_id"] for line in payload["updates"]] == ["CHANGE"]
    assert len(payload_hash) == 64
    assert pricing_idempotency_key(
        delivery_id="delivery-1", revision=3, payload_hash=payload_hash
    ) == pricing_idempotency_key(delivery_id="delivery-1", revision=3, payload_hash=payload_hash)


@pytest.mark.django_db
def test_delivery_has_one_pricing_plan_with_safe_defaults():
    owner = User.objects.create_user(
        login_code="PRICEOWNER",
        password="long-owner-password",
        display_name="Price Owner",
        role=Role.OWNER,
    )
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=owner,
    )
    delivery = Delivery.objects.create(submission=submission)
    plan = DeliveryPricingPlan.objects.create(delivery=delivery, prepared_by=owner)

    assert delivery.pricing_plan == plan
    assert plan.status == PricingPlanStatus.DRAFT
    assert plan.revision == 1
    assert plan.default_markup_percent is None
    assert plan.category_rules == {}
    assert plan.product_overrides == {}
    assert plan.category_assignments == {}
    assert plan.preview_lines == []
    assert plan.issues == []
    assert plan.frozen_payload == {}
