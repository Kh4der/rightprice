from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyOperationalAdmin, ReadOnlyOperationalInline

from .models import (
    CatalogCreationIntent,
    CatalogMapping,
    Delivery,
    DeliveryLine,
    InventorySandboxJob,
    SquareCatalogVariation,
    Vendor,
)


@admin.register(Vendor)
class VendorAdmin(ReadOnlyOperationalAdmin):
    list_display = ("name", "square_vendor_id", "active")
    search_fields = ("name", "square_vendor_id")


@admin.register(CatalogMapping)
class CatalogMappingAdmin(ReadOnlyOperationalAdmin):
    list_display = ("description", "vendor", "vendor_sku", "upc", "units_per_case")
    search_fields = ("description", "vendor_sku", "upc", "square_catalog_variation_id")


class DeliveryLineInline(ReadOnlyOperationalInline):
    model = DeliveryLine
    fields = (
        "position",
        "description",
        "cases",
        "units_per_case",
        "received_units",
        "match_status",
        "included",
    )


@admin.register(Delivery)
class DeliveryAdmin(ReadOnlyOperationalAdmin):
    list_display = ("invoice_number", "vendor_name_raw", "status", "created_at", "pushed_at")
    list_filter = ("status",)
    inlines = (DeliveryLineInline,)


@admin.register(SquareCatalogVariation)
class SquareCatalogVariationAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "item_name",
        "variation_name",
        "sku",
        "upc",
        "track_inventory",
        "present_at_location",
        "synced_at",
    )
    search_fields = ("item_name", "variation_name", "sku", "upc", "gtin", "variation_id")
    list_filter = ("track_inventory", "present_at_location")


@admin.register(InventorySandboxJob)
class InventorySandboxJobAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "id",
        "delivery",
        "status",
        "session_id",
        "requested_by",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "workflow_version")
    search_fields = ("id", "session_id", "delivery__invoice_number")


@admin.register(CatalogCreationIntent)
class CatalogCreationIntentAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "id",
        "line",
        "status",
        "normalized_sku",
        "normalized_upc",
        "created_by",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "id",
        "line__description",
        "normalized_sku",
        "normalized_upc",
        "square_item_id",
        "square_variation_id",
    )
