"""Early settings selection shared by WSGI, ASGI, and Celery entrypoints."""

from __future__ import annotations

import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent


def configure_settings(
    *, default: str = "config.settings.prod", env_file: Path | None = None
) -> str:
    """
    Load ``DJANGO_SETTINGS_MODULE`` from .env before applying a safe default.

    Entry points execute before Django imports ``config.settings.base``. Reading
    .env only inside base settings is therefore too late to choose the settings
    module: an earlier ``setdefault(...dev)`` has already won. Production-facing
    entry points use this helper; ``manage.py`` intentionally keeps its explicit
    development default.
    """
    environ.Env.read_env(env_file or BASE_DIR / ".env")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", default)
    return os.environ["DJANGO_SETTINGS_MODULE"]
