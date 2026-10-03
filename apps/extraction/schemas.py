"""Strict, evidence-preserving schemas for vision extraction.

The image is the source of truth.  Every value copied from it therefore keeps
the exact nearby text which supports the value.  There is deliberately no
``confidence`` field: a model's self-reported confidence is not evidence and
must never be used to decide whether money or inventory can be posted.

All money is represented as integer cents.  A field that is absent or
unreadable remains ``None``; zero is only valid when ``0``/``$0.00`` is visibly
printed on the document.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictSchema(BaseModel):
    """Base configuration shared by every structured model response."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvidenceValue[T](StrictSchema):
    """A typed reading plus the pixels-as-text which justify that reading.

    ``present`` distinguishes a genuinely absent field from an illegible one.
    ``legible`` says whether the photographed text can actually support a typed
    value.  This prevents a missing amount from silently becoming zero.
    """

    value: T | None = Field(
        description="Typed value read from the image, or null when absent/illegible."
    )
    verbatim: str | None = Field(
        description="Exact visible source text, including currency signs and punctuation."
    )
    present: bool = Field(description="Whether this field is visibly printed on the document.")
    legible: bool = Field(description="Whether the printed field is sufficiently legible to read.")
    location: str = Field(
        description="Short visual location, such as 'PAYMENTS row, right column'."
    )

    @model_validator(mode="after")
    def evidence_matches_value(self) -> EvidenceValue[T]:
        if not self.present:
            if self.value is not None or self.verbatim is not None:
                raise ValueError("an absent field cannot contain a value or verbatim text")
            if self.legible:
                raise ValueError("an absent field cannot be marked legible")
        elif not self.legible and self.value is not None:
            raise ValueError("an illegible field cannot contain a typed value")
        elif self.legible:
            if self.value is None:
                raise ValueError("a legible field must contain a typed value")
            if not self.verbatim or not self.verbatim.strip():
                raise ValueError("a legible field must contain verbatim source text")
        return self

    @classmethod
    def observed(cls, value: T, verbatim: str, *, location: str = "") -> EvidenceValue[T]:
        """Convenience constructor for deterministic providers and tests."""

        return cls(
            value=value,
            verbatim=verbatim,
            present=True,
            legible=True,
            location=location,
        )

    @classmethod
    def absent(cls, *, location: str = "") -> EvidenceValue[T]:
        """Represent a field which is genuinely not printed."""

        return cls(value=None, verbatim=None, present=False, legible=False, location=location)

    @classmethod
    def unreadable(cls, *, verbatim: str | None = None, location: str = "") -> EvidenceValue[T]:
        """Represent visible text which cannot be read without guessing."""

        return cls(
            value=None,
            verbatim=verbatim,
            present=True,
            legible=False,
            location=location,
        )


# Short alias which reads well in type annotations and downstream imports.
Evidence = EvidenceValue


class ClassifiedDocumentType(StrEnum):
    SQUARE_SALES_REPORT = "SQUARE_SALES_REPORT"
    SQUARE_DRAWER_SCREEN = "SQUARE_DRAWER_SCREEN"
    LOTTERY_DAILY_SALES = "LOTTERY_DAILY_SALES"
    LOTTERY_TICKET_BALANCE = "LOTTERY_TICKET_BALANCE"
    LOTTERY_DRAW_SCHEDULE = "LOTTERY_DRAW_SCHEDULE"
    LOTTERY_PAYOUT = "LOTTERY_PAYOUT"
    DELIVERY_INVOICE = "DELIVERY_INVOICE"
    UNKNOWN = "UNKNOWN"


class DocumentClassification(StrictSchema):
    """First-pass result from the unenhanced, EXIF-oriented photograph."""

    document_type: EvidenceValue[ClassifiedDocumentType]
    document_count: EvidenceValue[Annotated[int, Field(ge=0, le=20)]]
    orientation_degrees: Literal[0, 90, 180, 270] = Field(
        description="Additional clockwise rotation needed after EXIF orientation."
    )
    entire_document_visible: bool = Field(
        description="False when important edges or totals are cut off."
    )
    notes: str = Field(description="Brief factual reason for the classification or retake need.")

    @property
    def classified_type(self) -> ClassifiedDocumentType:
        return self.document_type.value or ClassifiedDocumentType.UNKNOWN

    @property
    def count(self) -> int:
        return self.document_count.value or 0


class CategorySale(StrictSchema):
    name: EvidenceValue[str]
    quantity: EvidenceValue[int]
    amount_cents: EvidenceValue[int]


