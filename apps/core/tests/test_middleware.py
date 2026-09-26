from __future__ import annotations

from zoneinfo import ZoneInfo

from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.core.middleware import StoreTimezoneMiddleware


@override_settings(STORE_TIMEZONE="America/New_York")
def test_store_timezone_is_active_only_while_serving_request():
    original = timezone.get_current_timezone()
    observed = []

    def view(request):
        observed.append(timezone.get_current_timezone())
        return object()

    middleware = StoreTimezoneMiddleware(view)
    middleware(RequestFactory().get("/"))

    assert observed == [ZoneInfo("America/New_York")]
    assert timezone.get_current_timezone() == original
