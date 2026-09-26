"""Production-only checks for integration settings Django cannot infer."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, register


def _error(message: str, *, hint: str, error_id: str) -> Error:
    return Error(message, hint=hint, id=error_id)


@register(Tags.security, deploy=True)
def production_configuration_check(app_configs=None, **kwargs):
    """Fail ``check --deploy`` when a production integration is incomplete."""
    del app_configs, kwargs
    if not settings.SETTINGS_MODULE.endswith(".prod"):
        return []

    errors: list[Error] = []

    if not settings.SQUARE_ACCESS_TOKEN:
        errors.append(
            _error(
                "SQUARE_ACCESS_TOKEN is empty in production.",
                hint="Set a server-side Square token with only the scopes this app needs.",
                error_id="store_ops.E001",
            )
        )
    if not settings.SQUARE_LOCATION_ID:
        errors.append(
            _error(
                "SQUARE_LOCATION_ID is empty in production.",
                hint="Set the exact store location; never fall back to the first API result.",
                error_id="store_ops.E002",
            )
        )
    if settings.SQUARE_ENVIRONMENT not in {"sandbox", "production"}:
        errors.append(
            _error(
                "SQUARE_ENVIRONMENT is invalid.",
                hint="Use either 'sandbox' or 'production'.",
                error_id="store_ops.E003",
            )
        )

    if settings.STORAGE_DRIVER == "s3":
        missing = [
            name
            for name in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
            if not getattr(settings, name)
        ]
        if missing:
            errors.append(
                _error(
                    f"S3 photo storage is missing: {', '.join(missing)}.",
                    hint="Configure the private production bucket before accepting photos.",
                    error_id="store_ops.E004",
                )
            )
    elif settings.STORAGE_DRIVER == "vercel_blob":
        if not getattr(settings, "BLOB_READ_WRITE_TOKEN", ""):
            errors.append(
                _error(
                    "Private Vercel Blob storage is missing BLOB_READ_WRITE_TOKEN.",
                    hint="Connect a private Blob store to every deployed environment.",
                    error_id="store_ops.E004",
                )
            )
    elif settings.STORAGE_DRIVER != "local":
        errors.append(
            _error(
                f"STORAGE_DRIVER {settings.STORAGE_DRIVER!r} is not supported.",
                hint="Use 'local', 's3', or 'vercel_blob'.",
                error_id="store_ops.E004",
            )
        )

    provider = str(getattr(settings, "EXTRACTION_PROVIDER", "")).strip().lower()
    if provider != "openai":
        errors.append(
            _error(
                f"EXTRACTION_PROVIDER {provider!r} is not supported by this release.",
                hint="Set EXTRACTION_PROVIDER=openai until another provider adapter is implemented.",
                error_id="store_ops.E005",
            )
        )
    elif not getattr(settings, "OPENAI_API_KEY", ""):
        errors.append(
            _error(
                "OPENAI_API_KEY is empty while EXTRACTION_PROVIDER=openai.",
                hint="Configure the OpenAI key used by the extraction worker.",
                error_id="store_ops.E005",
            )
        )

    if not settings.ALLOWED_HOSTS or "*" in settings.ALLOWED_HOSTS:
        errors.append(
            _error(
                "Production ALLOWED_HOSTS must be a non-empty explicit allowlist.",
                hint="List only the hostnames served by Caddy.",
                error_id="store_ops.E006",
            )
        )

    insecure_origins = [
        origin
        for origin in getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        if not origin.startswith("https://")
    ]
    if insecure_origins:
        errors.append(
            _error(
                "Production CSRF trusted origins must use HTTPS.",
                hint="Remove HTTP origins: " + ", ".join(insecure_origins),
                error_id="store_ops.E007",
            )
        )

    return errors