class SquareSalesReport(StrictSchema):
    report_date: EvidenceValue[dt.date]
    period_start: EvidenceValue[str]
    period_end: EvidenceValue[str]
    reported_at: EvidenceValue[str]
    team_scope: EvidenceValue[str]
    device_scope: EvidenceValue[str]

    gross_sales_cents: EvidenceValue[int]
    returns_cents: EvidenceValue[int]
    discounts_cents: EvidenceValue[int]
    net_sales_cents: EvidenceValue[int]
    tax_cents: EvidenceValue[int]
    tips_cents: EvidenceValue[int]
    gift_card_sales_cents: EvidenceValue[int]
    refunds_cents: EvidenceValue[int]
    total_cents: EvidenceValue[int]

    total_collected_cents: EvidenceValue[int]
    card_cents: EvidenceValue[int]
    cash_cents: EvidenceValue[int]
    fees_cents: EvidenceValue[int] = Field(
        description="Signed exactly as printed; Square normally prints fees as negative."
    )
    net_total_cents: EvidenceValue[int]
    category_sales: list[CategorySale]


class SquareDrawer(StrictSchema):
    started_at: EvidenceValue[str]
    started_by: EvidenceValue[str]
    drawer_state: EvidenceValue[Literal["OPEN", "CLOSED"]]
    starting_cash_cents: EvidenceValue[int]
    paid_in_out_cents: EvidenceValue[int] = Field(
        description="One signed net value exactly as shown; never invent separate paid-in/out."
    )
    cash_sales_cents: EvidenceValue[int]
    cash_refunds_cents: EvidenceValue[int]
    expected_in_drawer_cents: EvidenceValue[int]
    counted_cash_cents: EvidenceValue[int] = Field(
        description="Absent while the drawer is open; never substitute expected cash or zero."
    )
    over_short_cents: EvidenceValue[int]


class LotteryCountAmount(StrictSchema):
    count: EvidenceValue[int]
    amount_cents: EvidenceValue[int]


class LotteryDailySales(StrictSchema):
    report_date: EvidenceValue[dt.date]
    report_time: EvidenceValue[str]
    business_weekday: EvidenceValue[str]
    retailer_id: EvidenceValue[str]
    store_name: EvidenceValue[str]

    books_settled: LotteryCountAmount
    books_unsettled: LotteryCountAmount
    partial_return: LotteryCountAmount
    sales_commission_cents: EvidenceValue[int]
    pays: LotteryCountAmount
    cashing_commission_cents: EvidenceValue[int]
    claims: LotteryCountAmount
    books_received_count: EvidenceValue[int]
    books_activated_count: EvidenceValue[int]
    adjustments: LotteryCountAmount
    net_total_cents: EvidenceValue[int]


class LotteryTicketLine(StrictSchema):
    price_cents: EvidenceValue[int]
    game_name: EvidenceValue[str]
    status_code: EvidenceValue[str]
    game_number: EvidenceValue[str]
    book_number: EvidenceValue[str]
    range_start: EvidenceValue[int]
    range_end: EvidenceValue[int]
    sold_count: EvidenceValue[int]


class LotteryTicketBalance(StrictSchema):
    report_date: EvidenceValue[dt.date]
    report_time: EvidenceValue[str]
    retailer_id: EvidenceValue[str]
    register_number: EvidenceValue[str]
    shift_number: EvidenceValue[str]
    shift_started_at: EvidenceValue[str]
    lines: list[LotteryTicketLine]
    shift_total_cents: EvidenceValue[int]
    sold_total: EvidenceValue[int]


class LotteryPayout(StrictSchema):
    payout_date: EvidenceValue[dt.date]
    payout_time: EvidenceValue[str]
    amount_cents: EvidenceValue[int]
    game_name: EvidenceValue[str]
    ticket_reference: EvidenceValue[str]
    retailer_id: EvidenceValue[str]
    validation_reference: EvidenceValue[str]


# These limits mirror the storage fields used by the reviewed delivery draft.
# They are intentionally far above any real liquor-store invoice, while still
# preventing a schema-valid model response from overflowing PostgreSQL numeric
# columns during materialization.
InvoiceLineNumber = Annotated[int, Field(ge=1, le=1_000_000)]
InvoiceCases = Annotated[
    Decimal,
    Field(
        ge=Decimal("-999999999.999"),
        le=Decimal("999999999.999"),
        multiple_of=Decimal("0.001"),
    ),
]
InvoiceUnits = Annotated[
    Decimal,
    Field(
        ge=Decimal("-99999999999.999"),
        le=Decimal("99999999999.999"),
        multiple_of=Decimal("0.001"),
    ),
]
InvoiceMoneyCents = Annotated[
    int,
    Field(ge=-9_223_372_036_854_775_808, le=9_223_372_036_854_775_807),
]


