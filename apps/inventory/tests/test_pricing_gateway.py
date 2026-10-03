from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from apps.accounts.models import Role, User
from apps.capture.models import Submission, SubmissionKind
from apps.inventory.models import Delivery, PricingPlanStatus
from apps.inventory.pricing import frozen_pricing_payload_hash
from apps.inventory.pricing_gateway import (
    CatalogPriceWriteRequest,
    CatalogPriceWritesDisabled,
    PricingPlanDrift,
    PricingPlanNotReady,
    build_catalog_price_write,
    send_square_catalog_price_updates,
)


class ModelObject:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **_kwargs):
        return copy.deepcopy(self.value)


class FakeCatalog:
    def __init__(self, objects, *, model_response=False):
        self.objects = list(objects)
        self.model_response = model_response
        self.get_calls = []
        self.upsert_calls = []

    def batch_get(self, **kwargs):
        self.get_calls.append(kwargs)
        response = {"objects": copy.deepcopy(self.objects)}
        return ModelObject(response) if self.model_response else response

    def batch_upsert(self, **kwargs):
        self.upsert_calls.append(copy.deepcopy(kwargs))
        return {"objects": copy.deepcopy(kwargs["batches"][0]["objects"])}


class FakeSquare:
    def __init__(self, objects, *, model_response=False):
        self.catalog = FakeCatalog(objects, model_response=model_response)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "PRICEOWNER",
        "1234",
        display_name="Price Owner",
        role=Role.OWNER,
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "PRICEEMP",
        "1234",
        display_name="Price Employee",
        role=Role.EMPLOYEE,
    )


@pytest.fixture
def delivery(db, owner):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=owner,
    )
    return Delivery.objects.create(submission=submission, spreadsheet_revision=3)


@pytest.fixture(autouse=True)
def square_settings(settings):
    settings.SQUARE_LOCATION_ID = "LOCATION-1"
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = True


def _catalog_object(
    variation_id="VAR-1",
    *,
    price=1000,
    version=7,
    pricing_type="FIXED_PRICING",
    override_price=None,
):
    data = {
        "item_id": "ITEM-1",
        "name": "750 ml",
        "pricing_type": pricing_type,
        "price_money": {"amount": price, "currency": "USD"},
        "sku": "SKU-1",
        "upc": "012345678905",
        "track_inventory": True,
        "location_overrides": [
            {
                "location_id": "OTHER-LOCATION",
                "price_money": {"amount": 999, "currency": "USD"},
                "sold_out": False,
            }
        ],
    }
    if override_price is not None:
        data["location_overrides"].append(
            {
                "location_id": "LOCATION-1",
                "pricing_type": "FIXED_PRICING",
                "price_money": {"amount": override_price, "currency": "USD"},
                "sold_out": True,
            }
        )
    return {
        "type": "ITEM_VARIATION",
        "id": variation_id,
        "updated_at": "2026-10-03T12:00:00Z",
        "version": version,
        "is_deleted": False,
        "present_at_all_locations": True,
        "custom_attribute_values": {"proof": {"number_value": "80"}},
        "item_variation_data": data,
    }


def _entry(
    variation_id="VAR-1",
    *,
    current=1000,
    target=1200,
    version=7,
    scope="GLOBAL",
):
    return {
        "variation_id": variation_id,
        "target_price_cents": target,
        "snapshot_version": version,
        "snapshot_pricing_type": "FIXED_PRICING",
        "snapshot_price_cents": current,
        "snapshot_price_scope": scope,
        "price_changed": True,
        "category": "Whiskey / Bourbon / Scotch",
        "rule_source": "category",
    }


def _plan(delivery, *entries, status=PricingPlanStatus.PREVIEWED):
    payload = {
        "schema_version": 1,
        "delivery_id": str(delivery.pk),
        "plan_revision": 4,
        "delivery_revision": delivery.spreadsheet_revision,
        "location_id": "LOCATION-1",
        "updates": list(entries),
    }
    return SimpleNamespace(
        pk="plan-1",
        delivery=delivery,
        status=status,
        revision=4,
        frozen_payload=payload,
        frozen_payload_hash=frozen_pricing_payload_hash(payload),
        idempotency_key="price-0123456789abcdef",
    )


def test_global_price_becomes_store_override_without_changing_merchant_price(delivery, owner):
    original = _catalog_object()
    client = FakeSquare([original], model_response=True)

    result = send_square_catalog_price_updates(
        _plan(delivery, _entry()),
        actor=owner,
        client=client,
    )

    assert result.updated_count == 1
    assert not result.already_applied
    assert client.catalog.get_calls == [{"object_ids": ["VAR-1"], "include_related_objects": False}]
    call = client.catalog.upsert_calls[0]
    assert call["idempotency_key"] == "price-0123456789abcdef"
    assert len(call["batches"]) == 1
    updated = call["batches"][0]["objects"][0]
    expected = copy.deepcopy(original)
    expected["item_variation_data"]["location_overrides"].append(
        {
            "location_id": "LOCATION-1",
            "price_money": {"amount": 1200, "currency": "USD"},
        }
    )
    assert updated == expected
    assert original["item_variation_data"]["price_money"]["amount"] == 1000
    assert updated["item_variation_data"]["price_money"]["amount"] == 1000


