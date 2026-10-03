from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.accounts.models import Role, User
from apps.capture.models import Submission, SubmissionKind, SubmissionStatus
from apps.inventory.models import (
    Delivery,
    DeliveryLine,
    DeliveryPricingPlan,
    LineMatchStatus,
    PricingPlanStatus,
    SquareCatalogVariation,
)
from apps.inventory.pricing_service import (
    PricingPlanError,
    prepare_delivery_pricing,
    push_delivery_pricing,
)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        login_code="PRICEOWNER",
        password="owner-password",
        display_name="Price Owner",
        role=Role.OWNER,
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        login_code="PRICECLERK",
        password="employee-password",
        display_name="Price Clerk",
    )


@pytest.fixture
def delivery(employee):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=employee,
    )
    return Delivery.objects.create(
        submission=submission,
        spreadsheet_revision=4,
    )


def add_priced_line(
    delivery: Delivery,
    variation_id: str,
    *,
    position: int,
    cost_cents: int = 1000,
    current_price_cents: int = 900,
    category: str = "",
    pricing_type: str = "FIXED_PRICING",
) -> DeliveryLine:
    SquareCatalogVariation.objects.create(
        variation_id=variation_id,
        item_id=f"ITEM-{variation_id}",
        item_name=f"Product {variation_id}",
        variation_name="750 mL",
        current_price_cents=current_price_cents,
        current_price_currency="USD",
        pricing_type=pricing_type,
        catalog_version=position,
        reporting_category_id=f"CATEGORY-{variation_id}",
        reporting_category_name=category,
    )
    return DeliveryLine.objects.create(
        delivery=delivery,
        position=position,
        description=f"Invoice product {variation_id}",
        unit_cost_cents=cost_cents,
        square_catalog_variation_id=variation_id,
        match_status=LineMatchStatus.MATCHED,
    )


def prepare_one_update(delivery: Delivery, owner: User):
    add_priced_line(delivery, "VAR-1", position=1)
    return prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=Decimal("20"),
        category_rules={},
        product_overrides={},
        category_assignments={},
    )


@pytest.mark.django_db
def test_prepare_applies_product_category_global_precedence_without_price_reductions(
    delivery,
    owner,
):
    add_priced_line(delivery, "PRODUCT", position=1, category="Vodka")
    add_priced_line(delivery, "CATEGORY", position=2, category="Vodka")
    add_priced_line(delivery, "GLOBAL", position=3, category="Seasonal")
    add_priced_line(
        delivery,
        "KEEP-HIGHER",
        position=4,
        current_price_cents=2500,
        category="Seasonal",
    )

    prepared = prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=Decimal("20"),
        category_rules={"Vodka": "30"},
        product_overrides={"PRODUCT": "0"},
        category_assignments={},
    )

    lines = {line["variation_id"]: line for line in prepared.preview.preview_lines}
    assert (lines["PRODUCT"]["rule_source"], lines["PRODUCT"]["markup_percent"]) == (
        "PRODUCT",
        "0",
    )
    assert lines["PRODUCT"]["target_price_cents"] == 1000
    assert (lines["CATEGORY"]["rule_source"], lines["CATEGORY"]["target_price_cents"]) == (
        "CATEGORY",
        1300,
    )
    assert (lines["GLOBAL"]["rule_source"], lines["GLOBAL"]["target_price_cents"]) == (
        "GLOBAL",
        1200,
    )
    assert lines["KEEP-HIGHER"]["target_price_cents"] == 2500
    assert lines["KEEP-HIGHER"]["price_changed"] is False
    assert lines["KEEP-HIGHER"]["kept_existing_price"] is True

    plan = prepared.plan
    assert prepared.can_push is True
    assert plan.status == PricingPlanStatus.PREVIEWED
    assert plan.product_overrides == {"PRODUCT": "0"}
    assert plan.frozen_payload["delivery_revision"] == 4
    assert {update["variation_id"] for update in plan.frozen_payload["updates"]} == {
        "PRODUCT",
        "CATEGORY",
        "GLOBAL",
    }


@pytest.mark.django_db
def test_intentional_skips_do_not_block_an_otherwise_valid_frozen_update(delivery, owner):
    add_priced_line(delivery, "CHANGE", position=1, category="Vodka")
    add_priced_line(
        delivery,
        "VARIABLE",
        position=2,
        category="Vodka",
        pricing_type="VARIABLE_PRICING",
    )
    add_priced_line(delivery, "NO-RULE", position=3, category="Seasonal")

    prepared = prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=None,
        category_rules={"Vodka": "25"},
        product_overrides={},
        category_assignments={},
    )

    assert {issue.code for issue in prepared.preview.issues} == {
        "markup_rule_missing",
        "variable_pricing",
    }
    assert prepared.blocking_issues == ()
    assert prepared.can_push is True
    assert prepared.plan.status == PricingPlanStatus.PREVIEWED
    assert [update["variation_id"] for update in prepared.plan.frozen_payload["updates"]] == [
        "CHANGE"
    ]


