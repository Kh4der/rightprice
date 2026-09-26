from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [path("owner/audit/", views.audit_log, name="log")]
