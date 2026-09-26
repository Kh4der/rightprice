import pytest
from django.test import override_settings

from apps.core.checks import production_configuration_check

VALID_PRODUCTION = {
    "SETTINGS_MODULE": "config.settings.prod",
    "SQUARE_ACCESS_TOKEN": "not-a-real-token",
    "SQUARE_LOCATION_ID": "LOCATION",
    "SQUARE_ENVIRONMENT": "sandbox",
    "STORAGE_DRIVER": "local",
    "BLOB_READ_WRITE_TOKEN": "",
    "EXTRACTION_PROVIDER": "openai",
    "ANTHROPIC_API_KEY": "",
    "GOOGLE_GENAI_API_KEY": "",
    "OPENAI_API_KEY": "not-a-real-key",
    "ALLOWED_HOSTS": ["store.example.com"],
    "CSRF_TRUSTED_ORIGINS": ["https://store.example.com"],
}


@override_settings(**VALID_PRODUCTION)
def test_complete_production_configuration_passes_custom_checks():
    assert production_configuration_check() == []


@pytest.mark.parametrize(
    ("setting_name", "error_id"),
    [
        ("SQUARE_ACCESS_TOKEN", "store_ops.E001"),
        ("SQUARE_LOCATION_ID", "store_ops.E002"),
        ("OPENAI_API_KEY", "store_ops.E005"),
    ],
)
def test_required_integrations_fail_the_deployment_check(setting_name, error_id):
    configured = {**VALID_PRODUCTION, setting_name: ""}
    with override_settings(**configured):
        errors = production_configuration_check()

    assert error_id in {error.id for error in errors}


@override_settings(
    **(
        VALID_PRODUCTION
        | {
            "STORAGE_DRIVER": "s3",
            "S3_BUCKET": "",
            "S3_ACCESS_KEY_ID": "",
            "S3_SECRET_ACCESS_KEY": "",
        }
    )
)
def test_incomplete_private_photo_storage_fails_the_deployment_check():
    errors = production_configuration_check()
    assert "store_ops.E004" in {error.id for error in errors}


@override_settings(
    **(
        VALID_PRODUCTION
        | {
            "STORAGE_DRIVER": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "",
        }
    )
)
def test_vercel_blob_storage_requires_its_server_side_token():
    errors = production_configuration_check()
    assert "store_ops.E004" in {error.id for error in errors}


@override_settings(
    **(
        VALID_PRODUCTION
        | {
            "STORAGE_DRIVER": "vercel_blob",
            "BLOB_READ_WRITE_TOKEN": "not-a-real-token",
        }
    )
)
def test_complete_private_vercel_blob_storage_passes():
    assert production_configuration_check() == []


@override_settings(**(VALID_PRODUCTION | {"STORAGE_DRIVER": "unknown"}))
def test_unknown_storage_driver_fails_the_deployment_check():
    errors = production_configuration_check()
    assert "store_ops.E004" in {error.id for error in errors}


@override_settings(**(VALID_PRODUCTION | {"ALLOWED_HOSTS": ["*"]}))
def test_wildcard_host_fails_the_deployment_check():
    errors = production_configuration_check()
    assert "store_ops.E006" in {error.id for error in errors}


@override_settings(**(VALID_PRODUCTION | {"CSRF_TRUSTED_ORIGINS": ["http://store.example.com"]}))
def test_http_csrf_origin_fails_the_deployment_check():
    errors = production_configuration_check()
    assert "store_ops.E007" in {error.id for error in errors}


@override_settings(SETTINGS_MODULE="config.settings.test")
def test_integration_checks_do_not_apply_to_nonproduction_settings():
    assert production_configuration_check() == []


@override_settings(**(VALID_PRODUCTION | {"EXTRACTION_PROVIDER": "anthropic"}))
def test_unimplemented_extraction_provider_fails_even_with_its_key():
    errors = production_configuration_check()

    assert "store_ops.E005" in {error.id for error in errors}
