"""
Settings shared by every environment.

Nothing here reads a secret except through `env`, and the secrets that must
exist are required rather than defaulted, so a misconfigured deploy fails at
boot instead of at 11pm while an employee is trying to close out the drawer.

MONEY CONVENTION, app-wide: every monetary amount is an integer number of
CENTS. Square's API uses the same convention, so nothing is converted at the
boundary. There are no floats in the money path at any layer.
"""

from datetime import time
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# Vercel injects this flag into both the Django function and Celery workers.
# Keeping the platform switch here lets the same codebase retain its efficient
# long-lived VPS defaults while using serverless-safe connections in production.
IS_VERCEL = env.bool("VERCEL", default=False)

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "django_htmx",
    "simple_history",
    "axes",
    # local
    "apps.core",
    "apps.accounts",
    "apps.capture",
    "apps.extraction",
    "apps.squareapi",
    "apps.inventory",
    "apps.reconcile",
    "apps.audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.StoreTimezoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    # Last: needs request.user resolved so a failed login can be attributed.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}

# A process-local pool is useful on a long-lived VPS but counterproductive in a
# short-lived serverless function. Vercel uses the provider's pooled Postgres
# URL and closes the Django connection at the end of each request instead.
if IS_VERCEL:
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = False
else:
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["pool"] = {"min_size": 2, "max_size": 8}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # Must be first. AxesStandaloneBackend only runs the lockout check and
    # raises PermissionDenied, so it gates every backend listed after it.
    # (The name is AxesStandaloneBackend, not AxesStandardBackend.)
    "axes.backends.AxesStandaloneBackend",
    # USERNAME_FIELD is login_code, so ModelBackend's `username=` argument
    # authenticates against the short code the employee types.
    "django.contrib.auth.backends.ModelBackend",
]

# Argon2 first. Employee credentials are short numeric PINs with a tiny
# keyspace, so the hash is deliberately expensive — though the rate limiting
# below is what actually bounds the damage.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Owner accounts are created through Django's superuser flow and use a real
# password. Employee PINs are created only through the owner workflow and are
# protected by the stricter attempt lockout below.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# This runs on a shared phone behind the counter, so a session left open is a
# real exposure. Short idle window, and the cookie dies with the browser.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = env.int("SESSION_IDLE_MINUTES", default=30) * 60
SESSION_SAVE_EVERY_REQUEST = True  # makes SESSION_COOKIE_AGE an *idle* timeout
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the token out of the cookie

# --- django-axes: brute-force protection on a 4-6 digit PIN ---------------
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.25  # hours, so 15 minutes
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True

# Lock on the login code alone, deliberately NOT on ip_address.
#
# Every employee shares one phone behind the counter, so they all arrive from a
# single IP. An IP-based lockout means one tired employee mistyping their PIN
# five times locks the entire store out of the app mid-close — an availability
# failure worse than the attack it prevents.
#
# Locking on username is also the control that actually matters here: the threat
# is PIN brute force against a known login code, and that cannot be evaded by
# rotating IPs, user agents or cookies, which is what axes.W006 warns about.
# Enumeration protection belongs at the edge (Caddy rate limiting), not in a
# lockout that can take the store offline.
AXES_LOCKOUT_PARAMETERS = ["username"]
SILENCED_SYSTEM_CHECKS = ["axes.W006"]

# axes must know which account a failed login targeted, or it records nothing and
# the lockout above silently does not exist.
#
# A single AXES_USERNAME_FORM_FIELD cannot cover this app: the employee PIN form
# posts "login_code" (our USERNAME_FIELD) while Django's admin form posts
# "username" no matter what USERNAME_FIELD says. Whichever one the setting does
# not match loses brute-force protection entirely — and with AXES_ENABLE_ADMIN on,
# that would leave the admin login unprotected on a public domain.
#
# The callable takes precedence over AXES_USERNAME_FORM_FIELD and accepts either
# field from either the credentials dict or the POST body. It also applies the
# same upper-casing the User model does, so "jd" and "JD" share one counter
# rather than getting five guesses each.
#
# Regression tests: apps/accounts/tests/test_lockout.py
AXES_USERNAME_CALLABLE = "apps.accounts.axes_hooks.get_username"
AXES_USERNAME_FORM_FIELD = "login_code"

