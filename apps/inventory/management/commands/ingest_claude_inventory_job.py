from django.core.management.base import BaseCommand, CommandError

from apps.inventory.claude_sandbox import (
    InventorySandboxError,
    ingest_inventory_sandbox_outputs,
)
from apps.inventory.models import InventorySandboxJob


class Command(BaseCommand):
    help = "Validate and ingest fixed-path output from a Claude inventory sandbox."

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument("workspace")

    def handle(self, *args, **options):
        try:
            job = InventorySandboxJob.objects.get(pk=options["job_id"])
            ingest_inventory_sandbox_outputs(job, options["workspace"])
        except (InventorySandboxJob.DoesNotExist, InventorySandboxError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Ingested output for job {job.id}"))