@pytest.mark.django_db
def test_blocking_issue_keeps_plan_draft_and_clears_any_frozen_request(delivery, owner):
    add_priced_line(delivery, "CHANGE", position=1, category="Vodka")
    first = prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=None,
        category_rules={"Vodka": "25"},
        product_overrides={},
        category_assignments={},
    )
    assert first.plan.status == PricingPlanStatus.PREVIEWED
    assert first.plan.frozen_payload

    DeliveryLine.objects.create(
        delivery=delivery,
        position=2,
        description="Unmatched invoice product",
        unit_cost_cents=1000,
        match_status=LineMatchStatus.UNMATCHED,
    )
    second = prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=None,
        category_rules={"Vodka": "25"},
        product_overrides={},
        category_assignments={},
    )

    assert [issue.code for issue in second.blocking_issues] == ["item_unmatched"]
    assert second.can_push is False
    assert second.plan.status == PricingPlanStatus.DRAFT
    assert second.plan.revision == first.plan.revision + 1
    assert second.plan.frozen_payload == {}
    assert second.plan.frozen_payload_hash == ""
    assert second.plan.idempotency_key == ""


@pytest.mark.django_db
def test_only_owner_can_prepare_or_push_prices(delivery, owner, employee):
    add_priced_line(delivery, "VAR-1", position=1)

    with pytest.raises(PricingPlanError, match="Only an owner can prepare"):
        prepare_delivery_pricing(
            delivery,
            actor=employee,
            default_markup_percent=Decimal("20"),
            category_rules={},
            product_overrides={},
            category_assignments={},
        )
    assert not DeliveryPricingPlan.objects.filter(delivery=delivery).exists()

    prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=Decimal("20"),
        category_rules={},
        product_overrides={},
        category_assignments={},
    )
    with (
        patch("apps.inventory.pricing_service.send_square_catalog_price_updates") as send,
        pytest.raises(PricingPlanError, match="Only an owner can update"),
    ):
        push_delivery_pricing(delivery, actor=employee)
    send.assert_not_called()


@pytest.mark.django_db
def test_push_requires_approved_invoice_evidence(delivery, owner):
    prepared = prepare_one_update(delivery, owner)

    with (
        patch("apps.inventory.pricing_service.send_square_catalog_price_updates") as send,
        pytest.raises(PricingPlanError, match="Approve the invoice evidence"),
    ):
        push_delivery_pricing(delivery, actor=owner)

    send.assert_not_called()
    prepared.plan.refresh_from_db()
    assert prepared.plan.status == PricingPlanStatus.PREVIEWED


@pytest.mark.django_db
def test_push_rejects_a_delivery_revision_newer_than_the_frozen_preview(delivery, owner):
    prepared = prepare_one_update(delivery, owner)
    assert prepared.plan.frozen_payload["delivery_revision"] == 4
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    delivery.spreadsheet_revision = 5
    delivery.save(update_fields=["spreadsheet_revision", "updated_at"])

    with (
        patch("apps.inventory.pricing_service.send_square_catalog_price_updates") as send,
        pytest.raises(PricingPlanError, match="invoice changed after this price preview"),
    ):
        push_delivery_pricing(delivery, actor=owner)

    send.assert_not_called()
    prepared.plan.refresh_from_db()
    assert prepared.plan.status == PricingPlanStatus.PREVIEWED


@pytest.mark.django_db
def test_failed_push_records_retryable_status_then_reuses_frozen_request(delivery, owner):
    prepared = prepare_one_update(delivery, owner)
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    original_payload = prepared.plan.frozen_payload
    original_hash = prepared.plan.frozen_payload_hash
    original_key = prepared.plan.idempotency_key
    successful_result = SimpleNamespace(
        to_dict=lambda: {
            "updated_count": 1,
            "variation_ids": ["VAR-1"],
            "idempotency_key": original_key,
        }
    )

    with patch(
        "apps.inventory.pricing_service.send_square_catalog_price_updates",
        side_effect=[RuntimeError("temporary Square timeout"), successful_result],
    ) as send:
        with pytest.raises(RuntimeError, match="temporary Square timeout"):
            push_delivery_pricing(delivery, actor=owner)

        prepared.plan.refresh_from_db()
        assert prepared.plan.status == PricingPlanStatus.FAILED
        assert prepared.plan.square_error == "temporary Square timeout"
        assert prepared.plan.frozen_payload == original_payload
        assert prepared.plan.frozen_payload_hash == original_hash
        assert prepared.plan.idempotency_key == original_key

        result = push_delivery_pricing(delivery, actor=owner)

    assert result is successful_result
    assert send.call_count == 2
    assert all(call.args[0].status == PricingPlanStatus.PUSHING for call in send.call_args_list)
    prepared.plan.refresh_from_db()
    assert prepared.plan.status == PricingPlanStatus.PUSHED
    assert prepared.plan.square_error == ""
    assert prepared.plan.pushed_by == owner
    assert prepared.plan.pushed_at is not None
    assert prepared.plan.square_result["updated_count"] == 1
    assert prepared.plan.frozen_payload == original_payload
    assert prepared.plan.frozen_payload_hash == original_hash
    assert prepared.plan.idempotency_key == original_key
