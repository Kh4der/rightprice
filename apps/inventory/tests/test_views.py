from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent, FieldCorrection
from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionKind,
    SubmissionStatus,
)
from apps.inventory.services import DeliveryNotReady, refresh_delivery_readiness

from ..models import (
    Delivery,
    DeliveryLine,
    LineMatchStatus,
    SquareCatalogVariation,
)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        login_code="OWN1",
        password="a-very-long-owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        login_code="EMP1",
        password="1234",
        display_name="Employee One",
    )


@pytest.fixture
def delivery(employee):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.READY,
        submitted_by=employee,
    )
    delivery = Delivery.objects.create(
        submission=submission,
        invoice_number="INV-100",
        vendor_name_raw="Test Distributor",
    )
    DeliveryLine.objects.create(
        delivery=delivery,
        position=1,
        description="Test Bourbon 750ML",
        vendor_sku="B100",
        upc="012345678905",
        pack_text="12/750ML",
        cases=Decimal("2"),
        units_per_case=12,
        received_units=Decimal("24"),
    )
    return delivery


@pytest.mark.django_db
def test_submitter_can_see_comparison_but_other_employee_cannot(client, delivery):
    client.force_login(delivery.submission.submitted_by)

    response = client.get(reverse("inventory:delivery-detail", args=[delivery.pk]))

    assert response.status_code == 200
    assert b"Square now" in response.content
    assert b"This delivery" in response.content
    assert b"Projected" in response.content
    outsider = User.objects.create_user(
        login_code="EMP2", password="2345", display_name="Employee Two"
    )
    client.force_login(outsider)
    assert client.get(reverse("inventory:delivery-detail", args=[delivery.pk])).status_code == 404


@pytest.mark.django_db
def test_owner_correction_clears_stale_count_evidence(client, owner, delivery):
    line = delivery.lines.get()
    line.square_count_variation_id = "old-variation"
    line.square_count_before = Decimal("10")
    line.projected_count_after = Decimal("34")
    line.save()
    client.force_login(owner)
    prefix = f"line-{line.pk}"

    response = client.post(
        reverse("inventory:line-update", args=[delivery.pk, line.pk]),
        {
            f"{prefix}-description": line.description,
            f"{prefix}-vendor_sku": line.vendor_sku,
            f"{prefix}-upc": line.upc,
            f"{prefix}-pack_text": line.pack_text,
            f"{prefix}-cases": "3",
            f"{prefix}-loose_units": "0",
            f"{prefix}-units_per_case": "12",
            f"{prefix}-received_units": "36",
            f"{prefix}-included": "on",
            f"{prefix}-review_note": "Counted three cases",
        },
    )

    assert response.status_code == 302
    line.refresh_from_db()
    assert line.received_units == Decimal("36")
    assert line.square_count_variation_id == ""
    assert line.square_count_before is None
    assert line.projected_count_after is None


@pytest.mark.django_db
def test_owner_header_correction_records_original_values(client, owner, delivery):
    client.force_login(owner)

    response = client.post(
        reverse("inventory:header-update", args=[delivery.pk]),
        {
            "vendor_name_raw": "Corrected Distributor",
            "invoice_number": "INV-200",
            "invoice_date": "2026-10-02",
            "invoice_total": "123.45",
            "printed_total_cases": "2",
            "printed_total_loose_units": "0",
            "printed_total_physical_units": "24",
        },
    )

    assert response.status_code == 302
    event = AuditEvent.objects.get(action="inventory.header_corrected")
    assert event.detail["changes"]["vendor_name_raw"] == {
        "before": "Test Distributor",
        "after": "Corrected Distributor",
    }
    assert event.detail["changes"]["invoice_number"] == {
        "before": "INV-100",
        "after": "INV-200",
    }
    assert event.detail["changes"]["printed_total_cases"] == {
        "before": None,
        "after": "2.000",
    }
    assert event.detail["changes"]["printed_total_loose_units"] == {
        "before": None,
        "after": "0.000",
    }
    assert event.detail["changes"]["printed_total_physical_units"] == {
        "before": None,
        "after": "24.000",
    }


