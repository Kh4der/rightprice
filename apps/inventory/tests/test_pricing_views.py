from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.capture.models import Submission, SubmissionKind, SubmissionStatus
from apps.inventory.models import (
    Delivery,
    DeliveryLine,
    DeliveryPricingPlan,
    LineMatchStatus,
    PricingPlanStatus,
    SquareCatalogVariation,
)
from apps.inventory.pricing_gateway import CatalogPriceWriteResult
from apps.inventory.pricing_service import prepare_delivery_pricing


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
        login_code="PRICEEMP",
        password="employee-password",
        display_name="Price Employee",
    )


@pytest.fixture
def delivery(employee):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.READY,
        submitted_by=employee,
    )
    return Delivery.objects.create(
        submission=submission,
        invoice_number="PRICE-100",
        spreadsheet_revision=3,
    )


def _add_priced_product(
    delivery: Delivery,
    variation_id: str,
    *,
    position: int,
    category: str = "",
    cost_cents: int = 1000,
    current_price_cents: int = 900,
) -> DeliveryLine:
    SquareCatalogVariation.objects.create(
        variation_id=variation_id,
        item_id=f"ITEM-{variation_id}",
        item_name=f"Product {variation_id}",
        variation_name="750 mL",
        current_price_cents=current_price_cents,
        current_price_currency="USD",
        pricing_type="FIXED_PRICING",
        catalog_version=position,
        reporting_category_id=f"CATEGORY-{variation_id}",
        reporting_category_name=category,
        track_inventory=True,
        present_at_location=True,
    )
    return DeliveryLine.objects.create(
        delivery=delivery,
        position=position,
        description=f"Invoice product {variation_id}",
        unit_cost_cents=cost_cents,
        received_units=Decimal("1"),
        square_catalog_variation_id=variation_id,
        square_item_name=f"Product {variation_id}",
        match_status=LineMatchStatus.MATCHED,
    )


def _preview_post_data(
    variation_ids: list[str],
    *,
    default_markup: str = "20",
    category_rates: dict[int, str] | None = None,
    categories: dict[str, str] | None = None,
    product_rates: dict[str, str] | None = None,
) -> dict[str, str]:
    data = {
        "pricing-rules-default_markup_percent": default_markup,
        "pricing-products-TOTAL_FORMS": str(len(variation_ids)),
        "pricing-products-INITIAL_FORMS": str(len(variation_ids)),
        "pricing-products-MIN_NUM_FORMS": "0",
        "pricing-products-MAX_NUM_FORMS": "1000",
    }
    for category_index, rate in (category_rates or {}).items():
        data[f"pricing-rules-category_{category_index}"] = rate
    for index, variation_id in enumerate(variation_ids):
        data[f"pricing-products-{index}-variation_id"] = variation_id
        data[f"pricing-products-{index}-category"] = (categories or {}).get(variation_id, "")
        data[f"pricing-products-{index}-markup_percent"] = (product_rates or {}).get(
            variation_id, ""
        )
    return data


def _prepare_one_price(delivery: Delivery, owner: User) -> DeliveryPricingPlan:
    _add_priced_product(delivery, "VAR-1", position=1)
    return prepare_delivery_pricing(
        delivery,
        actor=owner,
        default_markup_percent=Decimal("20"),
        category_rules={},
        product_overrides={},
        category_assignments={},
    ).plan


def _confirmation_data(plan: DeliveryPricingPlan, *, preview_hash: str | None = None):
    return {
        "pricing-confirm-preview_hash": preview_hash or plan.frozen_payload_hash,
        "pricing-confirm-confirm": "on",
    }


