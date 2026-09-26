from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

import apps.inventory.services as inventory_services
from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.inventory.catalog_creation import (
    CatalogCreationError,
    CatalogWritesDisabled,
    create_square_catalog_item,
)
from apps.inventory.matching import nearest_catalog_matches
from apps.inventory.models import (
    CatalogCreationIntent,
    Delivery,
    DeliveryLine,
    DeliveryStatus,
    LineMatchStatus,
    SquareCatalogVariation,
    Vendor,
)
from apps.inventory.services import (
    DeliveryNotReady,
    InventoryWritesDisabled,
    WorkbookValidationError,
    export_delivery_xlsx,
    import_delivery_xlsx,
    push_delivery_to_square,
    refresh_delivery_readiness,
    refresh_square_catalog,
    refresh_square_counts,
)
from apps.inventory.square_gateway import build_square_batches
from apps.inventory.workbooks import safe_excel_text


class FakeCatalog:
    def __init__(self, objects=(), *, upsert_response=None, fail_upsert_once=False):
        self.objects = list(objects)
        self.calls = []
        self.object = FakeCatalogObject(
            upsert_response=upsert_response,
            fail_once=fail_upsert_once,
        )

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.objects)


class FakeCatalogObject:
    def __init__(self, *, upsert_response=None, fail_once=False):
        self.upsert_response = upsert_response or {
            "id_mappings": [
                {"client_object_id": "#item-placeholder", "object_id": "ITEM-NEW"},
                {"client_object_id": "#variation-placeholder", "object_id": "VAR-NEW"},
            ],
            "catalog_object": {
                "id": "ITEM-NEW",
                "item_data": {"variations": [{"id": "VAR-NEW"}]},
            },
        }
        self.fail_once = fail_once
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated lost catalog response")
        return self.upsert_response


class FakeInventory:
    def __init__(
        self,
        counts: dict[str, Decimal],
        *,
        applied_offset: Decimal = Decimal(0),
        fail_after_apply_once: bool = False,
        crash_after_apply_once: bool = False,
    ):
        self.counts = dict(counts)
        self.applied_offset = applied_offset
        self.fail_after_apply_once = fail_after_apply_once
        self.crash_after_apply_once = crash_after_apply_once
        self.seen_keys = set()
        self.count_calls = []
        self.change_calls = []

    def batch_get_counts(self, **kwargs):
        self.count_calls.append(kwargs)
        return [
            {
                "catalog_object_id": variation_id,
                "state": "IN_STOCK",
                "location_id": "TEST_LOCATION",
                "quantity": str(self.counts.get(variation_id, Decimal(0))),
            }
            for variation_id in kwargs["catalog_object_ids"]
        ]

    def batch_create_changes(self, **kwargs):
        self.change_calls.append(kwargs)
        if kwargs["idempotency_key"] in self.seen_keys:
            return {"counts": []}
        for change in kwargs["changes"]:
            adjustment = change["adjustment"]
            variation_id = adjustment["catalog_object_id"]
            self.counts[variation_id] = (
                self.counts.get(variation_id, Decimal(0))
                + Decimal(adjustment["quantity"])
                + self.applied_offset
            )
        self.seen_keys.add(kwargs["idempotency_key"])
        if self.fail_after_apply_once:
            self.fail_after_apply_once = False
            raise RuntimeError("simulated lost response after Square applied the batch")
        if self.crash_after_apply_once:
            self.crash_after_apply_once = False
            raise SystemExit("simulated process death after Square applied the batch")
        return {"counts": []}


class FakeSquare:
    def __init__(
        self,
        *,
        counts=None,
        catalog=(),
        applied_offset=Decimal(0),
        fail_after_apply_once=False,
        crash_after_apply_once=False,
        catalog_upsert_response=None,
        fail_catalog_upsert_once=False,
    ):
        self.catalog = FakeCatalog(
            catalog,
            upsert_response=catalog_upsert_response,
            fail_upsert_once=fail_catalog_upsert_once,
        )
        self.inventory = FakeInventory(
            counts or {},
            applied_offset=applied_offset,
            fail_after_apply_once=fail_after_apply_once,
            crash_after_apply_once=crash_after_apply_once,
        )


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "OWNER1",
        "1234",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner",
    )


