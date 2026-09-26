from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("deliveries/<uuid:pk>/", views.delivery_detail, name="delivery-detail"),
    path("deliveries/<uuid:pk>/header/", views.header_update, name="header-update"),
    path("deliveries/<uuid:pk>/export/", views.export_delivery, name="export"),
    path("deliveries/<uuid:pk>/sync-catalog/", views.sync_catalog, name="sync-catalog"),
    path("deliveries/<uuid:pk>/refresh-counts/", views.refresh_counts, name="refresh-counts"),
    path("deliveries/<uuid:pk>/push/", views.push_delivery, name="push"),
    path("deliveries/<uuid:pk>/lines/add/", views.line_create, name="line-create"),
    path(
        "deliveries/<uuid:pk>/lines/<uuid:line_pk>/",
        views.line_update,
        name="line-update",
    ),
    path(
        "deliveries/<uuid:pk>/lines/<uuid:line_pk>/match/",
        views.match_line,
        name="match-line",
    ),
    path(
        "deliveries/<uuid:pk>/lines/<uuid:line_pk>/choose-match/",
        views.choose_match,
        name="choose-match",
    ),
    path(
        "deliveries/<uuid:pk>/lines/<uuid:line_pk>/create-square-item/",
        views.create_catalog_item,
        name="create-catalog-item",
    ),
]