@pytest.mark.django_db
def test_pricing_section_is_owner_only_in_page_and_endpoint(client, owner, employee, delivery):
    _add_priced_product(delivery, "VAR-1", position=1, category="Vodka")

    client.force_login(owner)
    owner_response = client.get(reverse("inventory:delivery-detail", args=[delivery.pk]))
    assert owner_response.status_code == 200
    assert b"Set Square selling prices" in owner_response.content
    assert b"Preview new prices" in owner_response.content

    client.force_login(employee)
    employee_response = client.get(reverse("inventory:delivery-detail", args=[delivery.pk]))
    assert employee_response.status_code == 200
    assert b"Set Square selling prices" not in employee_response.content
    assert (
        client.post(
            reverse("inventory:preview-prices", args=[delivery.pk]),
            _preview_post_data(["VAR-1"]),
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_preview_applies_product_category_global_precedence_without_square_call(
    client,
    owner,
    delivery,
):
    _add_priced_product(delivery, "GLOBAL", position=1, category="Seasonal")
    _add_priced_product(delivery, "CATEGORY", position=2, category="Vodka")
    _add_priced_product(delivery, "PRODUCT", position=3, category="Gin")
    client.force_login(owner)

    with patch("apps.inventory.pricing_service.send_square_catalog_price_updates") as send:
        response = client.post(
            reverse("inventory:preview-prices", args=[delivery.pk]),
            _preview_post_data(
                ["GLOBAL", "CATEGORY", "PRODUCT"],
                default_markup="20",
                # Vodka is the third canonical category and therefore category_2.
                category_rates={2: "30"},
                product_rates={"PRODUCT": "40"},
            ),
            follow=True,
        )

    assert response.status_code == 200
    send.assert_not_called()
    plan = DeliveryPricingPlan.objects.get(delivery=delivery)
    rows = {row["variation_id"]: row for row in plan.preview_lines}
    assert (rows["GLOBAL"]["rule_source"], rows["GLOBAL"]["target_price_cents"]) == (
        "GLOBAL",
        1200,
    )
    assert (
        rows["CATEGORY"]["rule_source"],
        rows["CATEGORY"]["target_price_cents"],
    ) == ("CATEGORY", 1300)
    assert (
        rows["PRODUCT"]["rule_source"],
        rows["PRODUCT"]["target_price_cents"],
    ) == ("PRODUCT", 1400)
    assert plan.category_rules == {"Vodka": "30"}
    assert plan.product_overrides == {"PRODUCT": "40"}
    assert b"Preview ready for 3 prices" in response.content
    event = AuditEvent.objects.get(action="inventory.pricing_preview_saved")
    assert event.actor == owner
    assert event.detail["change_count"] == 3


@pytest.mark.django_db
def test_preview_rejects_tampered_or_missing_invoice_product_ids(client, owner, delivery):
    _add_priced_product(delivery, "VAR-1", position=1)
    client.force_login(owner)

    response = client.post(
        reverse("inventory:preview-prices", args=[delivery.pk]),
        _preview_post_data(["NOT-ON-INVOICE"]),
        follow=True,
    )

    assert response.status_code == 200
    assert b"invoice products changed while this form was open" in response.content
    assert not DeliveryPricingPlan.objects.filter(delivery=delivery).exists()
    assert not AuditEvent.objects.filter(action="inventory.pricing_preview_saved").exists()


@pytest.mark.django_db
def test_price_push_requires_approved_invoice_and_never_calls_square_early(
    client,
    owner,
    delivery,
    settings,
):
    plan = _prepare_one_price(delivery, owner)
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = True
    client.force_login(owner)

    with patch("apps.inventory.pricing_service.send_square_catalog_price_updates") as send:
        response = client.post(
            reverse("inventory:push-prices", args=[delivery.pk]),
            _confirmation_data(plan),
            follow=True,
        )

    assert response.status_code == 200
    send.assert_not_called()
    assert b"Approve the invoice evidence before updating Square prices" in response.content
    plan.refresh_from_db()
    assert plan.status == PricingPlanStatus.PREVIEWED
    assert AuditEvent.objects.filter(action="inventory.square_price_update_blocked").exists()


@pytest.mark.django_db
def test_price_push_uses_its_independent_write_gate(client, owner, delivery, settings):
    plan = _prepare_one_price(delivery, owner)
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = False
    client.force_login(owner)

    with patch("apps.inventory.views.push_delivery_pricing") as push:
        response = client.post(
            reverse("inventory:push-prices", args=[delivery.pk]),
            _confirmation_data(plan),
            follow=True,
        )

    assert response.status_code == 200
    push.assert_not_called()
    assert b"Square selling-price updates are locked by configuration" in response.content
    assert not AuditEvent.objects.filter(action="inventory.square_price_update_requested").exists()


@pytest.mark.django_db
def test_price_push_rejects_stale_preview_hash_before_service_call(
    client,
    owner,
    delivery,
    settings,
):
    plan = _prepare_one_price(delivery, owner)
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = True
    client.force_login(owner)

    with patch("apps.inventory.views.push_delivery_pricing") as push:
        response = client.post(
            reverse("inventory:push-prices", args=[delivery.pk]),
            _confirmation_data(plan, preview_hash="stale-preview-hash"),
            follow=True,
        )

    assert response.status_code == 200
    push.assert_not_called()
    assert b"This price preview changed" in response.content
    assert not AuditEvent.objects.filter(action="inventory.square_price_update_requested").exists()


@pytest.mark.django_db
def test_successful_price_push_works_when_inventory_gate_is_off_and_is_audited(
    client,
    owner,
    delivery,
    settings,
):
    plan = _prepare_one_price(delivery, owner)
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    settings.SQUARE_INVENTORY_WRITES_ENABLED = False
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = True
    result = CatalogPriceWriteResult(
        plan_id=str(plan.pk),
        updated_count=1,
        variation_ids=("VAR-1",),
        idempotency_key=plan.idempotency_key,
        response={"objects": [{"id": "VAR-1"}]},
    )
    client.force_login(owner)

    with patch(
        "apps.inventory.pricing_service.send_square_catalog_price_updates",
        return_value=result,
    ) as send:
        response = client.post(
            reverse("inventory:push-prices", args=[delivery.pk]),
            _confirmation_data(plan),
            follow=True,
        )

    assert response.status_code == 200
    send.assert_called_once()
    assert b"Updated 1 Square selling price" in response.content
    plan.refresh_from_db()
    assert plan.status == PricingPlanStatus.PUSHED
    assert plan.pushed_by == owner
    assert plan.square_result["updated_count"] == 1
    requested = AuditEvent.objects.get(action="inventory.square_price_update_requested")
    succeeded = AuditEvent.objects.get(action="inventory.square_price_update_succeeded")
    assert requested.actor == owner
    assert requested.detail["change_count"] == 1
    assert succeeded.actor == owner
    assert succeeded.detail["updated_count"] == 1
