from django.contrib import admin

from apps.core.admin_mixins import ReadOnlyOperationalAdmin, ReadOnlyOperationalInline

from .models import Document, PendingUpload, Submission


class DocumentInline(ReadOnlyOperationalInline):
    model = Document
    fields = ("original_name", "requested_type", "detected_type", "status", "sha256")
    readonly_fields = fields


@admin.register(Submission)
class SubmissionAdmin(ReadOnlyOperationalAdmin):
    list_display = ("business_day", "kind", "submitted_by", "status", "created_at")
    list_filter = ("kind", "status", "business_day")
    search_fields = ("submitted_by__display_name", "submitted_by__login_code")
    readonly_fields = ("id", "created_at", "updated_at", "submitted_at", "approved_at")
    inlines = (DocumentInline,)


@admin.register(Document)
class DocumentAdmin(ReadOnlyOperationalAdmin):
    list_display = ("original_name", "submission", "detected_type", "status", "created_at")
    list_filter = ("detected_type", "status")
    readonly_fields = (
        "id",
        "submission",
        "file",
        "original_name",
        "media_type",
        "size_bytes",
        "sha256",
        "created_at",
    )


@admin.register(PendingUpload)
class PendingUploadAdmin(ReadOnlyOperationalAdmin):
    list_display = (
        "original_name",
        "uploaded_by",
        "submission_kind",
        "created_at",
        "expires_at",
        "consumed_at",
    )
    list_filter = ("submission_kind", "consumed_at", "expires_at")
    readonly_fields = (
        "id",
        "uploaded_by",
        "submission_kind",
        "field_name",
        "file",
        "original_name",
        "media_type",
        "size_bytes",
        "sha256",
        "created_at",
        "expires_at",
        "consumed_at",
        "consumed_document",
    )
