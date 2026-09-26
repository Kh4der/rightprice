from __future__ import annotations

import asyncio

from django.core.management.base import BaseCommand, CommandError

from apps.inventory.claude_launcher import (
    InventoryLauncherError,
    run_inventory_launcher,
)


class Command(BaseCommand):
    help = (
        "Claim Claude self-hosted inventory work and run one fresh, isolated "
        "Docker sandbox per session."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Keep polling after each job. Without this option, process at most one job.",
        )

    def handle(self, *args, **options):
        try:
            result = asyncio.run(run_inventory_launcher(loop=options["loop"]))
        except KeyboardInterrupt as exc:
            raise CommandError("The Claude inventory launcher was stopped.") from exc
        except InventoryLauncherError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            # SDK and Docker failures can include remote response details. Keep
            # the terminal message deliberately generic; operational logs record
            # only exception types inside the launcher.
            raise CommandError(
                f"The Claude inventory launcher failed ({type(exc).__name__})."
            ) from exc

        if options["loop"]:
            # A loop normally exits only after a graceful signal/cancellation.
            self.stdout.write(
                self.style.SUCCESS(
                    f"Launcher stopped after {result.processed} completed and "
                    f"{result.failed} failed jobs."
                )
            )
        elif result.processed:
            self.stdout.write(self.style.SUCCESS("Completed one Claude inventory job."))
        else:
            self.stdout.write("No Claude inventory job was waiting.")
