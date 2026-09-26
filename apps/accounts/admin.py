from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.core.admin_mixins import ReadOnlyOperationalAdmin

from .models import User


@admin.register(User)
class UserAdmin(ReadOnlyOperationalAdmin, DjangoUserAdmin):
    """Accounts are managed by the audited owner workflow, not Django admin."""

    ordering = ("display_name",)
    list_display = ("login_code", "display_name", "role", "square_team_member_id", "is_active")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("login_code", "display_name", "square_team_member_id")
    fieldsets = (
        (None, {"fields": ("login_code", "password")}),
        ("Identity", {"fields": ("display_name", "role", "square_team_member_id")}),
        (
            "Access",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined", "pin_changed_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("login_code", "display_name", "role", "password1", "password2"),
            },
        ),
    )
    readonly_fields = ("last_login", "date_joined", "pin_changed_at")
