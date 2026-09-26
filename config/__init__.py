"""
Make the Celery app importable as soon as Django starts, so the shared_task
decorator in each app's tasks.py binds to it.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
