"""End-to-end document processing and submission status aggregation."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel

from apps.capture.models import (
    Document,
    DocumentStatus,
    DocumentType,
    Submission,
    SubmissionStatus,
)
from apps.reconcile import checks as reconciliation_checks

from .preprocess import Prepared, prepare
from .providers import VisionProvider, get_provider
from .schemas import (
    ClassifiedDocumentType,
    DeliveryInvoice,
    DocumentClassification,
    EvidenceValue,
    LotteryDailySales,
    LotteryTicketBalance,
    SquareDrawer,
    SquareSalesReport,
    StrictSchema,
    schema_for,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
TERMINAL_DOCUMENT_STATUSES = {
    DocumentStatus.EXTRACTED,
    DocumentStatus.REVIEWED,
    DocumentStatus.NEEDS_REVIEW,
    DocumentStatus.REJECTED,
}
REJECTED_CLASSIFICATIONS = {
    ClassifiedDocumentType.UNKNOWN,
    ClassifiedDocumentType.LOTTERY_DRAW_SCHEDULE,
}


def process_document(
    document_id: str | uuid.UUID,
    *,
    provider: VisionProvider | None = None,
    force: bool = False,
) -> Document:
    """Classify, prepare, extract, validate and persist one uploaded image.

    The original file is read but never modified.  Provider calls happen
    outside database transactions so a slow network response cannot hold a
    PostgreSQL row lock.  Each persistence step is a short atomic update.
    """

    document, claimed = _claim_document(document_id, force=force)
    if not claimed:
        return document

    aggregate_submission_status(document.submission_id)
    selected_provider = provider
    classification: DocumentClassification | None = None
    classification_image: Prepared | None = None

    try:
        selected_provider = selected_provider or get_provider()
        raw = _read_document(document)

        # Classification sees an EXIF-oriented, resized image without any
        # document-specific colour/contrast changes.  Those changes would bias
        # the classifier before it knows what kind of document it is seeing.
        classification_image = prepare(raw, doc_type=DocumentType.UNKNOWN)
        classification = selected_provider.classify(
            classification_image.data,
            media_type=classification_image.media_type,
        )
        detected_type = classification.classified_type

        rejection = _classification_rejection(document, classification)
        if rejection is not None:
            _mark_rejected(
                document,
                provider=selected_provider,
                classification=classification,
                prepared=classification_image,
                reason=rejection,
            )
            aggregate_submission_status(document.submission_id)
            return Document.objects.get(pk=document.pk)

        extraction_schema = schema_for(detected_type)
        prepared = prepare(
            raw,
            doc_type=detected_type.value,
            rotation_degrees=classification.orientation_degrees,
        )
        result = selected_provider.extract(
            prepared.data,
            media_type=prepared.media_type,
            document_type=detected_type,
            schema=extraction_schema,
        )

        check_results = validate_extraction(classification, result)
        needs_review = any(
            not item.get("passed", False) and item.get("severity", "hard") == "hard"
            for item in check_results
        )
        final_status = DocumentStatus.NEEDS_REVIEW if needs_review else DocumentStatus.EXTRACTED
        _mark_extracted(
            document,
            provider=selected_provider,
            classification=classification,
            prepared=prepared,
            result=result,
            check_results=check_results,
            status=final_status,
        )
    except Exception as exc:
        # Store a deliberately generic message.  Provider exception strings may
        # contain request metadata and must not become owner-visible database
        # content.  The traceback remains in restricted application logs.
        logger.exception(
            "Document extraction failed",
            extra={"document_id": str(document.pk), "exception_type": type(exc).__name__},
        )
        _mark_failed(
            document,
            provider=selected_provider,
            classification=classification,
            prepared=classification_image,
            exc=exc,
        )
        aggregate_submission_status(document.submission_id)
        raise

    aggregate_submission_status(document.submission_id)
    return Document.objects.get(pk=document.pk)


def process_submission(
    submission_id: str | uuid.UUID,
    *,
    provider: VisionProvider | None = None,
    force: bool = False,
) -> Submission:
    """Process every document, then materialize downstream workflow records.

    Each document owns its own failure state, so one bad photograph does not
    prevent the remaining photographs from being classified.  The hook runs
    only after no document remains uploaded/processing; it is idempotent and
    can therefore also be called after a retry.
    """

    submission = Submission.objects.select_related("submitted_by").get(pk=submission_id)
    if submission.submitted_by.is_demo:
        raise PermissionDenied("Practice mode never sends photos to an AI service.")
    document_ids = list(submission.documents.order_by("created_at").values_list("pk", flat=True))
    for document_id in document_ids:
        try:
            process_document(document_id, provider=provider, force=force)
        except Exception:
            # process_document has already persisted a safe FAILED state and a
            # restricted traceback. Continue so every photo gets a result.
            continue

    aggregate_submission_status(submission.pk)
    submission.refresh_from_db()
    unfinished = submission.documents.filter(
        status__in=[DocumentStatus.UPLOADED, DocumentStatus.PROCESSING]
    ).exists()
    if not unfinished:
        from .workflows import materialize_submission

        materialize_submission(submission)
    submission.refresh_from_db()
    return submission


def _claim_document(document_id: str | uuid.UUID, *, force: bool) -> tuple[Document, bool]:
    with transaction.atomic():
        document = (
            Document.objects.select_for_update()
            .select_related("submission__submitted_by")
            .get(pk=document_id)
        )
        if document.submission.submitted_by.is_demo:
            raise PermissionDenied("Practice mode never sends photos to an AI service.")
        if document.submission.is_terminal and not force:
            return document, False
        if document.status == DocumentStatus.PROCESSING and not force:
            # A duplicate delivery must not issue two paid model calls.
            return document, False
        if document.status in TERMINAL_DOCUMENT_STATUSES and not force:
            return document, False

        document.status = DocumentStatus.PROCESSING
        document.processing_error = ""
        document.processed_at = None
        document.save(update_fields=["status", "processing_error", "processed_at"])
        return document, True


def _read_document(document: Document) -> bytes:
    maximum = int(getattr(settings, "MAX_UPLOAD_BYTES", 15 * 1024 * 1024))
    with document.file.open("rb") as source:
        raw = source.read(maximum + 1)
    if not raw:
        raise ValueError("uploaded document is empty")
    if len(raw) > maximum:
        raise ValueError("uploaded document exceeds the configured size limit")
    return raw


def _classification_rejection(
    document: Document, classification: DocumentClassification
) -> str | None:
    if classification.count != 1:
        if classification.count > 1:
            return (
                f"Retake this photo with one document only; {classification.count} "
                "documents are visible."
            )
        return "Retake this photo with one complete document clearly visible."

    detected = classification.classified_type
    if detected is ClassifiedDocumentType.UNKNOWN:
        return "This document could not be identified. Retake it flat, sharp, and fully visible."
    if detected is ClassifiedDocumentType.LOTTERY_DRAW_SCHEDULE:
        return "This is a lottery draw schedule, not a sales or ticket-balance report."

    requested = document.requested_type
    if requested not in {DocumentType.AUTO, DocumentType.UNKNOWN} and requested != detected.value:
        return (
            f"The upload was labelled {requested}, but the photo shows {detected.value}. "
            "Retake or choose the correct document type."
        )
    return None


def _base_extracted_data(classification: DocumentClassification) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": classification.model_dump(mode="json"),
    }


def _mark_rejected(
    document: Document,
    *,
    provider: VisionProvider,
    classification: DocumentClassification,
    prepared: Prepared,
    reason: str,
) -> None:
    check = {
        "name": "classification",
        "passed": False,
        "severity": "hard",
        "detail": reason,
    }
    with transaction.atomic():
        Document.objects.filter(pk=document.pk).update(
            detected_type=classification.classified_type.value,
            status=DocumentStatus.REJECTED,
            extracted_data=_base_extracted_data(classification),
            check_results=[check],
            preparation_steps=list(prepared.steps),
            document_count=classification.count,
            processing_error=reason,
            provider=_bounded(provider.name, 30),
            model_name=_bounded(provider.classification_model, 100),
            processed_at=timezone.now(),
        )


def _mark_extracted(
    document: Document,
    *,
    provider: VisionProvider,
    classification: DocumentClassification,
    prepared: Prepared,
    result: StrictSchema,
    check_results: list[dict[str, Any]],
    status: str,
) -> None:
    payload = _base_extracted_data(classification)
    payload["result"] = result.model_dump(mode="json")
    with transaction.atomic():
        Document.objects.filter(pk=document.pk).update(
            detected_type=classification.classified_type.value,
            status=status,
            extracted_data=payload,
            check_results=check_results,
            preparation_steps=list(prepared.steps),
            document_count=classification.count,
            processing_error="",
            provider=_bounded(provider.name, 30),
            model_name=_bounded(provider.extraction_model, 100),
            processed_at=timezone.now(),
        )


def _mark_failed(
    document: Document,
    *,
    provider: VisionProvider | None,
    classification: DocumentClassification | None,
    prepared: Prepared | None,
    exc: Exception,
) -> None:
    payload = _base_extracted_data(classification) if classification is not None else {}
    if provider is None:
        provider_name = getattr(settings, "EXTRACTION_PROVIDER", "")
        model_name = getattr(settings, "EXTRACTION_MODEL", "")
    else:
        provider_name = provider.name
        model_name = (
            provider.extraction_model
            if classification is not None
            else provider.classification_model
        )
    with transaction.atomic():
        Document.objects.filter(pk=document.pk).update(
            detected_type=(
                classification.classified_type.value
                if classification is not None
                else DocumentType.UNKNOWN
            ),
            status=DocumentStatus.FAILED,
            extracted_data=payload,
            check_results=[],
            preparation_steps=list(prepared.steps) if prepared is not None else [],
            document_count=classification.count if classification is not None else 1,
            processing_error=_safe_failure_message(exc),
            provider=_bounded(provider_name, 30),
            model_name=_bounded(model_name, 100),
            processed_at=timezone.now(),
        )


def _safe_failure_message(exc: Exception) -> str:
    exception_name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:80] or "Error"
    return f"Document processing failed ({exception_name}). Retry the photo or ask the owner."


def _bounded(value: object, maximum: int) -> str:
    return str(value)[:maximum]


def aggregate_submission_status(submission_id: str | uuid.UUID) -> str:
    """Reduce document states into one user-facing submission state."""

    with transaction.atomic():
        submission = Submission.objects.select_for_update().get(pk=submission_id)
        if submission.is_terminal:
            return submission.status

        documents = list(submission.documents.only("status", "processing_error"))
        statuses = {document.status for document in documents}

        if not documents:
            status = SubmissionStatus.DRAFT
        elif DocumentStatus.PROCESSING in statuses:
            status = SubmissionStatus.PROCESSING
        elif DocumentStatus.UPLOADED in statuses:
            status = SubmissionStatus.QUEUED
        elif DocumentStatus.FAILED in statuses:
            status = SubmissionStatus.FAILED
        elif statuses & {DocumentStatus.NEEDS_REVIEW, DocumentStatus.REJECTED}:
            status = SubmissionStatus.NEEDS_REVIEW
        elif statuses and statuses <= {DocumentStatus.EXTRACTED, DocumentStatus.REVIEWED}:
            status = SubmissionStatus.READY
        else:  # Defensive fallback for a future document status.
            status = SubmissionStatus.NEEDS_REVIEW

        errors = [
            document.processing_error
            for document in documents
            if document.status == DocumentStatus.FAILED and document.processing_error
        ]
        processing_error = "\n".join(errors)[:4000] if status == SubmissionStatus.FAILED else ""
        if submission.status != status or submission.processing_error != processing_error:
            submission.status = status
            submission.processing_error = processing_error
            submission.save(update_fields=["status", "processing_error", "updated_at"])
        return status


def validate_extraction(
    classification: DocumentClassification, result: StrictSchema
) -> list[dict[str, Any]]:
    """Run evidence completeness and deterministic arithmetic checks."""

    checks: list[dict[str, Any]] = []
    if not classification.entire_document_visible:
        if isinstance(result, DeliveryInvoice):
            checks.append(
                {
                    "name": "complete_document_visible",
                    "passed": False,
                    "severity": "warning",
                    "detail": (
                        "This is one section of a long invoice. Make sure the submission "
                        "also includes the header, an overlapping product row, and totals."
                    ),
                }
            )
        else:
            checks.append(
                _failed_check(
                    "complete_document_visible",
                    "The classifier found a cropped edge or missing total; retake the photo.",
                )
            )

    for path, evidence in _walk_evidence(result):
        if evidence.present and not evidence.legible:
            checks.append(
                _failed_check(
                    f"evidence:{path}",
                    "The field is visible but cannot be read without guessing.",
                )
            )

    checks.extend(_required_evidence_checks(result))
    checks.extend(_arithmetic_checks(result))
    return checks


def _walk_evidence(value: Any, path: str = "") -> Iterable[tuple[str, EvidenceValue[Any]]]:
    if isinstance(value, EvidenceValue):
        yield path, value
        return
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            child_path = f"{path}.{field_name}" if path else field_name
            yield from _walk_evidence(getattr(value, field_name), child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_evidence(child, f"{path}[{index}]")


def _required_evidence_checks(result: StrictSchema) -> list[dict[str, Any]]:
    required: list[str]
    if isinstance(result, SquareSalesReport):
        required = [
            "report_date",
            "gross_sales_cents",
            "returns_cents",
            "discounts_cents",
            "net_sales_cents",
            "tax_cents",
            "tips_cents",
            "gift_card_sales_cents",
            "refunds_cents",
            "total_cents",
            "total_collected_cents",
            "card_cents",
            "cash_cents",
            "fees_cents",
            "net_total_cents",
        ]
    elif isinstance(result, SquareDrawer):
        required = [
            "started_at",
            "started_by",
            "drawer_state",
            "starting_cash_cents",
            "paid_in_out_cents",
            "cash_sales_cents",
            "cash_refunds_cents",
            "expected_in_drawer_cents",
        ]
        if result.drawer_state.value == "CLOSED":
            required.extend(["counted_cash_cents", "over_short_cents"])
    elif isinstance(result, LotteryDailySales):
        required = [
            "report_date",
            "business_weekday",
            "retailer_id",
            "pays.count",
            "pays.amount_cents",
            "net_total_cents",
        ]
    elif isinstance(result, LotteryTicketBalance):
        required = [
            "report_date",
            "retailer_id",
            "shift_total_cents",
            "sold_total",
        ]
        if not result.lines:
            return [_failed_check("evidence:lines", "No ticket-balance rows were extracted.")]
        for index in range(len(result.lines)):
            required.extend(
                [
                    f"lines[{index}].price_cents",
                    f"lines[{index}].game_name",
                    f"lines[{index}].game_number",
                    f"lines[{index}].book_number",
                    f"lines[{index}].range_start",
                    f"lines[{index}].range_end",
                    f"lines[{index}].sold_count",
                ]
            )
    elif isinstance(result, DeliveryInvoice):
        # A photographed page is not necessarily a whole invoice. Continuation
        # pages often omit the header, and only the final page may carry totals.
        # Header completeness is checked after all pages are combined.
        required = []
        if not result.lines:
            has_invoice_identity = any(
                evidence.value is not None
                for evidence in (
                    result.vendor_name,
                    result.invoice_number,
                    result.invoice_date,
                    result.invoice_total_cents,
                )
            )
            if not has_invoice_identity:
                return [_failed_check("evidence:lines", "No invoice content was extracted.")]
        for index in range(len(result.lines)):
            required.extend(
                [
                    f"lines[{index}].description",
                    f"lines[{index}].pack_text",
                    f"lines[{index}].cases",
                    f"lines[{index}].loose_units",
                    f"lines[{index}].line_total_cents",
                ]
            )
    else:  # Lottery payout
        required = ["payout_date", "amount_cents", "game_name", "ticket_reference"]

    checks: list[dict[str, Any]] = []
    for path in required:
        evidence = _get_path(result, path)
        if not isinstance(evidence, EvidenceValue) or evidence.value is None:
            checks.append(_failed_check(f"evidence:{path}", "Required evidence is missing."))
    return checks


def _get_path(value: Any, path: str) -> Any:
    current = value
    for name, index_text in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?", path):
        current = getattr(current, name)
        if index_text:
            current = current[int(index_text)]
    return current


def _arithmetic_checks(result: StrictSchema) -> list[dict[str, Any]]:
    if isinstance(result, SquareSalesReport):
        return _sales_checks(result)
    if isinstance(result, SquareDrawer):
        return _drawer_checks(result)
    if isinstance(result, LotteryDailySales):
        return _lottery_daily_checks(result)
    if isinstance(result, LotteryTicketBalance):
        return _ticket_balance_checks(result)
    if isinstance(result, DeliveryInvoice):
        return _delivery_checks(result)
    return []


def _sales_checks(result: SquareSalesReport) -> list[dict[str, Any]]:
    names = [
        "gross_sales_cents",
        "returns_cents",
        "discounts_cents",
        "net_sales_cents",
        "tax_cents",
        "tips_cents",
        "gift_card_sales_cents",
        "refunds_cents",
        "total_cents",
        "total_collected_cents",
        "card_cents",
        "cash_cents",
        "fees_cents",
        "net_total_cents",
    ]
    values = {name: getattr(result, name).value for name in names}
    if any(value is None for value in values.values()):
        return []

    categories: list[reconciliation_checks.CategorySale] = []
    for category in result.category_sales:
        if all(
            item.value is not None
            for item in (category.name, category.quantity, category.amount_cents)
        ):
            categories.append(
                reconciliation_checks.CategorySale(
                    name=str(category.name.value),
                    quantity=int(category.quantity.value),
                    amount_cents=int(category.amount_cents.value),
                )
            )

    report = reconciliation_checks.SalesReport(
        **{name: int(value) for name, value in values.items()},
        category_sales=categories,
    )
    return [
        _reconciliation_check(item)
        for item in reconciliation_checks.run_sales_report_checks(report)
    ]


def _drawer_checks(result: SquareDrawer) -> list[dict[str, Any]]:
    names = [
        "starting_cash_cents",
        "paid_in_out_cents",
        "cash_sales_cents",
        "cash_refunds_cents",
        "expected_in_drawer_cents",
    ]
    values = {name: getattr(result, name).value for name in names}
    if any(value is None for value in values.values()):
        return []
    drawer = reconciliation_checks.DrawerSnapshot(
        **{name: int(value) for name, value in values.items()},
        counted_cash_cents=(
            int(result.counted_cash_cents.value)
            if result.counted_cash_cents.value is not None
            else None
        ),
    )
    checks = [
        _reconciliation_check(item) for item in reconciliation_checks.run_drawer_checks(drawer)
    ]
    if drawer.over_short_cents is not None and result.over_short_cents.value is not None:
        checks.append(
            _numeric_check(
                "over_short = counted_cash - expected",
                drawer.over_short_cents,
                int(result.over_short_cents.value),
            )
        )
    return checks


def _lottery_daily_checks(result: LotteryDailySales) -> list[dict[str, Any]]:
    if result.report_date.value is None or result.business_weekday.value is None:
        return []
    expected = result.report_date.value.strftime("%A").upper()
    actual = str(result.business_weekday.value).strip().upper()
    return [
        {
            "name": "weekday matches report_date",
            "passed": expected == actual,
            "severity": "hard",
            "expected": expected,
            "actual": actual,
            "detail": "The printed weekday independently checks the printed date.",
        }
    ]


def _ticket_balance_checks(result: LotteryTicketBalance) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    priced_rows = [
        (line.price_cents.value, line.sold_count.value)
        for line in result.lines
        if line.price_cents.value is not None and line.sold_count.value is not None
    ]
    if len(priced_rows) == len(result.lines) and result.shift_total_cents.value is not None:
        expected = sum(int(price) * int(sold) for price, sold in priced_rows)
        checks.append(
            _numeric_check(
                "shift_total = sum(price x sold)",
                expected,
                int(result.shift_total_cents.value),
            )
        )
    if len(priced_rows) == len(result.lines) and result.sold_total.value is not None:
        expected_sold = sum(int(sold) for _, sold in priced_rows)
        checks.append(
            _numeric_check("sold_total = sum(sold)", expected_sold, int(result.sold_total.value))
        )

    for index, line in enumerate(result.lines):
        if line.game_name.value is None or line.game_number.value is None:
            continue
        match = re.match(r"(\d+)", str(line.game_name.value))
        if match:
            expected_game = str(line.game_number.value).lstrip("0") or "0"
            actual_game = match.group(1).lstrip("0") or "0"
            checks.append(
                {
                    "name": f"lines[{index}].game_name prefix = game_number",
                    "passed": expected_game == actual_game,
                    "severity": "hard",
                    "expected": expected_game,
                    "actual": actual_game,
                    "detail": "The game number is printed twice on the ticket-balance row.",
                }
            )
    return checks


def _delivery_checks(result: DeliveryInvoice) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    line_totals = [line.line_total_cents.value for line in result.lines]
    if (
        line_totals
        and all(value is not None for value in line_totals)
        and result.subtotal_cents.value is not None
    ):
        checks.append(
            _numeric_check(
                "subtotal = sum(line_totals)",
                sum(int(value) for value in line_totals),
                int(result.subtotal_cents.value),
            )
        )
    total_parts = (
        result.subtotal_cents.value,
        result.tax_cents.value,
        result.fees_cents.value,
        result.invoice_total_cents.value,
    )
    if all(value is not None for value in total_parts):
        subtotal, tax, fees, total = (int(value) for value in total_parts)
        checks.append(
            _numeric_check("invoice_total = subtotal + tax + fees", subtotal + tax + fees, total)
        )
    return checks


def _reconciliation_check(check: reconciliation_checks.CheckResult) -> dict[str, Any]:
    return {
        "name": check.name,
        "passed": check.passed,
        "severity": check.severity.value,
        "expected_cents": check.expected_cents,
        "actual_cents": check.actual_cents,
        "delta_cents": check.delta_cents,
        "detail": check.detail,
    }


def _numeric_check(
    name: str, expected: int, actual: int, *, severity: str = "hard", detail: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": expected == actual,
        "severity": severity,
        "expected": expected,
        "actual": actual,
        "delta": actual - expected,
        "detail": detail,
    }


def _failed_check(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": False, "severity": "hard", "detail": detail}
