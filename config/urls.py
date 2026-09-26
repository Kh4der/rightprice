"""
Root URL configuration.

App URLs are mounted here as each app gains its routes. Two things are
deliberate:

* The owner's screens live under /owner/ and the employee's at the root, because
  the employee side is what gets used on a phone fifty times a week and should
  have the shortest URLs.
* Authorization is never enforced by URL prefix alone. Every view scopes its
  queryset to the requesting user; the prefix is organisation, not a boundary.
"""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def healthz(_request):
    """
    Liveness probe for Caddy and for deploy scripts.

    Deliberately does not touch the database or Square: this answers "is the
    process up", and a readiness check that depends on every downstream service
    turns one slow dependency into a restart loop.
    """
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.audit.urls")),
    path("", include("apps.capture.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.reconcile.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    # Registers the 'djdt' namespace. Without it, debug_toolbar is in
    # INSTALLED_APPS, injects its template tag, and every page 500s with
    # "'djdt' is not a registered namespace".
    from debug_toolbar.toolbar import debug_toolbar_urls

    urlpatterns += debug_toolbar_urls()
