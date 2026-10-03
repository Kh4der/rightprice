"""Credential-free protocol shared by the Django host and sandbox worker."""

from __future__ import annotations

import datetime as dt
from typing import Annotated
from uuid import UUID

from pydantic import Field

from apps.extraction.schemas import DeliveryInvoice, EvidenceValue, StrictSchema


class SandboxInvoiceSourceIdentity(StrictSchema):
    """Invoice identity read independently from one staged source document."""

    document_id: UUID = Field(description="Document UUID copied exactly from the input manifest.")
    vendor_name: EvidenceValue[str]
    invoice_number: EvidenceValue[str]
    invoice_date: EvidenceValue[dt.date]


class SandboxDeliveryInvoiceResult(DeliveryInvoice):
    """Aggregate invoice plus a mandatory identity reading for every source."""

    source_documents: Annotated[
        list[SandboxInvoiceSourceIdentity],
        Field(min_length=1, max_length=100),
    ]


MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
WORKFLOW_VERSION = "inventory_invoice_v3"
MANIFEST_NAME = "job-manifest.json"
SCHEMA_NAME = "delivery-invoice.schema.json"
RESULT_JSON_NAME = "inventory-result.json"
RESULT_XLSX_NAME = "corrected-inventory.xlsx"

INPUT_DIRECTORY = "input"
SCHEMA_DIRECTORY = "schema"
OUTPUT_DIRECTORY = "output"

RESULT_JSON_PATH = f"{OUTPUT_DIRECTORY}/{RESULT_JSON_NAME}"
RESULT_XLSX_PATH = f"{OUTPUT_DIRECTORY}/{RESULT_XLSX_NAME}"
SCHEMA_PATH = f"{SCHEMA_DIRECTORY}/{SCHEMA_NAME}"