@pytest.mark.django_db
def test_owner_can_correct_misread_footer_total_and_clear_guard(client, owner, delivery):
    line = delivery.lines.get()
    line.loose_units = Decimal("0")
    line.save(update_fields=["loose_units", "updated_at"])
    delivery.printed_total_cases = Decimal("3")
    delivery.printed_total_loose_units = Decimal("0")
    delivery.printed_total_physical_units = Decimal("24")
    delivery.save(
        update_fields=[
            "printed_total_cases",
            "printed_total_loose_units",
            "printed_total_physical_units",
            "updated_at",
        ]
    )
    Document.objects.create(
        submission=delivery.submission,
        file=SimpleUploadedFile("invoice-footer.jpg", b"invoice footer", "image/jpeg"),
        original_name="invoice-footer.jpg",
        media_type="image/jpeg",
        size_bytes=14,
        sha256="f" * 64,
        requested_type=DocumentType.DELIVERY_INVOICE,
        detected_type=DocumentType.DELIVERY_INVOICE,
        status=DocumentStatus.EXTRACTED,
    )
    initial = refresh_delivery_readiness(delivery)
    assert "invoice_total_cases_mismatch" in {issue.code for issue in initial.issues}

    client.force_login(owner)
    response = client.post(
        reverse("inventory:header-update", args=[delivery.pk]),
        {
            "vendor_name_raw": delivery.vendor_name_raw,
            "invoice_number": delivery.invoice_number,
            "invoice_date": "2026-10-02",
            "invoice_total": "",
            "printed_total_cases": "2",
            "printed_total_loose_units": "0",
            "printed_total_physical_units": "24",
        },
    )

    assert response.status_code == 302
    delivery.refresh_from_db()
    corrected = refresh_delivery_readiness(delivery)
    assert not any(issue.code.startswith("invoice_total_") for issue in corrected.issues)
    event = AuditEvent.objects.get(action="inventory.header_corrected")
    assert event.detail["changes"]["printed_total_cases"] == {
        "before": "3.000",
        "after": "2.000",
    }

    detail = client.get(reverse("inventory:delivery-detail", args=[delivery.pk]))
    assert b"Footer: total cases" in detail.content
    assert b"Footer: loose bottles/cans" in detail.content
    assert b"Footer: total physical bottles/cans" in detail.content


@pytest.mark.django_db
def test_owner_can_record_loose_units_separately_from_cases(client, owner, delivery):
    line = delivery.lines.get()
    Document.objects.create(
        submission=delivery.submission,
        file=SimpleUploadedFile("invoice.jpg", b"invoice image", "image/jpeg"),
        original_name="invoice.jpg",
        media_type="image/jpeg",
        size_bytes=13,
        sha256="a" * 64,
        requested_type=DocumentType.DELIVERY_INVOICE,
        detected_type=DocumentType.DELIVERY_INVOICE,
        status=DocumentStatus.EXTRACTED,
    )
    client.force_login(owner)
    prefix = f"line-{line.pk}"

    response = client.post(
        reverse("inventory:line-update", args=[delivery.pk, line.pk]),
        {
            f"{prefix}-description": line.description,
            f"{prefix}-vendor_sku": line.vendor_sku,
            f"{prefix}-upc": line.upc,
            f"{prefix}-pack_text": line.pack_text,
            f"{prefix}-cases": "1",
            f"{prefix}-loose_units": "2",
            f"{prefix}-units_per_case": "12",
            f"{prefix}-received_units": "14",
            f"{prefix}-included": "on",
            f"{prefix}-review_note": "One case plus two loose units",
        },
    )

    assert response.status_code == 302
    line.refresh_from_db()
    assert line.loose_units == Decimal("2")
    assert line.received_units == Decimal("14")
    event = AuditEvent.objects.get(action="inventory.line_corrected", target_id=str(line.pk))
    assert event.detail["changes"]["loose_units"] == {"before": "0.000", "after": "2"}
    correction = FieldCorrection.objects.get(
        field_path="delivery.lines[1].loose_units",
        corrected_by=owner,
    )
    assert correction.previous_value == "0.000"
    assert correction.corrected_value == "2"


@pytest.mark.django_db
def test_employee_cannot_correct_or_export_inventory(client, delivery):
    line = delivery.lines.get()
    employee = delivery.submission.submitted_by
    client.force_login(employee)
    prefix = f"line-{line.pk}"

    correction = client.post(
        reverse("inventory:line-update", args=[delivery.pk, line.pk]),
        {
            f"{prefix}-description": "Changed by employee",
            f"{prefix}-included": "on",
        },
    )
    export = client.get(reverse("inventory:export", args=[delivery.pk]))

    assert correction.status_code == 403
    assert export.status_code == 403
    line.refresh_from_db()
    assert line.description == "Test Bourbon 750ML"