# --------------------------------------------------------------------------
# Internationalization / time
# --------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True

# Everything is stored in UTC. STORE_TIMEZONE below is the *business* timezone
# used to derive business-day windows; deliberately not TIME_ZONE, because the
# store could later be reported on from somewhere else.
TIME_ZONE = "UTC"

# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --------------------------------------------------------------------------
# Photo storage
# --------------------------------------------------------------------------
# STORAGE_DRIVER=s3 covers both Cloudflare R2 in production and the S3Mock
# container in development. Vercel deployments use a private Blob store so
# evidence survives the function's ephemeral filesystem.
STORAGE_DRIVER = env("STORAGE_DRIVER", default="local")

BLOB_READ_WRITE_TOKEN = env("BLOB_READ_WRITE_TOKEN", default="")

S3_BUCKET = env("S3_BUCKET", default="")
S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default="")
S3_REGION = env("S3_REGION", default="auto")
S3_ACCESS_KEY_ID = env("S3_ACCESS_KEY_ID", default="")
S3_SECRET_ACCESS_KEY = env("S3_SECRET_ACCESS_KEY", default="")

# A presigned photo URL is a bearer credential for a picture of the store's cash
# position, so it expires quickly.
SIGNED_URL_TTL_SECONDS = env.int("SIGNED_URL_TTL_SECONDS", default=300)

if STORAGE_DRIVER == "vercel_blob":
    _default_storage = {
        "BACKEND": "apps.core.storage.VercelBlobStorage",
        "OPTIONS": {"token": BLOB_READ_WRITE_TOKEN or None},
    }
elif STORAGE_DRIVER == "s3":
    _default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": S3_BUCKET,
            "endpoint_url": S3_ENDPOINT_URL or None,
            "region_name": S3_REGION,
            "access_key": S3_ACCESS_KEY_ID,
            "secret_key": S3_SECRET_ACCESS_KEY,
            # Photos are never world-readable; they are reached only through
            # short-lived presigned URLs.
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": SIGNED_URL_TTL_SECONDS,
            "file_overwrite": False,
            "signature_version": "s3v4",
            "addressing_style": "path",
        },
    }
else:
    _default_storage = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {
            "location": BASE_DIR / "storage",
            # No MEDIA_URL on purpose: local photos are served by an
            # authenticated view, never straight off the filesystem.
        },
    }

