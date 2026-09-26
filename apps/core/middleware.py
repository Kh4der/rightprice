"""Request-scoped application concerns."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


class StoreTimezoneMiddleware:
    """Render human-facing dates in the store's timezone while storing UTC."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.store_zone = ZoneInfo(settings.STORE_TIMEZONE)

    def __call__(self, request):
        with timezone.override(self.store_zone):
            return self.get_response(request)
