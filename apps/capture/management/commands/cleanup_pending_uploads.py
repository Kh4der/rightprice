from django.core.management.base import BaseCommand, CommandError

from apps.capture.staging import cleanup_expired_uploads


class Command(BaseCommand):
    help = "Delete expired photos that were staged but never submitted."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be at least 1")
        deleted, failed = cleanup_expired_uploads(limit=limit)
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired staged upload(s)."))
        if failed:
            raise CommandError(f"Storage deletion failed for {failed} staged upload(s).")
