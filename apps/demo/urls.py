from django.urls import path

from . import views

app_name = "demo"

urlpatterns = [
    path("demo/start/", views.start_demo, name="start"),
    path("demo/", views.dashboard, name="dashboard"),
    path("demo/reset/", views.reset_demo, name="reset"),
    path("demo/daily-cash/", views.daily_cash, name="daily-cash"),
    path("demo/daily-cash/<slug:day_id>/", views.record_daily_cash, name="daily-cash-count"),
    path("demo/payouts/", views.payouts, name="payouts"),
    path("demo/payouts/<slug:payout_id>/reimburse/", views.reimburse_payout, name="payout-reimburse"),
    path("demo/inventory/", views.inventory, name="inventory"),
    path("demo/inventory/<slug:line_id>/match/", views.match_inventory_line, name="inventory-match"),
]
