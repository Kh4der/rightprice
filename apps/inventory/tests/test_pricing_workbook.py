from __future__ import annotations

from decimal import Decimal

import pytest

from apps.accounts.models import User
from apps.capture.models import Submission, SubmissionKind, SubmissionStatus
from apps.inventory.models import (
    Delivery,
    DeliveryLine,
    DeliveryPricingPlan,
    LineMatchStatus,
    PricingPlanStatus,
)
from apps.inventory.workbooks import export_workbook, read_workbook


@pytest.mark.django_db
def test_final_workbook_includes_owner_reviewed_price_preview():
    employee = User.objects.create_user(
        login_code="PRICE_XLSX",
        password="employee-password",
        display_name="Workbook Employee",
    )
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        status=SubmissionStatus.APPROVED,
        submitted_by=employee,
    )
    delivery = Delivery.objects.create(submission=submission, invoice_number="PRICE-BOOK")
    line = DeliveryLine.objects.create(
        delivery=delivery,
        position=1,
        description="Example Vodka 750 mL",
        unit_cost_cents=1000,
        line_total_cents=12000,
        cases=Decimal("1"),
        loose_units=Decimal("0"),
        units_per_case=12,
        received_units=Decimal("12"),
        square_catalog_variation_id="VAR-VODKA",
        square_item_name="Example Vodka - 750 mL",
        match_status=LineMatchStatus.MATCHED,
    )
    DeliveryPricingPlan.objects.create(
        delivery=delivery,
        status=PricingPlanStatus.PREVIEWED,
        preview_lines=[
            {
                "variation_id": line.square_catalog_variation_id,
                "category": "Vodka",
                "markup_percent": "25",
                "current_price_cents": 1499,
                "target_price_cents": 1500,
            }
        ],
    )

    workbook_bytes = export_workbook(delivery)
    workbook = read_workbook(workbook_bytes)
    values = workbook.lines[0].values

    assert workbook.schema_version == "4"
    assert values["Pricing category"] == "Vodka"
    assert values["Markup %"] == Decimal("25")
    assert values["Square selling price before"] == Decimal("14.99")
    assert values["Proposed selling price"] == Decimal("15")
