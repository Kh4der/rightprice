"""Inventory deliveries, catalogue mappings and Square push state."""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class DeliveryStatus(models.TextChoices):
    EXTRACTING = "EXTRACTING", "Extracting"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
    READY = "READY", "Ready to post"
    PUSHING = "PUSHING", "Posting to Square"
    PUSHED = "PUSHED", "Posted to Square"
    PUSHED_WITH_DRIFT = "PUSHED_WITH_DRIFT", "Posted; count needs review"
    PUSHED_UNVERIFIED = "PUSHED_UNVERIFIED", "Posted; verification unavailable"
    FAILED = "FAILED", "Post failed"


class LineMatchStatus(models.TextChoices):
    UNMATCHED = "UNMATCHED", "Unmatched"
    SUGGESTED = "SUGGESTED", "Suggested match"
    MATCHED = "MATCHED", "Matched"
    EXCLUDED = "EXCLUDED", "Non-stock line"


class InventorySandboxJobStatus(models.TextChoices):
    """Lifecycle of one self-hosted Claude invoice-processing session."""

    PENDING = "PENDING", "Preparing session"
    QUEUED = "QUEUED", "Queued for sandbox"
    STAGED = "STAGED", "Invoice files staged"
    RUNNING = "RUNNING", "Processing in sandbox"
    SUCCEEDED = "SUCCEEDED", "Output received"
    FAILED = "FAILED", "Processing failed"


class CatalogCreationStatus(models.TextChoices):
    CLAIMED = "CLAIMED", "Protected request claimed"
    SUCCEEDED = "SUCCEEDED", "Created in Square"
    FAILED = "FAILED", "Request failed; safe retry required"


class PricingPlanStatus(models.TextChoices):
    """Lifecycle of an owner-reviewed delivery price update."""

    DRAFT = "DRAFT", "Draft"
    PREVIEWED = "PREVIEWED", "Ready to update"
    PUSHING = "PUSHING", "Updating Square"
    PUSHED = "PUSHED", "Updated in Square"
    FAILED = "FAILED", "Update failed"


class Vendor(models.Model):
    name = models.CharField(max_length=160, unique=True)
    square_vendor_id = models.CharField(max_length=64, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CatalogMapping(models.Model):
    """A reviewed vendor-line to Square item-variation mapping."""

    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="catalog_mappings",
    )
    vendor_sku = models.CharField(max_length=100, blank=True)
    upc = models.CharField(max_length=14, blank=True, db_index=True)
    description = models.CharField(max_length=300)
    square_catalog_variation_id = models.CharField(max_length=64, db_index=True)
    square_item_name = models.CharField(max_length=300, blank=True)
    units_per_case = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    last_verified_at = models.DateTimeField(default=timezone.now)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verified_catalog_mappings",
    )

    class Meta:
        ordering = ["description"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "vendor_sku"],
                condition=~models.Q(vendor_sku=""),
                name="unique_vendor_sku_mapping",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.description} -> {self.square_catalog_variation_id}"


