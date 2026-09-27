from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.capture.models import Document, DocumentType, Submission, SubmissionKind
from apps.inventory.claude_protocol import (
    MANAGED_AGENTS_BETA,
    MANIFEST_NAME,
    RESULT_JSON_PATH,
    RESULT_XLSX_PATH,
)
from apps.inventory.claude_sandbox import (
    InventorySandboxNotConfigured,
    InventorySandboxOutputError,
    InventorySandboxStateError,
    dispatch_inventory_sandbox_job,
    ingest_inventory_sandbox_outputs,
    stage_inventory_sandbox_workspace,
)
from apps.inventory.claude_worker import (
    WorkerConfigurationError,
    run_worker_once,
    validate_staged_workspace,
)
from apps.inventory.models import (
    Delivery,
    DeliveryLine,
    InventorySandboxJob,
    InventorySandboxJobStatus,
)
from apps.inventory.tasks import dispatch_claude_inventory_job


class FakeCreateSessions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="session_test_123")


class FakeCreateClient:
    def __init__(self):
        self.sessions = FakeCreateSessions()
        self.beta = SimpleNamespace(sessions=self.sessions)


def _invoice_image_bytes(*, image_format: str = "JPEG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (800, 1000), color=(246, 244, 238)).save(
        output,
        format=image_format,
    )
    return output.getvalue()


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "SANDBOX1",
        "owner-password",
        display_name="Sandbox Owner",
        role=Role.OWNER,
    )


@pytest.fixture
def demo_owner(db):
    return User.objects.create_user(
        "CLAUDEDEMO",
        "demo-password",
        display_name="Claude Demo Owner",
        role=Role.OWNER,
        is_demo=True,
        is_staff=False,
    )


@pytest.fixture
def delivery_with_invoice(owner):
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=owner,
    )
    invoice = _invoice_image_bytes()
    document = Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("invoice.jpg", invoice, content_type="image/jpeg"),
        original_name="invoice.jpg",
        media_type="image/jpeg",
        size_bytes=len(invoice),
        sha256=hashlib.sha256(invoice).hexdigest(),
        requested_type=DocumentType.DELIVERY_INVOICE,
    )
    delivery = Delivery.objects.create(submission=submission, invoice_number="INV-SANDBOX")
    DeliveryLine.objects.create(
        delivery=delivery,
        position=1,
        description="Bourbon 750ml",
        received_units=12,
    )
    return delivery, document


@override_settings(CLAUDE_INVENTORY_SANDBOX_ENABLED=False)
def test_dispatch_is_disabled_by_default(delivery_with_invoice, owner):
    delivery, _document = delivery_with_invoice

    with pytest.raises(InventorySandboxNotConfigured):
        dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())

    assert not InventorySandboxJob.objects.exists()


