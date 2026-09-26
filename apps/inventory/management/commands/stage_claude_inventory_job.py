from django.core.management.base import BaseCommand, CommandError

from apps.inventory.claude_sandbox import (
    InventorySandboxError,
    stage_inventory_sandbox_workspace,
)
from apps.inventory.models import InventorySandboxJob


class Command(BaseCommand):
    help = "Stage protected invoice evidence into a fresh Claude sandbox workspace."

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument("workspace")

    def handle(self, *args, **options):
        try:
            job = InventorySandboxJob.objects.select_related("delivery__submission").get(
                pk=options["job_id"]
            )
            workspace = stage_inventory_sandbox_workspace(job, options["workspace"])
        except (InventorySandboxJob.DoesNotExist, InventorySandboxError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Staged job {job.id} at {workspace}"))
