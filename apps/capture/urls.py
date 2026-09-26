from django.urls import path

from . import views

app_name = "capture"

urlpatterns = [
    path("capture/stage-photo/", views.stage_document, name="stage-document"),
    path("submissions/", views.submission_list, name="list"),
    path("submissions/<uuid:pk>/", views.submission_detail, name="detail"),
    path("submissions/<uuid:pk>/retry/", views.retry_submission, name="retry"),
    path("documents/<uuid:pk>/file/", views.document_file, name="document-file"),
    path("documents/<uuid:pk>/review/", views.review_document, name="document-review"),
    path("capture/daily/", views.daily_create, name="daily"),
    path("capture/payout/", views.payout_create, name="payout"),
    path("capture/inventory/", views.inventory_create, name="inventory"),
]