def test_demo_owned_delivery_fails_before_direct_or_celery_claude_dispatch(
    delivery_with_invoice,
    owner,
    demo_owner,
):
    delivery, _document = delivery_with_invoice
    submission = delivery.submission
    submission.submitted_by = demo_owner
    submission.save(update_fields=["submitted_by", "updated_at"])
    delivery.refresh_from_db()
    client = FakeCreateClient()
    original_delivery_status = delivery.status
    original_submission_status = submission.status

    with pytest.raises(InventorySandboxStateError, match="never sends invoice"):
        dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=client)
    with pytest.raises(InventorySandboxStateError, match="never sends invoice"):
        dispatch_claude_inventory_job.run(str(delivery.pk), str(owner.pk))

    delivery.refresh_from_db()
    submission.refresh_from_db()
    assert client.sessions.calls == []
    assert not InventorySandboxJob.objects.exists()
    assert delivery.status == original_delivery_status
    assert submission.status == original_submission_status


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
    CLAUDE_INVENTORY_MAX_COST_CENTS=175,
)
def test_dispatch_passes_only_opaque_metadata_and_no_resources(delivery_with_invoice, owner):
    delivery, document = delivery_with_invoice
    client = FakeCreateClient()

    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=client)

    assert job.status == InventorySandboxJobStatus.QUEUED
    assert job.session_id == "session_test_123"
    assert job.input_manifest["documents"][0]["document_id"] == str(document.id)
    call = client.sessions.calls[0]
    assert call["agent"] == "agent_test"
    assert call["environment_id"] == "env_test"
    assert call["betas"] == [MANAGED_AGENTS_BETA]
    assert call["budget"]["max_list_cost"] == {"amount": "175", "currency": "USD"}
    assert call["metadata"] == {
        "inventory_job_id": str(job.id),
        "workflow_version": "inventory_invoice_v1",
    }
    assert "resources" not in call
    assert "vault_ids" not in call
    serialized_call = json.dumps(call)
    assert document.file.name not in serialized_call
    assert document.original_name not in serialized_call
    assert AuditEvent.objects.filter(action="inventory.sandbox_queued", actor=owner).exists()


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_stage_uses_fixed_paths_and_rechecks_immutable_hash(
    delivery_with_invoice,
    owner,
    tmp_path,
):
    delivery, document = delivery_with_invoice
    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())
    workspace = tmp_path / "new-session-workspace"

    staged = stage_inventory_sandbox_workspace(job, workspace)

    job.refresh_from_db()
    assert job.status == InventorySandboxJobStatus.STAGED
    manifest = json.loads((staged / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["job_id"] == str(job.id)
    assert manifest["result_json_path"] == RESULT_JSON_PATH
    assert manifest["result_xlsx_path"] == RESULT_XLSX_PATH
    staged_document = staged / manifest["documents"][0]["path"]
    staged_bytes = staged_document.read_bytes()
    with Image.open(io.BytesIO(staged_bytes)) as staged_image:
        assert staged_image.format == "JPEG"
        assert staged_image.size == (800, 1000)
    assert manifest["documents"][0]["source_sha256"] == document.sha256
    assert manifest["documents"][0]["media_type"] == "image/jpeg"
    assert "exif_transpose" in manifest["documents"][0]["preprocessing_steps"]
    assert document.file.name not in (staged / MANIFEST_NAME).read_text(encoding="utf-8")
    assert validate_staged_workspace(staged, str(job.id)) == manifest
    assert AuditEvent.objects.filter(action="inventory.sandbox_staged").exists()

    staged_document.write_bytes(b"tampered")
    with pytest.raises(WorkerConfigurationError, match="size does not match"):
        validate_staged_workspace(staged, str(job.id))


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_stage_converts_iphone_heic_to_tool_readable_jpeg(
    owner,
    tmp_path,
):
    heic = _invoice_image_bytes(image_format="HEIF")
    submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=owner,
    )
    Document.objects.create(
        submission=submission,
        file=SimpleUploadedFile("invoice.heic", heic, content_type="image/heic"),
        original_name="invoice.heic",
        media_type="image/heic",
        size_bytes=len(heic),
        sha256=hashlib.sha256(heic).hexdigest(),
        requested_type=DocumentType.DELIVERY_INVOICE,
    )
    delivery = Delivery.objects.create(submission=submission, invoice_number="INV-HEIC")
    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())

    workspace = stage_inventory_sandbox_workspace(job, tmp_path / "heic-workspace")

    manifest = json.loads((workspace / MANIFEST_NAME).read_text(encoding="utf-8"))
    staged = manifest["documents"][0]
    assert staged["path"].endswith(".jpg")
    assert staged["media_type"] == "image/jpeg"
    assert staged["source_sha256"] == hashlib.sha256(heic).hexdigest()
    with Image.open(workspace / staged["path"]) as image:
        assert image.format == "JPEG"


def test_worker_refuses_square_or_application_credentials(tmp_path):
    environment = {
        "ANTHROPIC_ENVIRONMENT_ID": "env_test",
        "ANTHROPIC_ENVIRONMENT_KEY": "environment-secret",
        "ANTHROPIC_WORK_ID": "work_test",
        "ANTHROPIC_SESSION_ID": "session_test",
        "CLAUDE_SANDBOX_WORKDIR": str(tmp_path),
        "SQUARE_ACCESS_TOKEN": "must-not-enter-sandbox",
    }

    with pytest.raises(WorkerConfigurationError, match="SQUARE_ACCESS_TOKEN"):
        asyncio.run(run_worker_once(environment=environment, client=object()))


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_one_shot_worker_validates_session_and_scrubs_environment_key(
    delivery_with_invoice,
    owner,
    tmp_path,
):
    delivery, _document = delivery_with_invoice
    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())
    workspace = stage_inventory_sandbox_workspace(job, tmp_path / "worker-space")
    calls = {}

    class FakeAsyncSessions:
        async def retrieve(self, session_id, **kwargs):
            calls["retrieve"] = (session_id, kwargs)
            return SimpleNamespace(
                metadata={
                    "inventory_job_id": str(job.id),
                    "workflow_version": "inventory_invoice_v1",
                }
            )

    fake_client = SimpleNamespace(beta=SimpleNamespace(sessions=FakeAsyncSessions()))

    class FakeWorker:
        def __init__(self, client, **kwargs):
            calls["worker_init"] = (client, kwargs)

        async def handle_item(self, **kwargs):
            calls["handle_item"] = kwargs

    environment = {
        "ANTHROPIC_ENVIRONMENT_ID": "env_test",
        "ANTHROPIC_ENVIRONMENT_KEY": "environment-secret",
        "ANTHROPIC_WORK_ID": "work_test",
        "ANTHROPIC_SESSION_ID": "session_test_123",
        "ANTHROPIC_WORK_SECRET": "per-session-secret",
        "CLAUDE_SANDBOX_WORKDIR": str(workspace),
    }

    asyncio.run(
        run_worker_once(
            environment=environment,
            client=fake_client,
            worker_factory=FakeWorker,
        )
    )

    assert calls["retrieve"] == ("session_test_123", {"betas": [MANAGED_AGENTS_BETA]})
    assert calls["worker_init"][1]["workdir"] == Path(workspace)
    assert calls["worker_init"][1]["memory_sync_interval"] is None
    assert calls["handle_item"]["work_id"] == "work_test"
    assert calls["handle_item"]["work_secret"] == "per-session-secret"
    assert "ANTHROPIC_ENVIRONMENT_KEY" not in environment
    assert "ANTHROPIC_WORK_SECRET" not in environment


