from django.contrib import admin

from .models import DrawerFloat


@admin.register(DrawerFloat)
class DrawerFloatAdmin(admin.ModelAdmin):
    list_display = ("effective_from", "amount_cents", "set_by", "created_at")
    readonly_fields = ("created_at",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if obj.set_by_id is None:
            obj.set_by = request.user
        super().save_model(request, obj, form, change)