@pytest.fixture
def delivery(db, owner):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.APPROVED,
        submitted_by=owner,
        submitted_at=timezone.now(),
    )
    vendor = Vendor.objects.create(name="Distributor", square_vendor_id="VENDOR-1")
    delivery = Delivery.objects.create(
        submission=submission,
        vendor=vendor,
        invoice_number="INV-100",
        status=DeliveryStatus.NEEDS_REVIEW,
    )
    DeliveryLine.objects.create(
        delivery=delivery,
        position=1,
        vendor_sku="SKU-1",
        upc="012345678905",
        description="Test Bourbon 750ml",
        pack_text="12/750ML",
        cases=Decimal("1"),
        received_units=Decimal("12"),
        included=True,
    )
    SquareCatalogVariation.objects.create(
        variation_id="VAR-1",
        item_id="ITEM-1",
        item_name="Test Bourbon",
        variation_name="750ml",
        sku="SKU-1",
        upc="012345678905",
        track_inventory=True,
        present_at_location=True,
        location_id="TEST_LOCATION",
    )
    return delivery


def test_exact_upc_matches_but_count_snapshot_is_required(delivery):
    result = refresh_delivery_readiness(delivery)
    line = delivery.lines.get()

    assert line.match_status == LineMatchStatus.MATCHED
    assert line.square_catalog_variation_id == "VAR-1"
    assert line.units_per_case == 12
    assert line.received_units == Decimal("12")
    assert not result.ready
    assert "square_count_missing" in {issue.code for issue in result.issues}


def test_name_only_match_remains_a_reviewable_suggestion(delivery):
    line = delivery.lines.get()
    line.vendor_sku = ""
    line.upc = ""
    line.description = "Test Bourbon 750ml"
    line.save(update_fields=["vendor_sku", "upc", "description"])

    result = refresh_delivery_readiness(delivery)
    line.refresh_from_db()

    assert line.match_status == LineMatchStatus.SUGGESTED
    assert line.square_catalog_variation_id == "VAR-1"
    assert not result.ready
    assert "name-only Square match" in line.review_note


def test_nearest_matches_rank_names_but_reject_explicit_size_conflicts(delivery):
    SquareCatalogVariation.objects.all().delete()
    for variation_id, item_name, variation_name in [
        ("VAR-B", "Makers Mark Kentucky Bourbon", "750 ml"),
        ("VAR-A", "Makers Mark Bourbon", "750 ml"),
        ("VAR-WRONG-SIZE", "Makers Mark Bourbon", "1.75 L"),
    ]:
        SquareCatalogVariation.objects.create(
            variation_id=variation_id,
            item_name=item_name,
            variation_name=variation_name,
            track_inventory=True,
            present_at_location=True,
            location_id="TEST_LOCATION",
        )
    line = delivery.lines.get()
    line.description = "Makers Mark Bourbon 750ml"
    line.vendor_sku = ""
    line.upc = ""
    line.save(update_fields=["description", "vendor_sku", "upc", "updated_at"])

    suggestions = nearest_catalog_matches(line)

    assert suggestions[0].variation_id == "VAR-A"
    assert "VAR-WRONG-SIZE" not in {candidate.variation_id for candidate in suggestions}
    assert all(candidate.suggestion_reason for candidate in suggestions)


def test_nearest_matches_uses_bottle_size_from_pack_text(delivery):
    SquareCatalogVariation.objects.all().delete()
    for variation_id, variation_name in [
        ("VAR-RIGHT-SIZE", "750 ml"),
        ("VAR-WRONG-SIZE", "1.75 L"),
    ]:
        SquareCatalogVariation.objects.create(
            variation_id=variation_id,
            item_name="Makers Mark Bourbon",
            variation_name=variation_name,
            track_inventory=True,
            present_at_location=True,
            location_id="TEST_LOCATION",
        )
    line = delivery.lines.get()
    line.description = "Makers Mark Bourbon"
    line.pack_text = "12/750ML"
    line.vendor_sku = ""
    line.upc = ""
    line.save(
        update_fields=["description", "pack_text", "vendor_sku", "upc", "updated_at"]
    )

    suggestions = nearest_catalog_matches(line)

    assert [candidate.variation_id for candidate in suggestions] == ["VAR-RIGHT-SIZE"]


