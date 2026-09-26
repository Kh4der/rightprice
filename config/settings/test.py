"""
Test settings.

The reconciliation engine is pure functions and needs no database, but the model
and view tests do. Nothing here may reach the network: a test that silently
calls Square or Anthropic is a test that fails in CI and bills real money.
"""

from .base import *
from .base import DATABASES

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]

# Fast hashing: the suite creates a lot of users and Argon2 is deliberately slow.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Run tasks inline so a test can assert on their effects without a live worker.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Keep photos out of the real bucket and off the developer's disk.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Obvious placeholders. Any test that needs a credential must inject a fake
# client rather than rely on one of these being real.
SQUARE_ACCESS_TOKEN = "test-square-token"
SQUARE_LOCATION_ID = "TEST_LOCATION"
ANTHROPIC_API_KEY = "test-anthropic-key"

# A connection pool is pointless per-test and slows the suite down.
DATABASES["default"].get("OPTIONS", {}).pop("pool", None)