@pytest.mark.django_db
def test_owner_can_add_a_line_the_photo_reader_missed(client, owner, delivery):
    client.force_login(owner)

    response = client.post(
        reverse("inventory:line-create", args=[delivery.pk]),
        {
            "new-description": "Missed Rye 1L",
            "new-vendor_sku": "RYE-1L",
            "new-upc": "012345678929",
            "new-pack_text": "6/1L",
            "new-cases": "2",
            "new-loose_units": "0",
            "new-units_per_case": "6",
            "new-received_units": "12",
            "new-unit_cost": "15.00",
            "new-line_total": "180.00",
            "new-included": "on",
            "new-review_note": "Added from invoice photo",
        },
    )

    assert response.status_code == 302
    added = delivery.lines.get(position=2)
    assert added.description == "Missed Rye 1L"
    assert added.received_units == Decimal("12")
    assert added.unit_cost_cents == 1500
    assert added.match_status == LineMatchStatus.UNMATCHED
    assert AuditEvent.objects.filter(
        action="inventory.line_added_by_owner", target_id=str(added.pk)
    ).exists()


@pytest.mark.django_db
def test_owner_can_choose_only_cached_stocked_square_variation(client, owner, delivery):
    variation = SquareCatalogVariation.objects.create(
        variation_id="VAR-1",
        item_id="ITEM-1",
        item_name="Test Bourbon",
        variation_name="750ML",
        sku="SQ-B100",
        upc="012345678905",
        track_inventory=True,
        present_at_location=True,
        location_id="LOC-1",
    )
    line = delivery.lines.get()
    client.force_login(owner)

    response = client.post(
        reverse("inventory:choose-match", args=[delivery.pk, line.pk]),
        {"variation_id": variation.pk},
    )

    assert response.status_code == 302
    line.refresh_from_db()
    assert line.square_catalog_variation_id == variation.pk
    assert line.match_status == LineMatchStatus.MATCHED
    assert line.square_count_before is None


@pytest.mark.django_db
def test_owner_can_explicitly_create_and_match_new_square_item(client, owner, delivery, settings):
    settings.SQUARE_CATALOG_WRITES_ENABLED = True
    line = delivery.lines.get()
    client.force_login(owner)

    with patch(
        "apps.inventory.views.create_square_catalog_item",
        return_value=SimpleNamespace(already_created=False),
    ) as create:
        response = client.post(
            reverse("inventory:create-catalog-item", args=[delivery.pk, line.pk]),
            {
                "item_name": "New Bourbon",
                "variation_name": "750 ml",
                "sku": "NEW-750",
                "upc": "012345678912",
                "sale_price": "21.99",
                "confirm": "on",
            },
        )

    assert response.status_code == 302
    assert response.url == reverse("inventory:delivery-detail", args=[delivery.pk])
    kwargs = create.call_args.kwargs
    assert kwargs["actor"] == owner
    assert kwargs["sale_price_cents"] == 2199
    assert kwargs["variable_price"] is False


@pytest.mark.django_db
def test_owner_downloads_final_workbook_only_after_live_comparison(client, owner, delivery):
    variation = SquareCatalogVariation.objects.create(
        variation_id="VAR-FINAL",
        item_id="ITEM-FINAL",
        item_name="Test Bourbon",
        variation_name="750ML",
        sku="B100",
        upc="012345678905",
        track_inventory=True,
        present_at_location=True,
        location_id="LOC-1",
        vendor_costs=[
            {
                "vendor_id": "",
                "vendor_code": "B100",
                "amount": 1000,
                "currency": "USD",
            }
        ],
        default_unit_cost_cents=1000,
        default_unit_cost_currency="USD",
    )
    line = delivery.lines.get()
    line.square_catalog_variation_id = variation.pk
    line.square_item_name = str(variation)
    line.match_status = LineMatchStatus.MATCHED
    line.square_count_variation_id = variation.pk
    line.square_count_before = Decimal("10")
    line.projected_count_after = Decimal("34")
    line.square_count_snapshot_at = timezone.now()
    line.unit_cost_cents = 1100
    line.line_total_cents = 26400
    line.save()
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.approved_by = owner
    delivery.submission.approved_at = timezone.now()
    delivery.submission.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    client.force_login(owner)

    response = client.get(reverse("inventory:export", args=[delivery.pk]))

    assert response.status_code == 200
    assert response["Content-Disposition"].endswith('INV-100-final.xlsx"')
    assert response.content.startswith(b"PK")
    assert AuditEvent.objects.filter(
        action="inventory.final_workbook_exported", target_id=str(delivery.pk)
    ).exists()


