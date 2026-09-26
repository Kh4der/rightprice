from django.contrib import admin

from .models import AuditEvent, FieldCorrection


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAuditAdmin):
    list_display = ("created_at", "actor", "action", "target_type", "target_id")
    list_filter = ("action", "target_type")
    search_fields = ("target_id", "actor__display_name", "actor__login_code")


@admin.register(FieldCorrection)
class FieldCorrectionAdmin(ReadOnlyAuditAdmin):
    list_display = ("created_at", "document", "field_path", "corrected_by")
    search_fields = ("document__original_name", "field_path", "corrected_by__display_name")
