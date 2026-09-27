"""
Create the initial owner and employee logins.

Credentials are read from the environment, never hardcoded here, because this
file is committed and a secret in git is a secret on every laptop that clones it.
Set them in .env (which is gitignored):

    SEED_OWNER_PASSWORD=...
    SEED_EMPLOYEE_PIN=...

Then:

    uv run python manage.py seed_accounts

Re-running is safe: existing accounts are left unchanged unless an explicit
update flag is supplied. In particular, a deploy must never reactivate an
offboarded employee or roll a PIN back to an old value from the environment.
"""

import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Role, User

MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 12


class Command(BaseCommand):
    help = "Create or update the owner and employee accounts from environment PINs."

    def add_arguments(self, parser):
        parser.add_argument("--owner-code", default="ADMIN")
        parser.add_argument("--owner-name", default="Store Owner")
        parser.add_argument(
            "--owner-password",
            default=None,
            help=(
                "Defaults to $SEED_OWNER_PASSWORD. Prefer the environment to avoid shell history."
            ),
        )
        parser.add_argument("--employee-code", default="JD")
        parser.add_argument("--employee-name", default="Store Employee")
        parser.add_argument(
            "--employee-pin",
            default=None,
            help="Defaults to $SEED_EMPLOYEE_PIN. Prefer the environment to avoid shell history.",
        )
        parser.add_argument(
            "--owner-square-id",
            default=None,
            help="The owner's Square team member ID, once known.",
        )
        parser.add_argument(
            "--employee-square-id",
            default=None,
            help="The employee's Square team member ID. Run verify_square to find it.",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help="Update names, roles and supplied Square IDs on existing accounts.",
        )
        parser.add_argument(
            "--rotate-existing-credentials",
            action="store_true",
            help="Replace existing owner password and employee PIN from supplied values.",
        )
        parser.add_argument(
            "--reactivate-existing",
            action="store_true",
            help="Reactivate existing accounts. Never happens implicitly.",
        )

    def handle(self, *args, **opts):
        owner_password = opts["owner_password"] or os.environ.get("SEED_OWNER_PASSWORD")
        employee_pin = opts["employee_pin"] or os.environ.get("SEED_EMPLOYEE_PIN")
        if opts["owner_code"].strip().upper() == opts["employee_code"].strip().upper():
            raise CommandError("Owner and employee login codes must be different.")

        with transaction.atomic():
            owner = self._ensure_account(
                login_code=opts["owner_code"],
                display_name=opts["owner_name"],
                pin=owner_password,
                role=Role.OWNER,
                square_team_member_id=opts["owner_square_id"],
                label="owner",
                update_existing=opts["update_existing"],
                rotate_existing_pin=opts["rotate_existing_credentials"],
                reactivate_existing=opts["reactivate_existing"],
            )
            employee = self._ensure_account(
                login_code=opts["employee_code"],
                display_name=opts["employee_name"],
                pin=employee_pin,
                role=Role.EMPLOYEE,
                square_team_member_id=opts["employee_square_id"],
                label="employee",
                update_existing=opts["update_existing"],
                rotate_existing_pin=opts["rotate_existing_credentials"],
                reactivate_existing=opts["reactivate_existing"],
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Account state:"))
        for user in (owner, employee):
            square = user.square_team_member_id or "(no Square team member ID yet)"
            status = "active" if user.is_active else "inactive"
            self.stdout.write(
                f"  {user.login_code:8s} {user.role:9s} {status:8s} {user.display_name}  {square}"
            )
        self.stdout.write("")
        self.stdout.write(
            "Credentials are not printed. To rotate existing credentials, re-run with "
            "--rotate-existing-credentials; existing accounts are otherwise left unchanged."
        )

    def _validate_credential(self, *, label: str, credential: str | None, user: User) -> str:
        if not credential:
            raise CommandError(
                "No owner password. Set SEED_OWNER_PASSWORD in .env or pass --owner-password."
                if label == "owner"
                else ("No employee PIN. Set SEED_EMPLOYEE_PIN in .env or pass --employee-pin.")
            )
        if label == "employee":
            if not credential.isdigit() or not MIN_PIN_LENGTH <= len(credential) <= MAX_PIN_LENGTH:
                raise CommandError(
                    f"The employee PIN must be {MIN_PIN_LENGTH}-{MAX_PIN_LENGTH} digits."
                )
            return credential

        try:
            validate_password(credential, user=user)
        except ValidationError as exc:
            raise CommandError(
                "The owner password is not strong enough: " + " ".join(exc.messages)
            ) from exc
        return credential

    def _ensure_account(
        self,
        *,
        login_code,
        display_name,
        pin,
        role,
        square_team_member_id,
        label,
        update_existing,
        rotate_existing_pin,
        reactivate_existing,
    ):
        login_code = login_code.strip().upper()
        user = User.objects.filter(login_code=login_code).first()
        if user is not None and user.is_demo:
            raise CommandError(
                "Demo accounts are isolated. Use seed_demo_account to reset ADMIN_DEMO."
            )

        if user is None:
            user = User(login_code=login_code)
            user.display_name = display_name
            user.role = role
            user.is_staff = role == Role.OWNER
            user.is_superuser = role == Role.OWNER
            user.is_active = True
            user.square_team_member_id = square_team_member_id or None
            user.set_password(self._validate_credential(label=label, credential=pin, user=user))
            user.full_clean(exclude=["password", "last_login"])
            user.save()
            action = "created"
        else:
            changed: set[str] = set()

            if update_existing:
                profile = {
                    "display_name": display_name,
                    "role": role,
                    "is_staff": role == Role.OWNER,
                    "is_superuser": role == Role.OWNER,
                }
                if square_team_member_id:
                    profile["square_team_member_id"] = square_team_member_id
                for field, value in profile.items():
                    if getattr(user, field) != value:
                        setattr(user, field, value)
                        changed.add(field)

            if reactivate_existing and not user.is_active:
                user.is_active = True
                changed.add("is_active")

            if rotate_existing_pin:
                user.set_password(self._validate_credential(label=label, credential=pin, user=user))
                changed.update({"password", "pin_changed_at"})

            if changed:
                user.full_clean(exclude=["password", "last_login"])
                user.save(update_fields=sorted(changed))
                action = "updated"
            else:
                action = "unchanged"

        self.stdout.write(f"{action}: {login_code} ({user.role})")
        return user