def test_owner_can_create_new_square_item_without_posting_inventory(delivery, owner, settings):
    settings.SQUARE_CATALOG_WRITES_ENABLED = True
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    client = FakeSquare(catalog=[])
    line = delivery.lines.get()

    result = create_square_catalog_item(
        line,
        actor=owner,
        item_name="New Bourbon",
        variation_name="750 ml",
        sku="NEW-750",
        upc="012345678912",
        sale_price_cents=2199,
        variable_price=False,
        client=client,
    )

    line.refresh_from_db()
    assert result.variation_id == "VAR-NEW"
    assert result.already_created is False
    assert line.square_catalog_variation_id == "VAR-NEW"
    assert line.match_status == LineMatchStatus.MATCHED
    request = client.catalog.object.calls[0]
    variation_data = request["object"]["item_data"]["variations"][0]["item_variation_data"]
    assert variation_data["price_money"]["amount"] == 2199
    assert "vendor_information" not in variation_data
    assert client.inventory.change_calls == []
    assert CatalogCreationIntent.objects.get(line=line).status == "SUCCEEDED"


def test_catalog_create_has_separate_gate_and_owner_boundary(delivery, owner, settings):
    settings.SQUARE_CATALOG_WRITES_ENABLED = False
    client = FakeSquare(catalog=[])
    line = delivery.lines.get()
    kwargs = {
        "item_name": "New Bourbon",
        "variation_name": "750 ml",
        "sku": "NEW-750",
        "upc": "012345678912",
        "sale_price_cents": 2199,
        "variable_price": False,
        "client": client,
    }

    with pytest.raises(CatalogWritesDisabled):
        create_square_catalog_item(line, actor=owner, **kwargs)

    settings.SQUARE_CATALOG_WRITES_ENABLED = True
    employee = User.objects.create_user("CLERK2", "1234", display_name="Clerk Two")
    with pytest.raises(CatalogCreationError, match="Only an owner"):
        create_square_catalog_item(line, actor=employee, **kwargs)
    assert client.catalog.object.calls == []


def test_catalog_create_lost_response_reuses_protected_key(delivery, owner, settings):
    settings.SQUARE_CATALOG_WRITES_ENABLED = True
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    client = FakeSquare(catalog=[], fail_catalog_upsert_once=True)
    line = delivery.lines.get()
    kwargs = {
        "actor": owner,
        "item_name": "Retry Bourbon",
        "variation_name": "750 ml",
        "sku": "RETRY-750",
        "upc": "012345678929",
        "sale_price_cents": 2499,
        "variable_price": False,
        "client": client,
    }

    with pytest.raises(RuntimeError, match="lost catalog response"):
        create_square_catalog_item(line, **kwargs)
    result = create_square_catalog_item(line, **kwargs)

    keys = [call["idempotency_key"] for call in client.catalog.object.calls]
    assert keys[0] == keys[1] == result.idempotency_key
    assert CatalogCreationIntent.objects.get(line=line).status == "SUCCEEDED"


