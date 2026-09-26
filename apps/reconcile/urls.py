from django.urls import path

from . import views

app_name = "reconcile"

urlpatterns = [
    path("owner/", views.owner_dashboard, name="owner-dashboard"),
    path("owner/daily-cash/", views.daily_cash, name="daily-cash"),
    path("owner/submissions/<uuid:pk>/decision/", views.submission_decision, name="decision"),
    path("owner/payouts/<uuid:pk>/reimburse/", views.reimburse_payout, name="reimburse-payout"),
    path(
        "owner/payouts/<uuid:pk>/amount/",
        views.confirm_payout_amount,
        name="payout-amount",
    ),
    path(
        "owner/submissions/<uuid:pk>/daily-cash/",
        views.record_daily_cash_count,
        name="daily-cash-count",
    ),
    path(
        "owner/submissions/<uuid:pk>/cash-collection/",
        views.record_daily_cash_count,
        name="cash-collection",
    ),
    path(
        "owner/submissions/<uuid:pk>/sync-square/",
        views.sync_square_day,
        name="sync-square-day",
    ),
]
