"""Credential-free protocol shared by the Django host and sandbox worker."""

from __future__ import annotations

MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
WORKFLOW_VERSION = "inventory_invoice_v1"
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
