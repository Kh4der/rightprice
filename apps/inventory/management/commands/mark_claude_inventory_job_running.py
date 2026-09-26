from django.core.management.base import BaseCommand, CommandError

from apps.inventory.claude_sandbox import (
    InventorySandboxError,
    mark_inventory_sandbox_job_running,
)
from apps.inventory.models import InventorySandboxJob


class Command(BaseCommand):
    help = "Record that an isolated Claude worker has claimed a staged inventory job."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job = InventorySandboxJob.objects.get(pk=options["job_id"])
            mark_inventory_sandbox_job_running(job)
        except (InventorySandboxJob.DoesNotExist, InventorySandboxError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Marked job {job.id} as running"))
