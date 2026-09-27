"""Create the isolated public practice owner used by the guided demo."""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Role, User

DEMO_LOGIN_CODE = "ADMIN_DEMO"
DEFAULT_DEMO_PASSWORD = "RightPrice!Demo26"


class Command(BaseCommand):
    help = "Create or reset the isolated ADMIN_DEMO practice account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=None,
            help="Defaults to $DEMO_OWNER_PASSWORD or the documented public demo password.",
        )

    def handle(self, *args, **options):
        password = (
            options["password"]
            or os.environ.get("DEMO_OWNER_PASSWORD")
            or DEFAULT_DEMO_PASSWORD
        )
        with transaction.atomic():
            user = User.objects.filter(login_code=DEMO_LOGIN_CODE).first()
            if user is None:
                user = User.objects.create_user(
                    login_code=DEMO_LOGIN_CODE,
                    password=password,
                    display_name="Demo Owner",
                    role=Role.OWNER,
                    is_demo=True,
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                )
                action = "created"
            elif not user.is_demo:
                raise CommandError(
                    f"{DEMO_LOGIN_CODE} already belongs to a live account; it was not changed."
                )
            else:
                user.display_name = "Demo Owner"
                user.role = Role.OWNER
                user.is_demo = True
                user.is_active = True
                user.is_staff = False
                user.is_superuser = False
                user.square_team_member_id = None
                user.set_password(password)
                user.full_clean(exclude=["password", "last_login"])
                user.save()
                action = "reset"

        self.stdout.write(self.style.SUCCESS(f"{action}: {DEMO_LOGIN_CODE} (practice only)"))