def _evidence(value=None, *, present=False):
    if not present:
        return {
            "value": None,
            "verbatim": None,
            "present": False,
            "legible": False,
            "location": "",
        }
    return {
        "value": value,
        "verbatim": str(value),
        "present": True,
        "legible": True,
        "location": "invoice header",
    }


def _valid_invoice_result():
    return {
        "vendor_name": _evidence("Distributor", present=True),
        "invoice_number": _evidence("INV-SANDBOX", present=True),
        "invoice_date": _evidence(dt.date(2026, 9, 26).isoformat(), present=True),
        "purchase_order_number": _evidence(),
        "lines": [
            {
                "line_number": _evidence(1, present=True),
                "vendor_sku": _evidence("SKU-CLAUDE", present=True),
                "upc": _evidence("012345678905", present=True),
                "description": _evidence("Claude Bourbon 750ml", present=True),
                "pack_text": _evidence("12/750ML", present=True),
                "cases": _evidence("1", present=True),
                "stated_units": _evidence("12", present=True),
                "unit_cost_cents": _evidence(500, present=True),
                "line_total_cents": _evidence(6000, present=True),
            }
        ],
        "subtotal_cents": _evidence(),
        "tax_cents": _evidence(),
        "fees_cents": _evidence(),
        "invoice_total_cents": _evidence(),
    }


def _values_only_workbook() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Corrected inventory" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c></row></sheetData>
</worksheet>""",
        )
    return output.getvalue()


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_ingest_validates_json_and_stores_workbook_in_fixed_private_path(
    delivery_with_invoice,
    owner,
    tmp_path,
):
    delivery, _document = delivery_with_invoice
    delivery.lines.all().delete()
    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())
    workspace = stage_inventory_sandbox_workspace(job, tmp_path / "ingest-space")
    (workspace / RESULT_JSON_PATH).write_text(
        json.dumps(_valid_invoice_result()),
        encoding="utf-8",
    )
    workbook = _values_only_workbook()
    (workspace / RESULT_XLSX_PATH).write_bytes(workbook)

    ingest_inventory_sandbox_outputs(job, workspace)

    job.refresh_from_db()
    assert job.status == InventorySandboxJobStatus.SUCCEEDED
    assert job.output_workbook.name == (
        f"inventory-sandbox/{job.id}/corrected-inventory.xlsx"
    )
    assert job.output_sha256 == hashlib.sha256(workbook).hexdigest()
    assert job.output_size_bytes == len(workbook)
    assert job.extracted_result["invoice_number"]["value"] == "INV-SANDBOX"
    delivery.refresh_from_db()
    line = delivery.lines.get()
    assert line.description == "Claude Bourbon 750ml"
    assert line.received_units == 12
    assert line.unit_cost_cents == 500
    assert delivery.submission.status == "READY"
    assert delivery.submission.documents.get().status == "EXTRACTED"
    assert AuditEvent.objects.filter(action="inventory.sandbox_succeeded").exists()


@override_settings(
    CLAUDE_INVENTORY_SANDBOX_ENABLED=True,
    CLAUDE_INVENTORY_AGENT_ID="agent_test",
    CLAUDE_INVENTORY_ENVIRONMENT_ID="env_test",
)
def test_ingest_rejects_numbers_that_cannot_fit_delivery_storage(
    delivery_with_invoice,
    owner,
    tmp_path,
):
    delivery, _document = delivery_with_invoice
    delivery.lines.all().delete()
    job = dispatch_inventory_sandbox_job(delivery, requested_by=owner, client=FakeCreateClient())
    workspace = stage_inventory_sandbox_workspace(job, tmp_path / "overflow-space")
    result = _valid_invoice_result()
    result["lines"][0]["cases"] = _evidence("1000000000.000", present=True)
    (workspace / RESULT_JSON_PATH).write_text(json.dumps(result), encoding="utf-8")
    (workspace / RESULT_XLSX_PATH).write_bytes(_values_only_workbook())

    with pytest.raises(InventorySandboxOutputError, match="invalid output"):
        ingest_inventory_sandbox_outputs(job, workspace)

    job.refresh_from_db()
    delivery.refresh_from_db()
    delivery.submission.refresh_from_db()
    assert job.status == InventorySandboxJobStatus.FAILED
    assert delivery.lines.count() == 0
    assert delivery.submission.status == "NEEDS_REVIEW"
    assert "original photos are saved" in delivery.submission.processing_error