class SquareCatalogVariation(models.Model):
    """Read-only local cache of stockable Square catalogue variations."""

    variation_id = models.CharField(max_length=64, primary_key=True)
    item_id = models.CharField(max_length=64, blank=True, db_index=True)
    item_name = models.CharField(max_length=300, blank=True)
    variation_name = models.CharField(max_length=300, blank=True)
    sku = models.CharField(max_length=100, blank=True, db_index=True)
    upc = models.CharField(max_length=14, blank=True, db_index=True)
    gtin = models.CharField(max_length=14, blank=True, db_index=True)
    track_inventory = models.BooleanField(default=False)
    present_at_location = models.BooleanField(default=False, db_index=True)
    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    default_unit_cost_cents = models.BigIntegerField(null=True, blank=True)
    default_unit_cost_currency = models.CharField(max_length=3, blank=True)
    default_unit_cost_vendor_id = models.CharField(max_length=64, blank=True)
    vendor_costs = models.JSONField(default=list, blank=True)
    current_price_cents = models.BigIntegerField(null=True, blank=True)
    current_price_currency = models.CharField(max_length=3, blank=True)
    pricing_type = models.CharField(max_length=32, blank=True)
    catalog_version = models.BigIntegerField(null=True, blank=True)
    reporting_category_id = models.CharField(max_length=64, blank=True, db_index=True)
    reporting_category_name = models.CharField(max_length=300, blank=True)
    category_path = models.JSONField(default=list, blank=True)
    price_from_location_override = models.BooleanField(default=False)
    catalog_object_snapshot = models.JSONField(default=dict, blank=True)
    synced_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["item_name", "variation_name", "variation_id"]
        indexes = [
            models.Index(fields=["sku", "present_at_location"]),
            models.Index(fields=["upc", "present_at_location"]),
            models.Index(fields=["gtin", "present_at_location"]),
        ]

    def __str__(self) -> str:
        label = " - ".join(part for part in (self.item_name, self.variation_name) if part)
        return label or self.variation_id

    def cost_snapshot_for_vendor(self, vendor_id: str = "") -> tuple[int | None, str, str]:
        """Return the applicable Square unit cost and an auditable source label.

        Square defines the first ``vendor_information`` entry as the default.
        A delivery whose local vendor has a Square vendor ID uses that exact
        entry when present; otherwise the catalog default is shown explicitly.
        """

        entries = self.vendor_costs if isinstance(self.vendor_costs, list) else []
        normalized_vendor_id = str(vendor_id or "").strip()
        selected = None
        exact_vendor = False
        if normalized_vendor_id:
            selected = next(
                (
                    entry
                    for entry in entries
                    if isinstance(entry, dict)
                    and str(entry.get("vendor_id") or "") == normalized_vendor_id
                ),
                None,
            )
            exact_vendor = selected is not None
        if selected is None and entries:
            selected = entries[0] if isinstance(entries[0], dict) else None

        if selected is not None:
            amount = selected.get("amount")
            amount = amount if isinstance(amount, int) and not isinstance(amount, bool) else None
            currency = str(selected.get("currency") or "")[:3].upper()
            selected_vendor_id = str(selected.get("vendor_id") or "")
            if exact_vendor:
                source = f"Square vendor {selected_vendor_id}"
            elif selected_vendor_id:
                source = f"Square default · vendor {selected_vendor_id}"
            else:
                source = "Square default"
            return amount, currency, source

        source = ""
        if self.default_unit_cost_cents is not None:
            if self.default_unit_cost_vendor_id:
                source = f"Square default · vendor {self.default_unit_cost_vendor_id}"
            else:
                source = "Square default"
        return self.default_unit_cost_cents, self.default_unit_cost_currency, source


class Delivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.OneToOneField(
        "capture.Submission",
        on_delete=models.CASCADE,
        related_name="delivery",
    )
    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    vendor_name_raw = models.CharField(max_length=160, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    invoice_total_cents = models.BigIntegerField(null=True, blank=True)
    # Independent footer quantities from distributor invoices. Their complete
    # evidence remains on Document.extracted_data; these values make it possible
    # to guard the owner-reviewed, de-duplicated line set before any Square write.
    printed_total_cases = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    printed_total_loose_units = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    printed_total_physical_units = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    status = models.CharField(
        max_length=24,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.EXTRACTING,
        db_index=True,
    )
    spreadsheet_revision = models.PositiveIntegerField(default=1)
    square_batch_keys = models.JSONField(default=list, blank=True)
    square_result = models.JSONField(default=dict, blank=True)
    pushed_at = models.DateTimeField(null=True, blank=True)
    pushed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inventory_pushes",
    )
    push_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        number = self.invoice_number or str(self.id)[:8]
        return f"Delivery {number}"

    @property
    def unresolved_line_count(self) -> int:
        resolved = [LineMatchStatus.MATCHED, LineMatchStatus.EXCLUDED]
        return self.lines.exclude(match_status__in=resolved).count()


class DeliveryLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField()
    vendor_sku = models.CharField(max_length=100, blank=True)
    upc = models.CharField(max_length=14, blank=True)
    description = models.CharField(max_length=300)
    pack_text = models.CharField(max_length=80, blank=True)
    cases = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # Distributor invoices commonly split quantity into full cases and loose
    # bottles (for example ``CS/BT 1/2``).  Keep the loose quantity separate so
    # a partial case is never disguised as a fractional case count.
    loose_units = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        default=Decimal("0"),
    )
    units_per_case = models.PositiveIntegerField(null=True, blank=True)
    received_units = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    unit_cost_cents = models.BigIntegerField(null=True, blank=True)
    line_total_cents = models.BigIntegerField(null=True, blank=True)
    square_unit_cost_cents = models.BigIntegerField(null=True, blank=True)
    square_unit_cost_currency = models.CharField(max_length=3, blank=True)
    square_unit_cost_source = models.CharField(max_length=120, blank=True)
    square_catalog_variation_id = models.CharField(max_length=64, blank=True, db_index=True)
    square_item_name = models.CharField(max_length=300, blank=True)
    match_status = models.CharField(
        max_length=20,
        choices=LineMatchStatus.choices,
        default=LineMatchStatus.UNMATCHED,
    )
    included = models.BooleanField(default=True)
    review_note = models.CharField(max_length=300, blank=True)
    square_change_id = models.CharField(max_length=64, blank=True)
    square_count_before = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    square_count_variation_id = models.CharField(max_length=64, blank=True)
    projected_count_after = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    square_count_after = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    square_count_drift = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    square_count_snapshot_at = models.DateTimeField(null=True, blank=True)
    square_count_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["delivery", "position"], name="delivery_line_position"),
        ]

    def __str__(self) -> str:
        return f"{self.position}. {self.description}"

    def clean(self):
        super().clean()
        if self.upc:
            self.upc = "".join(ch for ch in self.upc if ch.isdigit())

    @property
    def is_ready(self) -> bool:
        if not self.included or self.match_status == LineMatchStatus.EXCLUDED:
            return True
        return bool(
            self.square_catalog_variation_id
            and self.received_units is not None
            and self.received_units != 0
            and self.match_status == LineMatchStatus.MATCHED
        )

    @property
    def proposed_delta(self):
        """The reviewed stock change; named explicitly for the comparison UI."""

        return self.received_units

    @property
    def unit_cost_change_cents(self) -> int | None:
        if self.unit_cost_cents is None or self.square_unit_cost_cents is None:
            return None
        return self.unit_cost_cents - self.square_unit_cost_cents

    @property
    def unit_cost_change_percent(self) -> Decimal | None:
        baseline = self.square_unit_cost_cents
        if self.unit_cost_cents is None or baseline is None or baseline <= 0:
            return None
        change = Decimal(self.unit_cost_cents - baseline) * Decimal(100) / Decimal(baseline)
        return change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @property
    def unit_cost_change_display(self) -> str:
        change = self.unit_cost_change_percent
        if change is None:
            return "No baseline"
        prefix = "+" if change > 0 else ""
        return f"{prefix}{change}%"


