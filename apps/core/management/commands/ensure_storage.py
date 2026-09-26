"""
Verify (and in development, create) the photo bucket.

This exists for two reasons:

1. The dev S3Mock container does not reliably honour its own bucket-seeding
   options, so a fresh clone otherwise starts with no bucket and the first photo
   upload fails with a confusing NoSuchBucket deep inside django-storages.
2. On a real deploy it is a credentials smoke test. Discovering that the R2
   token is wrong here, at deploy time, beats discovering it when an employee
   tries to submit the day's drawer count.

    uv run python manage.py ensure_storage
    uv run python manage.py ensure_storage --create   # dev convenience

It never creates a bucket unless asked, so it is safe to run on every deploy.
"""

import contextlib
import io

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

PROBE_KEY = ".store-ops-healthcheck"


class Command(BaseCommand):
    help = "Check the photo storage bucket is reachable and writable; optionally create it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--create",
            action="store_true",
            help="Create the bucket if it does not exist. Intended for local development.",
        )
        parser.add_argument(
            "--skip-write",
            action="store_true",
            help="Only check the bucket exists; do not write a probe object.",
        )

    def handle(self, *args, **options):
        if settings.STORAGE_DRIVER != "s3":
            self.stdout.write(
                self.style.WARNING(
                    f"STORAGE_DRIVER is {settings.STORAGE_DRIVER!r}, not 's3'. "
                    "Photos go to the local filesystem; nothing to check."
                )
            )
            return

        bucket = settings.S3_BUCKET
        if not bucket:
            raise CommandError("S3_BUCKET is not set.")

        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )

        endpoint = settings.S3_ENDPOINT_URL or "(AWS default)"
        self.stdout.write(f"endpoint : {endpoint}")
        self.stdout.write(f"region   : {settings.S3_REGION}")
        self.stdout.write(f"bucket   : {bucket}")

        # --- reachable? ---
        try:
            client.head_bucket(Bucket=bucket)
            exists = True
        except EndpointConnectionError as exc:
            raise CommandError(
                f"Cannot reach the storage endpoint at {endpoint}.\n"
                "In development, is the stack up?  docker compose up -d\n"
                f"({exc})"
            ) from exc
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchBucket"}:
                exists = False
            elif code in {"403", "AccessDenied"}:
                raise CommandError(
                    f"Bucket {bucket!r} exists but these credentials cannot access it. "
                    "Check S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY and the token's bucket scope."
                ) from exc
            else:
                raise CommandError(f"Unexpected error reaching bucket {bucket!r}: {exc}") from exc

        if not exists:
            if not options["create"]:
                raise CommandError(
                    f"Bucket {bucket!r} does not exist.\n"
                    "Re-run with --create for local development, or create it in the "
                    "Cloudflare R2 dashboard for production."
                )
            client.create_bucket(Bucket=bucket)
            self.stdout.write(self.style.SUCCESS(f"created bucket {bucket!r}"))
        else:
            self.stdout.write(self.style.SUCCESS("bucket exists"))

        if options["skip_write"]:
            return

        # --- writable, readable, and presignable? ---
        # A photo is useless if it can be stored but not served back, so check
        # the presigned GET too rather than assuming it follows from the PUT.
        payload = b"store-ops storage healthcheck"
        try:
            client.put_object(Bucket=bucket, Key=PROBE_KEY, Body=io.BytesIO(payload))
            got = client.get_object(Bucket=bucket, Key=PROBE_KEY)["Body"].read()
            if got != payload:
                raise CommandError("Probe object read back with different contents.")

            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": PROBE_KEY},
                ExpiresIn=60,
            )
            if "Signature" not in url:
                raise CommandError(
                    "Presigned URL came back unsigned. Photos would be served without "
                    "access control; refusing to call this healthy."
                )
        except ClientError as exc:
            raise CommandError(
                f"Bucket {bucket!r} is not writable with these credentials: {exc}"
            ) from exc
        finally:
            # Leaving one tiny probe object behind is not worth failing over.
            with contextlib.suppress(ClientError):
                client.delete_object(Bucket=bucket, Key=PROBE_KEY)

        self.stdout.write(self.style.SUCCESS("write, read and presign all OK"))
