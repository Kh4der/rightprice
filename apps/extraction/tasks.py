"""Celery entry points for vision extraction."""

from __future__ import annotations

from celery import shared_task

from .processing import process_document
from .processing import process_submission as process_submission_now


@shared_task(name="extraction.process_document")
def process_document_task(document_id: str, *, force: bool = False) -> str:
    """Process one document and return its final status to Celery."""

    document = process_document(document_id, force=force)
    return document.status


# A concise import name for upload code, without registering a second task.
extract_document = process_document_task


@shared_task(name="extraction.process_submission")
def process_submission(submission_id: str, *, force: bool = False) -> str:
    """Process all photos in a submission and materialize its workflow data."""

    submission = process_submission_now(submission_id, force=force)
    return submission.status


process_submission_task = process_submission