def test_catalog_refresh_is_read_only_and_caches_location_data(db, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    variation = {
        "type": "ITEM_VARIATION",
        "id": "VAR-CACHE",
        "present_at_location_ids": ["TEST_LOCATION"],
        "item_variation_data": {
            "item_id": "ITEM-CACHE",
            "name": "1 L",
            "sku": "CACHE-SKU",
            "upc": "00012345678905",
            "track_inventory": True,
            "vendor_information": [
                {
                    "vendor_id": "VENDOR-DEFAULT",
                    "vendor_code": "DEFAULT-1",
                    "unit_cost_money": {"amount": 1299, "currency": "USD"},
                },
                {
                    "vendor_id": "VENDOR-1",
                    "vendor_code": "CACHE-SKU",
                    "unit_cost_money": {"amount": 1199, "currency": "USD"},
                },
            ],
        },
    }
    item = {
        "type": "ITEM",
        "id": "ITEM-CACHE",
        "item_data": {"name": "Cache Vodka", "variations": [variation]},
    }
    client = FakeSquare(catalog=[item, variation])

    result = refresh_square_catalog(client=client)
    cached = SquareCatalogVariation.objects.get(pk="VAR-CACHE")

    assert result.seen == 1
    assert client.catalog.calls == [{"types": "ITEM,ITEM_VARIATION"}]
    assert cached.item_name == "Cache Vodka"
    assert cached.gtin == "00012345678905"
    assert cached.track_inventory is True
    assert cached.present_at_location is True
    assert cached.default_unit_cost_cents == 1299
    assert cached.default_unit_cost_vendor_id == "VENDOR-DEFAULT"
    assert cached.vendor_costs[1]["amount"] == 1199
    assert cached.cost_snapshot_for_vendor("VENDOR-1") == (
        1199,
        "USD",
        "Square vendor VENDOR-1",
    )


def test_matching_snapshots_square_cost_and_calculates_change(delivery):
    variation = SquareCatalogVariation.objects.get(pk="VAR-1")
    variation.vendor_costs = [
        {
            "vendor_id": "VENDOR-1",
            "vendor_code": "SKU-1",
            "amount": 1000,
            "currency": "USD",
        }
    ]
    variation.default_unit_cost_cents = 1000
    variation.default_unit_cost_currency = "USD"
    variation.default_unit_cost_vendor_id = "VENDOR-1"
    variation.save()
    line = delivery.lines.get()
    line.unit_cost_cents = 1125
    line.save(update_fields=["unit_cost_cents", "updated_at"])

    refresh_delivery_readiness(delivery)
    line.refresh_from_db()

    assert line.square_unit_cost_cents == 1000
    assert line.square_unit_cost_source == "Square vendor VENDOR-1"
    assert line.unit_cost_change_cents == 125
    assert line.unit_cost_change_percent == Decimal("12.5")
    assert line.unit_cost_change_display == "+12.5%"


@pytest.mark.parametrize(
    ("item_location", "variation_location", "expected"),
    [
        ({}, {}, True),
        ({"present_at_all_locations": True}, {"absent_at_location_ids": ["TEST_LOCATION"]}, False),
        (
            {"present_at_all_locations": False, "present_at_location_ids": ["OTHER"]},
            {"present_at_all_locations": True},
            False,
        ),
        (
            {"present_at_all_locations": True},
            {"present_at_all_locations": False, "present_at_location_ids": ["TEST_LOCATION"]},
            True,
        ),
    ],
)
def test_catalog_location_combines_item_and_variation_rules(
    db, settings, item_location, variation_location, expected
):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    variation = {
        "type": "ITEM_VARIATION",
        "id": "VAR-LOCATION",
        **variation_location,
        "item_variation_data": {
            "item_id": "ITEM-LOCATION",
            "name": "750 ml",
            "track_inventory": True,
        },
    }
    item = {
        "type": "ITEM",
        "id": "ITEM-LOCATION",
        **item_location,
        "item_data": {"name": "Location Bottle", "variations": [variation]},
    }

    refresh_square_catalog(client=FakeSquare(catalog=[item, variation]))

    assert SquareCatalogVariation.objects.get(pk="VAR-LOCATION").present_at_location is expected


def test_count_snapshot_and_verified_push(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(counts={"VAR-1": Decimal("10")})

    snapshot = refresh_square_counts(delivery, client=client)
    line = delivery.lines.get()
    assert snapshot.readiness.ready
    assert line.square_count_before == Decimal("10")
    assert line.proposed_delta == Decimal("12")
    assert line.projected_count_after == Decimal("22")

    result = push_delivery_to_square(delivery, owner, client=client)
    line.refresh_from_db()
    delivery.refresh_from_db()

    assert result.verified is True
    assert delivery.status == DeliveryStatus.PUSHED
    assert line.square_count_after == Decimal("22")
    assert line.square_count_drift == 0
    assert len(client.inventory.change_calls) == 1
    request = client.inventory.change_calls[0]
    assert len(request["changes"]) == 1
    assert re.fullmatch(r"[0-9a-f-]{36}", request["idempotency_key"])
    adjustment = request["changes"][0]["adjustment"]
    assert adjustment["from_state"] == "NONE"
    assert adjustment["to_state"] == "IN_STOCK"
    assert adjustment["to_location_id"] == "TEST_LOCATION"
    assert adjustment["team_member_id"] == "TM-owner"
    assert adjustment["reference_id"] == str(line.id)
    assert adjustment["quantity"] == "12"
    assert adjustment["occurred_at"].endswith("Z")

    replay = push_delivery_to_square(delivery, owner, client=client)
    assert replay.already_pushed
    assert len(client.inventory.change_calls) == 1


def test_push_service_refuses_employee_even_when_called_directly(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    employee = User.objects.create_user(
        "CLERK1",
        "1234",
        display_name="Clerk",
        role=Role.EMPLOYEE,
        square_team_member_id="TM-clerk",
    )

    with pytest.raises(DeliveryNotReady, match="Only an owner"):
        push_delivery_to_square(delivery, employee, client=client)

    delivery.refresh_from_db()
    assert delivery.square_batch_keys == []
    assert client.inventory.change_calls == []


def test_push_service_refuses_unapproved_submission(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    delivery.submission.status = SubmissionStatus.READY
    delivery.submission.save(update_fields=["status", "updated_at"])

    with pytest.raises(DeliveryNotReady, match="Approve the invoice evidence"):
        push_delivery_to_square(delivery, owner, client=client)

    delivery.refresh_from_db()
    assert delivery.square_batch_keys == []
    assert client.inventory.change_calls == []


def test_square_cost_is_validated_full_receipt_total(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_COST_WRITES_ENABLED = True
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    line = delivery.lines.get()
    line.unit_cost_cents = 500
    line.line_total_cents = 6000
    line.save(update_fields=["unit_cost_cents", "line_total_cents", "updated_at"])

    adjustment = build_square_batches(delivery, actor=owner)[0].changes[0]["adjustment"]
    assert adjustment["cost_money"]["amount"] == 6000

    line.line_total_cents = 500
    line.save(update_fields=["line_total_cents", "updated_at"])
    adjustment = build_square_batches(delivery, actor=owner)[0].changes[0]["adjustment"]
    assert "cost_money" not in adjustment


def test_square_cost_is_omitted_when_separate_gate_is_off(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_COST_WRITES_ENABLED = False
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    line = delivery.lines.get()
    line.unit_cost_cents = 500
    line.line_total_cents = 6000
    line.save(update_fields=["unit_cost_cents", "line_total_cents", "updated_at"])

    adjustment = build_square_batches(delivery, actor=owner)[0].changes[0]["adjustment"]
    assert "cost_money" not in adjustment


def test_post_push_count_drift_is_terminal_and_visible(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(
        counts={"VAR-1": Decimal("10")},
        applied_offset=Decimal("1"),
    )
    refresh_square_counts(delivery, client=client)

    result = push_delivery_to_square(delivery, owner, client=client)
    line = delivery.lines.get()
    delivery.refresh_from_db()

    assert result.verified is False
    assert result.drift_lines[0]["drift"] == "1"
    assert delivery.status == DeliveryStatus.PUSHED_WITH_DRIFT
    assert line.square_count_after == Decimal("23")
    assert line.square_count_drift == Decimal("1")


def test_push_refuses_when_square_count_changed_after_review(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    client.inventory.counts["VAR-1"] = Decimal("11")

    with pytest.raises(DeliveryNotReady, match="changed after review"):
        push_delivery_to_square(delivery, owner, client=client)

    delivery.refresh_from_db()
    assert client.inventory.change_calls == []
    assert delivery.status == DeliveryStatus.NEEDS_REVIEW
    assert delivery.square_batch_keys == []
    assert delivery.pushed_by_id is None
    line = delivery.lines.get()
    assert line.square_count_before is None
    assert line.projected_count_after is None


def test_count_snapshot_and_audit_commit_together(
    delivery,
    owner,
    settings,
    monkeypatch,
):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    client = FakeSquare(counts={"VAR-1": Decimal("10")})

    def fail_audit(**_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(inventory_services, "_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        refresh_square_counts(delivery, client=client, actor=owner)

    line = delivery.lines.get()
    assert line.square_count_before is None
    assert line.projected_count_after is None


def test_count_snapshot_treats_never_initialized_square_count_as_zero(delivery, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    client.inventory.batch_get_counts = lambda **_kwargs: []

    refresh_square_counts(delivery, client=client)

    line = delivery.lines.get()
    assert line.square_count_before == 0
    assert line.projected_count_after == 12


def test_readiness_blocks_duplicate_vendor_invoice(delivery, owner):
    delivery.invoice_date = dt.date(2026, 9, 26)
    delivery.save(update_fields=["invoice_date", "updated_at"])
    earlier_submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.APPROVED,
        submitted_by=owner,
    )
    Delivery.objects.create(
        submission=earlier_submission,
        vendor=delivery.vendor,
        invoice_number=" inv-100 ",
        invoice_date=delivery.invoice_date,
        status=DeliveryStatus.PUSHED,
    )

    result = refresh_delivery_readiness(delivery)

    assert not result.ready
    assert "duplicate_vendor_invoice" in {issue.code for issue in result.issues}
    assert "already appears" in next(
        issue.message for issue in result.issues if issue.code == "duplicate_vendor_invoice"
    )


def test_rejected_vendor_invoice_duplicate_no_longer_blocks(delivery, owner):
    delivery.invoice_date = dt.date(2026, 9, 26)
    delivery.save(update_fields=["invoice_date", "updated_at"])
    rejected_submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.REJECTED,
        submitted_by=owner,
    )
    Delivery.objects.create(
        submission=rejected_submission,
        vendor=delivery.vendor,
        invoice_number="INV-100",
        invoice_date=delivery.invoice_date,
    )

    result = refresh_delivery_readiness(delivery)

    assert "duplicate_vendor_invoice" not in {issue.code for issue in result.issues}


def test_readiness_blocks_reused_invoice_photo(delivery, owner):
    duplicate_sha = "d" * 64
    _add_delivery_document(delivery.submission, duplicate_sha, "current.jpg")
    other_submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.READY,
        submitted_by=owner,
    )
    _add_delivery_document(other_submission, duplicate_sha, "other.jpg")
    Delivery.objects.create(
        submission=other_submission,
        vendor=delivery.vendor,
        invoice_number="DIFFERENT-200",
        invoice_date=dt.date(2026, 9, 25),
    )

    result = refresh_delivery_readiness(delivery)

    assert not result.ready
    assert "duplicate_source_photo" in {issue.code for issue in result.issues}


def test_push_rechecks_duplicate_invoice_before_square_write(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    delivery.invoice_date = dt.date(2026, 9, 26)
    delivery.save(update_fields=["invoice_date", "updated_at"])
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    duplicate_submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.APPROVED,
        submitted_by=owner,
    )
    Delivery.objects.create(
        submission=duplicate_submission,
        vendor=delivery.vendor,
        invoice_number=delivery.invoice_number,
        invoice_date=delivery.invoice_date,
        status=DeliveryStatus.PUSHED,
    )

    with pytest.raises(DeliveryNotReady, match="already appears"):
        push_delivery_to_square(delivery, owner, client=client)

    assert client.inventory.change_calls == []


def test_failed_push_retries_the_same_idempotency_key(delivery, owner, settings):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    client = FakeSquare(
        counts={"VAR-1": Decimal("10")},
        fail_after_apply_once=True,
    )
    refresh_square_counts(delivery, client=client)

    with pytest.raises(RuntimeError, match="lost response"):
        push_delivery_to_square(delivery, owner, client=client)
    delivery.refresh_from_db()
    first_key = delivery.square_batch_keys[0]
    assert delivery.status == DeliveryStatus.FAILED
    assert client.inventory.counts["VAR-1"] == Decimal("22")

    result = push_delivery_to_square(delivery, owner, client=client)
    delivery.refresh_from_db()
    assert result.verified is True
    assert delivery.status == DeliveryStatus.PUSHED
    assert [call["idempotency_key"] for call in client.inventory.change_calls] == [
        first_key,
        first_key,
    ]
    assert client.inventory.counts["VAR-1"] == Decimal("22")


def test_process_crash_keeps_committed_keys_and_stale_resume_is_idempotent(
    delivery,
    owner,
    settings,
):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    settings.SQUARE_PUSH_STALE_SECONDS = 60
    client = FakeSquare(
        counts={"VAR-1": Decimal("10")},
        crash_after_apply_once=True,
    )
    refresh_square_counts(delivery, client=client)

    with pytest.raises(SystemExit, match="process death"):
        push_delivery_to_square(delivery, owner, client=client)

    delivery.refresh_from_db()
    first_key = delivery.square_batch_keys[0]
    assert delivery.status == DeliveryStatus.PUSHING
    assert delivery.pushed_by == owner
    assert client.inventory.counts["VAR-1"] == Decimal("22")
    assert AuditEvent.objects.filter(
        action="inventory.square_push_claimed",
        target_id=str(delivery.pk),
    ).exists()

    with pytest.raises(DeliveryNotReady, match="still in progress"):
        push_delivery_to_square(delivery, owner, client=client)
    assert len(client.inventory.change_calls) == 1

    Delivery.objects.filter(pk=delivery.pk).update(
        updated_at=timezone.now() - dt.timedelta(seconds=61)
    )
    alternate_owner = User.objects.create_user(
        "OWNER2",
        "1234",
        display_name="Backup Owner",
        role=Role.OWNER,
        square_team_member_id="TM-backup",
    )

    result = push_delivery_to_square(delivery, alternate_owner, client=client)

    delivery.refresh_from_db()
    assert result.verified is True
    assert delivery.status == DeliveryStatus.PUSHED
    assert client.inventory.counts["VAR-1"] == Decimal("22")
    assert [call["idempotency_key"] for call in client.inventory.change_calls] == [
        first_key,
        first_key,
    ]
    assert [
        call["changes"][0]["adjustment"]["team_member_id"] for call in client.inventory.change_calls
    ] == ["TM-owner", "TM-owner"]


def test_final_audit_failure_leaves_resumable_claim_not_false_success(
    delivery,
    owner,
    settings,
    monkeypatch,
):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    settings.SQUARE_INVENTORY_WRITES_ENABLED = True
    settings.SQUARE_PUSH_STALE_SECONDS = 60
    client = FakeSquare(counts={"VAR-1": Decimal("10")})
    refresh_square_counts(delivery, client=client)
    real_audit = inventory_services._audit

    def fail_only_success(*, action, **kwargs):
        if action == "inventory.square_push_succeeded":
            raise RuntimeError("audit unavailable")
        return real_audit(action=action, **kwargs)

    monkeypatch.setattr(inventory_services, "_audit", fail_only_success)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        push_delivery_to_square(delivery, owner, client=client)

    delivery.refresh_from_db()
    first_key = delivery.square_batch_keys[0]
    assert delivery.status == DeliveryStatus.PUSHING
    assert delivery.pushed_at is None
    assert client.inventory.counts["VAR-1"] == Decimal("22")

    monkeypatch.setattr(inventory_services, "_audit", real_audit)
    Delivery.objects.filter(pk=delivery.pk).update(
        updated_at=timezone.now() - dt.timedelta(seconds=61)
    )
    result = push_delivery_to_square(delivery, owner, client=client)

    delivery.refresh_from_db()
    assert result.verified is True
    assert delivery.status == DeliveryStatus.PUSHED
    assert client.inventory.counts["VAR-1"] == Decimal("22")
    assert [call["idempotency_key"] for call in client.inventory.change_calls] == [
        first_key,
        first_key,
    ]


def test_square_write_gate_prevents_even_fake_calls(delivery, owner, settings):
    settings.SQUARE_INVENTORY_WRITES_ENABLED = False
    client = FakeSquare(counts={"VAR-1": Decimal("10")})

    with pytest.raises(InventoryWritesDisabled):
        push_delivery_to_square(delivery, owner, client=client)

    assert client.inventory.change_calls == []


def test_square_batches_are_capped_and_idempotency_keys_are_deterministic(
    delivery, owner, settings
):
    settings.SQUARE_LOCATION_ID = "TEST_LOCATION"
    delivery.lines.all().delete()
    DeliveryLine.objects.bulk_create(
        [
            DeliveryLine(
                delivery=delivery,
                position=position,
                description=f"Item {position}",
                units_per_case=1,
                received_units=Decimal("1"),
                square_catalog_variation_id=f"VAR-{position}",
                square_item_name=f"Item {position}",
                match_status=LineMatchStatus.MATCHED,
                included=True,
                square_count_before=Decimal("0"),
                square_count_variation_id=f"VAR-{position}",
                projected_count_after=Decimal("1"),
                square_count_snapshot_at=timezone.now(),
            )
            for position in range(1, 206)
        ]
    )

    first = build_square_batches(delivery, actor=owner)
    second = build_square_batches(delivery, actor=owner)

    assert [len(batch.changes) for batch in first] == [100, 100, 5]
    assert [batch.idempotency_key for batch in first] == [batch.idempotency_key for batch in second]


def test_export_import_is_single_use_and_auditable(delivery, owner):
    exported = export_delivery_xlsx(delivery)
    assert exported.startswith(b"PK")

    first = import_delivery_xlsx(delivery, exported, actor=owner)
    assert first.revision_before == 1
    assert first.revision_after == 2

    with pytest.raises(WorkbookValidationError, match="stale"):
        import_delivery_xlsx(delivery, exported, actor=owner)


def test_final_workbook_has_cost_comparison_and_download_only_instructions(delivery):
    line = delivery.lines.get()
    line.unit_cost_cents = 1125
    line.square_unit_cost_cents = 1000
    line.square_unit_cost_currency = "USD"
    line.square_unit_cost_source = "Square vendor VENDOR-1"
    line.line_total_cents = 13500
    line.save()

    exported = export_delivery_xlsx(delivery)
    with zipfile.ZipFile(io.BytesIO(exported)) as workbook:
        sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
        styles = workbook.read("xl/styles.xml").decode()

    assert "Finalized owner-reviewed snapshot" in sheet
    assert "cannot be uploaded back into the app" in sheet
    assert "Invoice unit cost" in sheet
    assert "Square baseline unit cost" in sheet
    assert "Unit cost change %" in sheet
    assert "+12.5%" in sheet
    assert "Square vendor VENDOR-1" in sheet
    assert "FFFFF2CC" not in styles


def test_workbook_rejects_formulas_and_non_numeric_quantities(delivery):
    exported = export_delivery_xlsx(delivery)
    formula_file = _replace_sheet_cell(
        exported,
        "G5",
        '<c r="G5" t="n" s="5"><f>1+1</f><v>2</v></c>',
    )
    with pytest.raises(WorkbookValidationError, match="Formulas"):
        import_delivery_xlsx(delivery, formula_file)

    text_file = _replace_sheet_cell(
        exported,
        "G5",
        '<c r="G5" t="inlineStr" s="5"><is><t>twelve</t></is></c>',
    )
    with pytest.raises(WorkbookValidationError, match="numeric"):
        import_delivery_xlsx(delivery, text_file)


def test_formula_like_text_is_neutralized():
    assert safe_excel_text('=HYPERLINK("https://bad.example")').startswith("'=")
    assert safe_excel_text("ordinary item") == "ordinary item"


def _replace_sheet_cell(workbook: bytes, reference: str, replacement: str) -> bytes:
    source = io.BytesIO(workbook)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as incoming,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as outgoing,
    ):
        for info in incoming.infolist():
            content = incoming.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                text = content.decode()
                pattern = rf'<c r="{reference}"[^>]*>.*?</c>'
                text, replacements = re.subn(pattern, replacement, text, count=1)
                assert replacements == 1
                content = text.encode()
            outgoing.writestr(info, content)
    return output.getvalue()


def _add_delivery_document(submission, sha256: str, name: str) -> Document:
    return Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile(name, b"invoice image", "image/jpeg"),
        original_name=name,
        media_type="image/jpeg",
        size_bytes=13,
        sha256=sha256,
        requested_type=DocumentType.DELIVERY_INVOICE,
        detected_type=DocumentType.DELIVERY_INVOICE,
        status=DocumentStatus.EXTRACTED,
    )