def test_location_override_changes_only_that_store_price(delivery, owner):
    original = _catalog_object(override_price=1100)
    client = FakeSquare([original])

    send_square_catalog_price_updates(
        _plan(
            delivery,
            _entry(current=1100, target=1250, scope="LOCATION_OVERRIDE"),
        ),
        actor=owner,
        client=client,
    )

    updated = client.catalog.upsert_calls[0]["batches"][0]["objects"][0]
    expected = copy.deepcopy(original)
    expected["item_variation_data"]["location_overrides"][1]["price_money"]["amount"] = 1250
    assert updated == expected
    assert updated["item_variation_data"]["price_money"]["amount"] == 1000
    overrides = updated["item_variation_data"]["location_overrides"]
    assert overrides[0]["price_money"]["amount"] == 999
    assert overrides[1]["price_money"]["amount"] == 1250
    assert overrides[1]["sold_out"] is True


def test_all_prices_at_target_is_safe_already_applied_noop(delivery, owner):
    client = FakeSquare([_catalog_object(price=1000, override_price=1200, version=8)])

    result = send_square_catalog_price_updates(
        _plan(delivery, _entry()),
        actor=owner,
        client=client,
    )

    assert result.already_applied
    assert result.updated_count == 0
    assert client.catalog.upsert_calls == []


def test_matching_global_target_is_not_treated_as_store_override(delivery):
    with pytest.raises(PricingPlanDrift, match="changed after"):
        build_catalog_price_write(
            _plan(delivery, _entry()),
            live_objects=[_catalog_object(price=1200, version=8)],
            location_id="LOCATION-1",
        )


@pytest.mark.parametrize(
    ("live", "reason"),
    [
        (_catalog_object(price=1050), "changed_since_preview"),
        (_catalog_object(version=8), "changed_since_preview"),
        (_catalog_object(pricing_type="VARIABLE_PRICING"), "changed_since_preview"),
    ],
)
def test_price_version_or_type_drift_requires_a_new_preview(delivery, live, reason):
    with pytest.raises(PricingPlanDrift) as caught:
        build_catalog_price_write(
            _plan(delivery, _entry()),
            live_objects=[live],
            location_id="LOCATION-1",
        )

    assert caught.value.details[0]["reason"] == reason


def test_partial_target_state_never_changes_retry_payload(delivery):
    plan = _plan(
        delivery,
        _entry("VAR-1"),
        _entry("VAR-2", current=2000, target=2200),
    )
    live = [
        _catalog_object("VAR-1", price=1000, override_price=1200, version=8),
        _catalog_object("VAR-2", price=2000, version=7),
    ]

    with pytest.raises(PricingPlanDrift, match="part"):
        build_catalog_price_write(plan, live_objects=live, location_id="LOCATION-1")


def test_request_is_identical_for_safe_retry(delivery):
    plan = _plan(delivery, _entry())
    live = [_catalog_object()]

    first = build_catalog_price_write(
        plan,
        live_objects=live,
        location_id="LOCATION-1",
    )
    second = build_catalog_price_write(
        plan,
        live_objects=live,
        location_id="LOCATION-1",
    )

    assert isinstance(first, CatalogPriceWriteRequest)
    assert first.to_dict() == second.to_dict()
    assert first.idempotency_key == plan.idempotency_key


def test_employee_demo_gate_and_disabled_gate_make_no_network_calls(
    delivery,
    owner,
    employee,
    settings,
):
    plan = _plan(delivery, _entry())
    client = FakeSquare([_catalog_object()])

    with pytest.raises(PricingPlanNotReady, match="owner"):
        send_square_catalog_price_updates(plan, actor=employee, client=client)

    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = False
    with pytest.raises(CatalogPriceWritesDisabled, match="disabled"):
        send_square_catalog_price_updates(plan, actor=owner, client=client)

    demo = User.objects.create_user(
        "PRICEDEMO",
        "1234",
        display_name="Demo Owner",
        role=Role.OWNER,
        is_demo=True,
    )
    demo_delivery = Delivery.objects.create(
        submission=Submission.objects.create(
            kind=SubmissionKind.INVENTORY,
            submitted_by=demo,
        )
    )
    settings.SQUARE_CATALOG_PRICE_WRITES_ENABLED = True
    with pytest.raises(CatalogPriceWritesDisabled, match="Practice"):
        send_square_catalog_price_updates(
            _plan(demo_delivery, _entry()),
            actor=demo,
            client=client,
        )

    assert client.catalog.get_calls == []
    assert client.catalog.upsert_calls == []


def test_draft_empty_and_too_large_plans_fail_before_square(delivery, owner):
    client = FakeSquare([_catalog_object()])

    with pytest.raises(PricingPlanNotReady):
        send_square_catalog_price_updates(
            _plan(delivery, _entry(), status=PricingPlanStatus.DRAFT),
            actor=owner,
            client=client,
        )
    with pytest.raises(PricingPlanNotReady, match="no changed prices"):
        send_square_catalog_price_updates(
            _plan(delivery),
            actor=owner,
            client=client,
        )
    entries = [_entry(f"VAR-{index}") for index in range(1001)]
    with pytest.raises(PricingPlanNotReady, match="at most 1000"):
        send_square_catalog_price_updates(
            _plan(delivery, *entries),
            actor=owner,
            client=client,
        )

    assert client.catalog.get_calls == []


def test_tampered_payload_or_changed_delivery_revision_is_rejected(delivery, owner):
    plan = _plan(delivery, _entry())
    plan.frozen_payload["updates"][0]["target_price_cents"] = 9999

    with pytest.raises(PricingPlanNotReady, match="integrity"):
        send_square_catalog_price_updates(
            plan,
            actor=owner,
            client=FakeSquare([_catalog_object()]),
        )

    plan = _plan(delivery, _entry())
    Delivery.objects.filter(pk=delivery.pk).update(spreadsheet_revision=4)
    with pytest.raises(PricingPlanNotReady, match="invoice changed"):
        send_square_catalog_price_updates(
            plan,
            actor=owner,
            client=FakeSquare([_catalog_object()]),
        )