@pytest.mark.django_db
def test_owner_cannot_export_unfinished_delivery(client, owner, delivery):
    client.force_login(owner)

    response = client.get(reverse("inventory:export", args=[delivery.pk]))

    assert response.status_code == 403


@pytest.mark.django_db
def test_owner_cannot_export_ready_delivery_before_evidence_approval(client, owner, delivery):
    variation = SquareCatalogVariation.objects.create(
        variation_id="VAR-NOT-APPROVED",
        item_id="ITEM-NOT-APPROVED",
        item_name="Test Bourbon",
        variation_name="750ML",
        track_inventory=True,
        present_at_location=True,
        location_id="LOC-1",
    )
    line = delivery.lines.get()
    line.square_catalog_variation_id = variation.pk
    line.square_item_name = str(variation)
    line.match_status = LineMatchStatus.MATCHED
    line.square_count_variation_id = variation.pk
    line.square_count_before = Decimal("10")
    line.projected_count_after = Decimal("34")
    line.square_count_snapshot_at = timezone.now()
    line.save()
    client.force_login(owner)

    response = client.get(reverse("inventory:export", args=[delivery.pk]))

    assert response.status_code == 403


@pytest.mark.django_db
def test_square_push_view_refuses_unapproved_evidence(client, owner, delivery):
    client.force_login(owner)

    with patch("apps.inventory.views.push_delivery_to_square") as push:
        response = client.post(
            reverse("inventory:push", args=[delivery.pk]),
            {"confirm": "on"},
        )

    assert response.status_code == 302
    push.assert_not_called()


@pytest.mark.django_db
def test_owner_can_refresh_live_counts_after_evidence_approval(client, owner, delivery):
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.approved_by = owner
    delivery.submission.approved_at = timezone.now()
    delivery.submission.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    client.force_login(owner)

    with patch("apps.inventory.views.refresh_square_counts") as refresh:
        refresh.return_value = SimpleNamespace(snapshots=[object()])
        response = client.post(reverse("inventory:refresh-counts", args=[delivery.pk]))

    assert response.status_code == 302
    refresh.assert_called_once_with(delivery, actor=owner)


@pytest.mark.django_db
def test_blocked_square_push_is_shown_and_audited(client, owner, delivery):
    delivery.submission.status = SubmissionStatus.APPROVED
    delivery.submission.save(update_fields=["status", "updated_at"])
    client.force_login(owner)

    with patch(
        "apps.inventory.views.push_delivery_to_square",
        side_effect=DeliveryNotReady("This vendor invoice already appears elsewhere."),
    ):
        response = client.post(
            reverse("inventory:push", args=[delivery.pk]),
            {"confirm": "on"},
            follow=True,
        )

    assert response.status_code == 200
    assert b"invoice already appears" in response.content
    event = AuditEvent.objects.get(action="inventory.square_push_blocked")
    assert event.actor == owner
    assert event.target_id == str(delivery.pk)
    assert "already appears" in event.detail["reason"]


@pytest.mark.django_db
def test_duplicate_variation_rows_show_the_grouped_delivery_delta(client, delivery):
    SquareCatalogVariation.objects.create(
        variation_id="VAR-SAME",
        item_id="ITEM-SAME",
        item_name="Grouped item",
        variation_name="750ML",
        track_inventory=True,
        present_at_location=True,
        location_id="LOC-1",
    )
    first = delivery.lines.get()
    first.square_catalog_variation_id = "VAR-SAME"
    first.match_status = LineMatchStatus.MATCHED
    first.received_units = Decimal("24")
    first.square_count_before = Decimal("10")
    first.square_count_variation_id = "VAR-SAME"
    first.projected_count_after = Decimal("40")
    first.save()
    DeliveryLine.objects.create(
        delivery=delivery,
        position=2,
        description="Same Square variation, second invoice row",
        units_per_case=1,
        received_units=Decimal("6"),
        square_catalog_variation_id="VAR-SAME",
        match_status=LineMatchStatus.MATCHED,
        square_count_before=Decimal("10"),
        square_count_variation_id="VAR-SAME",
        projected_count_after=Decimal("40"),
    )
    client.force_login(delivery.submission.submitted_by)

    response = client.get(reverse("inventory:delivery-detail", args=[delivery.pk]))

    assert response.status_code == 200
    assert response.content.count(b"All 2 matched lines") == 2
    assert [line.comparison_delta for line in response.context["lines"]] == [
        Decimal("30"),
        Decimal("30"),
    ]