class DeliveryPricingPlan(models.Model):
    """Owner pricing rules and the immutable Square update prepared from them.

    Editable rules are deliberately separate from ``frozen_payload``.  Once a
    preview is approved, the Square writer can retry the exact same payload and
    idempotency key without silently applying later form edits.
    """

    delivery = models.OneToOneField(
        Delivery,
        on_delete=models.CASCADE,
        related_name="pricing_plan",
    )
    default_markup_percent = models.DecimalField(
        max_digits=9,
        decimal_places=3,
        null=True,
        blank=True,
    )
    category_rules = models.JSONField(default=dict, blank=True)
    product_overrides = models.JSONField(default=dict, blank=True)
    category_assignments = models.JSONField(default=dict, blank=True)
    preview_lines = models.JSONField(default=list, blank=True)
    issues = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=PricingPlanStatus.choices,
        default=PricingPlanStatus.DRAFT,
        db_index=True,
    )
    revision = models.PositiveIntegerField(default=1)
    frozen_payload = models.JSONField(default=dict, blank=True)
    frozen_payload_hash = models.CharField(max_length=64, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True)
    square_result = models.JSONField(default=dict, blank=True)
    square_error = models.TextField(blank=True)
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prepared_delivery_pricing_plans",
    )
    prepared_at = models.DateTimeField(null=True, blank=True)
    pushed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pushed_delivery_pricing_plans",
    )
    pushed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_pricing_plan_idempotency_key",
            ),
        ]

    def __str__(self) -> str:
        return f"Pricing for {self.delivery}"


def inventory_sandbox_workbook_path(instance: InventorySandboxJob, _filename: str) -> str:
    """Return the only protected-storage path accepted for a sandbox workbook."""

    return f"inventory-sandbox/{instance.id}/corrected-inventory.xlsx"


class InventorySandboxJob(models.Model):
    """Auditable bridge between an invoice delivery and a Managed Agents session.

    Original invoice files remain on :class:`capture.Document`.  The session is
    given only this job's opaque UUID; a trusted host-side launcher resolves that
    UUID and stages immutable copies into a fresh per-session workspace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(
        Delivery,
        on_delete=models.PROTECT,
        related_name="sandbox_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inventory_sandbox_jobs",
    )
    status = models.CharField(
        max_length=16,
        choices=InventorySandboxJobStatus.choices,
        default=InventorySandboxJobStatus.PENDING,
        db_index=True,
    )
    workflow_version = models.CharField(max_length=40, default="inventory_invoice_v3")
    session_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    input_manifest = models.JSONField(default=dict, blank=True)
    extracted_result = models.JSONField(default=dict, blank=True)
    output_workbook = models.FileField(
        upload_to=inventory_sandbox_workbook_path,
        max_length=500,
        blank=True,
    )
    output_sha256 = models.CharField(max_length=64, blank=True)
    output_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    error_message = models.TextField(blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    staged_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery"],
                condition=models.Q(
                    status__in=[
                        InventorySandboxJobStatus.PENDING,
                        InventorySandboxJobStatus.QUEUED,
                        InventorySandboxJobStatus.STAGED,
                        InventorySandboxJobStatus.RUNNING,
                    ]
                ),
                name="one_active_inventory_sandbox_job",
            ),
        ]

    def __str__(self) -> str:
        return f"Sandbox job {str(self.id)[:8]} for {self.delivery}"


class CatalogCreationIntent(models.Model):
    """Durable identity for one owner-approved Square item creation request."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line = models.OneToOneField(
        DeliveryLine,
        on_delete=models.PROTECT,
        related_name="catalog_creation_intent",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="square_catalog_creation_intents",
    )
    status = models.CharField(
        max_length=16,
        choices=CatalogCreationStatus.choices,
        default=CatalogCreationStatus.CLAIMED,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    payload_hash = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    normalized_sku = models.CharField(max_length=100, blank=True)
    normalized_upc = models.CharField(max_length=14, blank=True)
    square_item_id = models.CharField(max_length=64, blank=True)
    square_variation_id = models.CharField(max_length=64, blank=True)
    response = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["normalized_sku"],
                condition=~models.Q(normalized_sku=""),
                name="unique_square_create_sku",
            ),
            models.UniqueConstraint(
                fields=["normalized_upc"],
                condition=~models.Q(normalized_upc=""),
                name="unique_square_create_upc",
            ),
        ]

    def __str__(self) -> str:
        return f"Square item request {str(self.id)[:8]} ({self.status})"
