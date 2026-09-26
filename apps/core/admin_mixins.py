"""Admin safeguards for records that must change only through audited workflows."""

from django.contrib import admin


class ReadOnlyOperationalAdmin(admin.ModelAdmin):
    """Expose operational records for support without an unaudited edit path."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyOperationalInline(admin.TabularInline):
    """View-only inline counterpart for evidence and delivery children."""

    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
