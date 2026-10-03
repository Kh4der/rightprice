from decimal import Decimal

from apps.inventory.forms import (
    DeliveryPricingRulesForm,
    PricingProductRuleForm,
    SquarePriceConfirmationForm,
)
from apps.inventory.pricing import LIQUOR_PRICING_CATEGORIES


def test_pricing_rules_form_preserves_global_category_and_explicit_zero_rates():
    vodka_index = LIQUOR_PRICING_CATEGORIES.index("Vodka")
    wine_index = LIQUOR_PRICING_CATEGORIES.index("Wine")
    form = DeliveryPricingRulesForm(
        data={
            "default_markup_percent": "20",
            form_field(vodka_index): "0",
            form_field(wine_index): "30.125",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["default_markup_percent"] == Decimal("20")
    assert form.cleaned_category_rules() == {
        "Wine": "30.125",
        "Vodka": "0",
    }


def test_pricing_rules_form_builds_all_category_fields_and_restores_saved_values():
    form = DeliveryPricingRulesForm(category_rules={"Other": "12.5"})

    assert [category for category, _field in form.category_rate_fields()] == list(
        LIQUOR_PRICING_CATEGORIES
    )
    other_index = LIQUOR_PRICING_CATEGORIES.index("Other")
    assert form.initial[form_field(other_index)] == "12.5"


def test_product_rule_form_accepts_owner_category_and_explicit_zero_override():
    form = PricingProductRuleForm(
        data={
            "variation_id": "VAR-1",
            "category": "Other",
            "markup_percent": "0",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data == {
        "variation_id": "VAR-1",
        "category": "Other",
        "markup_percent": Decimal("0"),
    }


def test_pricing_forms_reject_negative_rates_and_require_explicit_confirmation():
    rules_form = DeliveryPricingRulesForm(data={"default_markup_percent": "-0.1"})
    product_form = PricingProductRuleForm(
        data={
            "variation_id": "VAR-1",
            "category": "Vodka",
            "markup_percent": "-1",
        }
    )
    confirmation = SquarePriceConfirmationForm(data={"preview_hash": "frozen-preview-hash"})

    assert not rules_form.is_valid()
    assert "default_markup_percent" in rules_form.errors
    assert not product_form.is_valid()
    assert "markup_percent" in product_form.errors
    assert not confirmation.is_valid()
    assert "confirm" in confirmation.errors


def form_field(index: int) -> str:
    return DeliveryPricingRulesForm.category_field_name(index)
