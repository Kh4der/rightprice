"""
Celery application.

The employee's phone must never wait on a vision API call, so document
extraction, Square day pulls and inventory pushes all run here. Workers are
synchronous, which is why the Square client used in tasks is the sync `Square`
rather than `AsyncSquare`.
"""

from celery import Celery

from config.bootstrap import configure_settings

configure_settings()

app = Celery("store_ops")

# All Celery settings live in Django settings under a CELERY_ prefix, so there is
# one place to look and one place for env overrides.
app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Smoke test that a worker is actually consuming from the queue."""
    print(f"celery request: {self.request!r}")
