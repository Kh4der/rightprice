from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyOperationalAdmin

from .models import DailyCashCount, DailyReconciliation, PayoutRecord


@admin.register(DailyReconciliation)
class DailyReconciliationAdmin(ReadOnlyOperationalAdmin):
    list_display = ("submission", "status", "unexplained_variance_cents", "owner_collected_cents")
    list_filter = ("status",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(DailyCashCount)
class DailyCashCountAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "reconciliation",
        "expected_cents",
        "counted_cents",
        "variance_cents",
        "entered_by",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(PayoutRecord)
class PayoutRecordAdmin(ReadOnlyOperationalAdmin):
    list_display = ("submission", "amount_cents", "status", "reimbursed_at")
    list_filter = ("status",)
