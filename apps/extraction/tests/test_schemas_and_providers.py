from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import ValidationError

from apps.extraction.providers import (
    DELIVERY_INVOICE_EXTRACTION_PROMPT,
    FakeVisionProvider,
    OpenAIVisionProvider,
    ProviderResponseError,
)
from apps.extraction.schemas import (
    ClassifiedDocumentType,
    DeliveryInvoice,
    DocumentClassification,
    EvidenceValue,
    SquareDrawer,
)
from apps.extraction.tasks import process_submission as process_submission_task


def observed(value, text: str | None = None, location: str = "row"):
    return {
        "value": value,
        "verbatim": str(value) if text is None else text,
        "present": True,
        "legible": True,
        "location": location,
    }


def absent():
    return {"value": None, "verbatim": None, "present": False, "legible": False, "location": ""}


def classification(document_type=ClassifiedDocumentType.SQUARE_DRAWER_SCREEN, count=1):
    return DocumentClassification.model_validate(
        {
            "document_type": observed(document_type, document_type.value, "heading"),
            "document_count": observed(count, str(count), "frame"),
            "orientation_degrees": 0,
            "entire_document_visible": True,
            "notes": "heading and screen layout",
        }
    )


def drawer_payload():
    return {
        "started_at": observed("9/26/26, 10:12 AM"),
        "started_by": observed("employee"),
        "drawer_state": observed("OPEN"),
        "starting_cash_cents": observed(26_500, "$265.00"),
        "paid_in_out_cents": observed(0, "$0.00"),
        "cash_sales_cents": observed(3_491, "$34.91"),
        "cash_refunds_cents": observed(0, "$0.00"),
        "expected_in_drawer_cents": observed(29_991, "$299.91"),
        "counted_cash_cents": absent(),
        "over_short_cents": absent(),
    }


def invoice_payload():
    return {
        "vendor_name": observed("Distributor"),
        "invoice_number": observed("INV-1"),
        "invoice_date": observed("2026-09-26"),
        "purchase_order_number": absent(),
        "lines": [],
        "printed_total_cases": absent(),
        "printed_total_loose_units": absent(),
        "printed_total_physical_units": absent(),
        "subtotal_cents": absent(),
        "tax_cents": absent(),
        "fees_cents": absent(),
        "invoice_total_cents": absent(),
    }


def test_evidence_distinguishes_printed_zero_from_absent_value():
    zero = EvidenceValue[int].observed(0, "$0.00")
    missing = EvidenceValue[int].absent()

    assert zero.value == 0
    assert zero.present and zero.legible
    assert missing.value is None
    assert not missing.present and not missing.legible


@pytest.mark.parametrize(
    "bad",
    [
        {"value": 0, "verbatim": None, "present": False, "legible": False, "location": ""},
        {"value": 100, "verbatim": "$1.00", "present": True, "legible": False, "location": ""},
        {"value": 100, "verbatim": "", "present": True, "legible": True, "location": ""},
    ],
)
def test_evidence_rejects_contradictory_states(bad):
    with pytest.raises(ValidationError):
        EvidenceValue[int].model_validate(bad)


def test_schemas_forbid_model_reported_confidence():
    with pytest.raises(ValidationError):
        EvidenceValue[int].model_validate({**observed(1), "confidence": 0.99})

    assert "confidence" not in str(SquareDrawer.model_json_schema()).lower()


def test_literal_evidence_is_strictly_typed():
    with pytest.raises(ValidationError):
        EvidenceValue[Literal["OPEN", "CLOSED"]].model_validate(observed("maybe"))


def test_fake_provider_validates_queued_dicts_and_records_calls():
    fake = FakeVisionProvider(
        classifications=classification().model_dump(mode="json"),
        extractions={ClassifiedDocumentType.SQUARE_DRAWER_SCREEN: drawer_payload()},
    )

    classified = fake.classify(b"raw", media_type="image/jpeg")
    result = fake.extract(
        b"prepared",
        media_type="image/jpeg",
        document_type=classified.classified_type,
        schema=SquareDrawer,
    )

    assert result.expected_in_drawer_cents.value == 29_991
    assert [call.operation for call in fake.calls] == ["classify", "extract"]
    assert fake.calls[1].schema_name == "SquareDrawer"


class RecordingResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.parsed, status="completed", output=[])


def test_openai_adapter_uses_responses_parse_with_pydantic_and_data_url():
    responses = RecordingResponses(classification())
    client = SimpleNamespace(responses=responses)
    provider = OpenAIVisionProvider(
        client=client,
        classification_model="classifier-test",
        extraction_model="extractor-test",
    )

    result = provider.classify(b"jpeg bytes", media_type="image/jpeg")

    assert result.classified_type is ClassifiedDocumentType.SQUARE_DRAWER_SCREEN
    assert responses.kwargs["model"] == "classifier-test"
    assert responses.kwargs["text_format"] is DocumentClassification
    assert responses.kwargs["store"] is False
    image_part = responses.kwargs["input"][1]["content"][1]
    assert image_part["image_url"].startswith("data:image/jpeg;base64,")
    assert image_part["detail"] == "high"


def test_delivery_adapter_explains_real_distributor_columns_and_overlaps():
    responses = RecordingResponses(DeliveryInvoice.model_validate(invoice_payload()))
    provider = OpenAIVisionProvider(
        client=SimpleNamespace(responses=responses),
        classification_model="classifier-test",
        extraction_model="extractor-test",
    )

    provider.extract(
        b"jpeg bytes",
        media_type="image/jpeg",
        document_type=ClassifiedDocumentType.DELIVERY_INVOICE,
        schema=DeliveryInvoice,
    )

    system_prompt = responses.kwargs["input"][0]["content"]
    assert DELIVERY_INVOICE_EXTRACTION_PROMPT in system_prompt
    assert "CS/BT" in system_prompt
    assert "BPC and QPC" in system_prompt
    assert "overlapping section" in system_prompt
    assert "Keep that product row in `lines`" in system_prompt
    assert "zero price or zero total alone" in system_prompt
    assert "TOTAL CS/BTLS" in system_prompt
    assert (
        "loose_units"
        in DeliveryInvoice.model_json_schema()["$defs"]["DeliveryInvoiceLine"]["properties"]
    )
    assert {
        "printed_total_cases",
        "printed_total_loose_units",
        "printed_total_physical_units",
    } <= DeliveryInvoice.model_json_schema()["properties"].keys()


def test_openai_adapter_rejects_an_empty_structured_response():
    responses = RecordingResponses(None)
    provider = OpenAIVisionProvider(
        client=SimpleNamespace(responses=responses),
        classification_model="classifier-test",
        extraction_model="extractor-test",
    )

    with pytest.raises(ProviderResponseError, match="no structured extraction"):
        provider.classify(b"jpeg bytes", media_type="image/jpeg")


def test_capture_imports_a_real_celery_submission_task():
    assert callable(process_submission_task.delay)
    assert process_submission_task.name == "extraction.process_submission"
