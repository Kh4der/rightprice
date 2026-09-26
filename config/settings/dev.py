"""Local development. Never used in production."""

from .base import *
from .base import INSTALLED_APPS, MIDDLEWARE, env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

# 0.0.0.0 so a phone on the shop wifi can reach the dev server by LAN IP.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

if DEBUG:
    INSTALLED_APPS += ["debug_toolbar"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INTERNAL_IPS = ["127.0.0.1"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