class DeliveryInvoiceLine(StrictSchema):
    line_number: EvidenceValue[InvoiceLineNumber] = Field(
        description="Printed row number only; absent when the invoice has no row numbers."
    )
    vendor_sku: EvidenceValue[str] = Field(
        description="Distributor item/product number, not the UPC or Square identifier."
    )
    upc: EvidenceValue[str] = Field(
        description="Digits printed below the product barcode, preserving leading zeroes."
    )
    description: EvidenceValue[str] = Field(
        description="Full printed product description, including multipack wording."
    )
    pack_text: EvidenceValue[str] = Field(
        description=(
            "Case pack and item size in a parseable form such as 12/750ML, 6/1.75L, "
            "or 2/12/355ML when the case contains two consumer 12-packs."
        )
    )
    cases: EvidenceValue[InvoiceCases] = Field(
        description="Cases actually received, not cases ordered or backordered."
    )
    loose_units: EvidenceValue[InvoiceUnits] = Field(
        description=(
            "Loose bottles/cans actually received in addition to whole cases; this is the "
            "BTL/BT value, not bottles-per-case."
        )
    )
    stated_units: EvidenceValue[InvoiceUnits] = Field(
        description=(
            "Total received inventory units only when printed or unambiguous from fully "
            "legible case, loose-unit, and pack evidence; otherwise absent."
        )
    )
    unit_cost_cents: EvidenceValue[InvoiceMoneyCents] = Field(
        description=(
            "Net cost per received inventory unit. Leave absent when consumer multipack "
            "wording makes the Square sellable unit ambiguous."
        )
    )
    line_total_cents: EvidenceValue[InvoiceMoneyCents] = Field(
        description="Printed extended net amount for the quantity actually received."
    )


class DeliveryInvoice(StrictSchema):
    vendor_name: EvidenceValue[str]
    invoice_number: EvidenceValue[str]
    invoice_date: EvidenceValue[dt.date]
    purchase_order_number: EvidenceValue[str]
    lines: list[DeliveryInvoiceLine] = Field(max_length=1000)
    printed_total_cases: EvidenceValue[InvoiceUnits] = Field(
        description=(
            "Final invoice-footer total for cases actually received, such as TOTAL CASES "
            "or the first number in TOTAL CS/BTLS. Mark absent on a cropped page or when "
            "the invoice does not print this total."
        )
    )
    printed_total_loose_units: EvidenceValue[InvoiceUnits] = Field(
        description=(
            "Final invoice-footer total for loose bottles/cans received, such as TOTAL BOT "
            "or the second number in TOTAL CS/BTLS. This is not the total physical bottles "
            "inside full cases. Mark absent when the invoice does not print it."
        )
    )
    printed_total_physical_units: EvidenceValue[InvoiceUnits] = Field(
        description=(
            "Final invoice-footer physical bottle/can count, such as TOTAL BOTTLES. Do not "
            "substitute a calculated value or a Square sellable-unit count. Mark absent "
            "when this independent printed total is not present."
        )
    )
    subtotal_cents: EvidenceValue[InvoiceMoneyCents]
    tax_cents: EvidenceValue[InvoiceMoneyCents]
    fees_cents: EvidenceValue[InvoiceMoneyCents]
    invoice_total_cents: EvidenceValue[InvoiceMoneyCents]


ExtractionResult = (
    SquareSalesReport
    | SquareDrawer
    | LotteryDailySales
    | LotteryTicketBalance
    | LotteryPayout
    | DeliveryInvoice
)


SCHEMA_BY_DOCUMENT_TYPE: dict[ClassifiedDocumentType, type[StrictSchema]] = {
    ClassifiedDocumentType.SQUARE_SALES_REPORT: SquareSalesReport,
    ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: SquareDrawer,
    ClassifiedDocumentType.LOTTERY_DAILY_SALES: LotteryDailySales,
    ClassifiedDocumentType.LOTTERY_TICKET_BALANCE: LotteryTicketBalance,
    ClassifiedDocumentType.LOTTERY_PAYOUT: LotteryPayout,
    ClassifiedDocumentType.DELIVERY_INVOICE: DeliveryInvoice,
}


# Compatibility aliases with explicit ``Extraction`` suffixes.  They make call
# sites self-documenting without creating a second schema implementation.
ClassificationResult = DocumentClassification
SquareSalesExtraction = SquareSalesReport
SquareDrawerExtraction = SquareDrawer
LotteryDailySalesExtraction = LotteryDailySales
LotteryTicketBalanceExtraction = LotteryTicketBalance
LotteryPayoutExtraction = LotteryPayout
DeliveryInvoiceExtraction = DeliveryInvoice


def schema_for(document_type: str | ClassifiedDocumentType) -> type[StrictSchema]:
    """Return the only valid extraction schema for a classified document."""

    try:
        classified = ClassifiedDocumentType(document_type)
        return SCHEMA_BY_DOCUMENT_TYPE[classified]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"No extraction schema exists for {document_type!r}") from exc