STORAGES = {
    "default": _default_storage,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --------------------------------------------------------------------------
# Background jobs (Celery)
# --------------------------------------------------------------------------
if IS_VERCEL:
    # Vercel installs this transport for declared Celery subscribers and routes
    # queued work through Vercel Queues. This keeps vision/Square work outside
    # the employee's request without requiring a separate Redis service.
    CELERY_BROKER_URL = "vercel://"
    CELERY_RESULT_BACKEND = None
else:
    CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_TRACK_STARTED = True
# A vision call on a large photo can take a while; time it out rather than let
# a worker hang forever holding a slot.
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300

# --------------------------------------------------------------------------
# Square
# --------------------------------------------------------------------------
SQUARE_ENVIRONMENT = env("SQUARE_ENVIRONMENT", default="sandbox")
SQUARE_ACCESS_TOKEN = env("SQUARE_ACCESS_TOKEN", default="")
SQUARE_APPLICATION_ID = env("SQUARE_APPLICATION_ID", default="")
SQUARE_LOCATION_ID = env("SQUARE_LOCATION_ID", default="")
SQUARE_WEBHOOK_SIGNATURE_KEY = env("SQUARE_WEBHOOK_SIGNATURE_KEY", default="")
SQUARE_INVENTORY_WRITES_ENABLED = env.bool("SQUARE_INVENTORY_WRITES_ENABLED", default=False)
# Square restricts inventory cost/vendor writes by subscription. Keep receipt
# cost posting behind its own gate even when quantity updates are enabled.
SQUARE_INVENTORY_COST_WRITES_ENABLED = env.bool(
    "SQUARE_INVENTORY_COST_WRITES_ENABLED", default=False
)
# Creating a catalog item is a separate, rarer mutation and requires its own
# explicit sandbox-tested owner gate.
SQUARE_CATALOG_WRITES_ENABLED = env.bool("SQUARE_CATALOG_WRITES_ENABLED", default=False)
SQUARE_CATALOG_CREATE_STALE_SECONDS = env.int(
    "SQUARE_CATALOG_CREATE_STALE_SECONDS", default=900
)
# A PUSHING delivery keeps its committed request keys. Only after this quiet
# period can an owner resume those exact keys following a process crash.
SQUARE_PUSH_STALE_SECONDS = env.int("SQUARE_PUSH_STALE_SECONDS", default=900)

# --------------------------------------------------------------------------
# Vision extraction
# --------------------------------------------------------------------------
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
EXTRACTION_PROVIDER = env("EXTRACTION_PROVIDER", default="openai").lower()

# Keep model names provider-specific. The old .env used EXTRACTION_MODEL for a
# Claude name; silently sending that name to OpenAI is an avoidable outage.
if EXTRACTION_PROVIDER == "openai":
    EXTRACTION_MODEL = env("OPENAI_EXTRACTION_MODEL", default="gpt-6-astra")
    EXTRACTION_FALLBACK_MODEL = env("OPENAI_EXTRACTION_FALLBACK_MODEL", default="gpt-5.6-terra")
else:
    EXTRACTION_MODEL = env(
        "ANTHROPIC_EXTRACTION_MODEL",
        default=env("EXTRACTION_MODEL", default="claude-sonnet-5"),
    )
    EXTRACTION_FALLBACK_MODEL = env(
        "ANTHROPIC_EXTRACTION_FALLBACK_MODEL",
        default=env("EXTRACTION_FALLBACK_MODEL", default="claude-opus-5-5"),
    )

# Uploads are evidence but still untrusted input. These limits are enforced by
# the form and checked again before image decoding.
MAX_UPLOAD_BYTES = env.int("MAX_UPLOAD_BYTES", default=15 * 1024 * 1024)
MAX_IMAGE_PIXELS = env.int("MAX_IMAGE_PIXELS", default=40_000_000)
# Blind N-of-the-same-model runs are the wrong shape of redundancy here, and 2
# is the worst possible value: two readings that disagree tell you there is a
# problem but not which one is right, so every disagreement becomes a human
# review regardless.
#
# Most fields do not need a second reading at all, because an arithmetic identity
# in apps/reconcile/checks.py already pins them down — a misread digit cannot
# also make the column add up. Spending a second call on those proves nothing.
#
# What genuinely needs a second opinion is the handful of money fields with NO
# arithmetic cross-check anywhere, listed below. Those get one re-read on the
# FALLBACK model, so the second reading is actually independent rather than the
# same model repeating itself.
EXTRACTION_RUNS = env.int("EXTRACTION_RUNS", default=1)

# Fields no identity can verify, so a disagreement between two different models
# is the only signal available.
SECOND_OPINION_FIELDS = [
    "counted_cash",  # drawer, once ended — nothing else reports it
    "pays",  # lottery winners paid in cash — the payout ledger depends on it
    "net_total",  # lottery settlement — not derivable from the other printed rows
    "report_date",  # a right figure filed under the wrong day is still wrong
]
GOOGLE_GENAI_API_KEY = env("GOOGLE_GENAI_API_KEY", default="")

# --------------------------------------------------------------------------
# Claude Managed Agents: self-hosted inventory sandbox
# --------------------------------------------------------------------------
# This is a separate, opt-in path from ordinary vision extraction.  The web app
# uses ANTHROPIC_API_KEY only to create a session.  The isolated worker must be
# launched with an ANTHROPIC_ENVIRONMENT_KEY instead and must never inherit this
# Django process's database, storage, or Square credentials.
CLAUDE_INVENTORY_SANDBOX_ENABLED = env.bool(
    "CLAUDE_INVENTORY_SANDBOX_ENABLED", default=False
)
# When enabled, inventory uploads use the self-hosted Managed Agents path
# instead of also spending on the ordinary extraction provider.
CLAUDE_INVENTORY_SANDBOX_PRIMARY = env.bool(
    "CLAUDE_INVENTORY_SANDBOX_PRIMARY", default=False
)
CLAUDE_INVENTORY_AGENT_ID = env("CLAUDE_INVENTORY_AGENT_ID", default="")
CLAUDE_INVENTORY_ENVIRONMENT_ID = env("CLAUDE_INVENTORY_ENVIRONMENT_ID", default="")
# Managed Agents budgets use minor currency units represented as a decimal
# string.  The default is a hard $2.50 list-cost ceiling per invoice session.
CLAUDE_INVENTORY_MAX_COST_CENTS = env.int(
    "CLAUDE_INVENTORY_MAX_COST_CENTS", default=250
)
CLAUDE_INVENTORY_MAX_RESULT_JSON_BYTES = env.int(
    "CLAUDE_INVENTORY_MAX_RESULT_JSON_BYTES", default=2 * 1024 * 1024
)
CLAUDE_INVENTORY_MAX_WORKBOOK_BYTES = env.int(
    "CLAUDE_INVENTORY_MAX_WORKBOOK_BYTES", default=25 * 1024 * 1024
)

# The trusted launcher is deliberately a second gate.  It runs only on a Linux
# host with access to Django's protected storage and a local Docker daemon.  The
# Anthropic environment key is read directly by the management command at run
# time and is intentionally not a Django setting.
CLAUDE_INVENTORY_LAUNCHER_ENABLED = env.bool(
    "CLAUDE_INVENTORY_LAUNCHER_ENABLED", default=False
)
CLAUDE_INVENTORY_WORKSPACE_ROOT = env(
    "CLAUDE_INVENTORY_WORKSPACE_ROOT",
    default="/var/lib/store-ops/claude-inventory",
)
CLAUDE_INVENTORY_DOCKER_IMAGE = env("CLAUDE_INVENTORY_DOCKER_IMAGE", default="")
CLAUDE_INVENTORY_DOCKER_NETWORK = env("CLAUDE_INVENTORY_DOCKER_NETWORK", default="")
CLAUDE_INVENTORY_DOCKER_USER = env("CLAUDE_INVENTORY_DOCKER_USER", default="")
CLAUDE_INVENTORY_DOCKER_PYTHON = env(
    "CLAUDE_INVENTORY_DOCKER_PYTHON", default="/app/.venv/bin/python"
)
CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST = env.bool(
    "CLAUDE_INVENTORY_DOCKER_REQUIRE_DIGEST", default=True
)
CLAUDE_INVENTORY_DOCKER_MEMORY = env(
    "CLAUDE_INVENTORY_DOCKER_MEMORY", default="1g"
)
CLAUDE_INVENTORY_DOCKER_CPUS = env(
    "CLAUDE_INVENTORY_DOCKER_CPUS", default="1.0"
)
CLAUDE_INVENTORY_DOCKER_PIDS_LIMIT = env.int(
    "CLAUDE_INVENTORY_DOCKER_PIDS_LIMIT", default=128
)
CLAUDE_INVENTORY_RUN_TIMEOUT_SECONDS = env.int(
    "CLAUDE_INVENTORY_RUN_TIMEOUT_SECONDS", default=900
)
CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS = env.int(
    "CLAUDE_INVENTORY_STOP_TIMEOUT_SECONDS", default=45
)
CLAUDE_INVENTORY_RETAIN_FAILED_WORKSPACES = env.bool(
    "CLAUDE_INVENTORY_RETAIN_FAILED_WORKSPACES", default=False
)

# --------------------------------------------------------------------------
# Store operating rules
# --------------------------------------------------------------------------
# A store closing after midnight has no calendar business day. This window
# drives every Square query and every report, so a wrong value silently files
# late-night sales under the wrong date.
STORE_TIMEZONE = env("STORE_TIMEZONE", default="America/New_York")

_cutoff_hour, _cutoff_minute = (
    int(p) for p in env("BUSINESS_DAY_CUTOFF", default="00:00").split(":")
)
BUSINESS_DAY_CUTOFF = time(hour=_cutoff_hour, minute=_cutoff_minute)

# Whether lottery sales ring through Square or only through the lottery
# terminal. Backwards, this double-counts every lottery dollar, so the
# reconciliation engine reads it explicitly instead of inferring it.
LOTTERY_RINGS_THROUGH_POS = env.bool("LOTTERY_RINGS_THROUGH_POS", default=False)

CASH_VARIANCE_TOLERANCE_CENTS = env.int("CASH_VARIANCE_TOLERANCE_CENTS", default=0)

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        "apps": {"level": "INFO", "handlers": ["console"], "propagate": False},
    },
}
