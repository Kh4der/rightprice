import pytest
from django.contrib import admin
from django.test import RequestFactory

from apps.accounts.models import User
from apps.capture.models import Document, Submission
from apps.inventory.models import CatalogMapping, Delivery, SquareCatalogVariation, Vendor
from apps.reconcile.models import DailyReconciliation, PayoutRecord


@pytest.fixture
def admin_request(db):
    user = User.objects.create_superuser(
        "AUDITOR",
        "Audit!Viewer#2026",
        display_name="Admin Auditor",
    )
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


@pytest.mark.parametrize(
    "model",
    [
        User,
        Submission,
        Document,
        Vendor,
        CatalogMapping,
        SquareCatalogVariation,
        Delivery,
        DailyReconciliation,
        PayoutRecord,
    ],
)
def test_operational_admins_are_view_only(admin_request, model):
    model_admin = admin.site._registry[model]

    assert model_admin.has_view_permission(admin_request)
    assert not model_admin.has_add_permission(admin_request)
    assert not model_admin.has_change_permission(admin_request)
    assert not model_admin.has_delete_permission(admin_request)
