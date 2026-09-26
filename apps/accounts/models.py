"""
Employee and owner accounts.

Identity here is deliberately two-part:

* ``square_team_member_id`` is the real identity. Square stamps it on cash
  drawer shifts and on inventory adjustments, so it is what lets a drawer or a
  delivery attribute back to a person. It is an opaque string like
  ``TMa1bC2dE3fG4hI5j`` and nobody is going to type it on a phone at 11pm.

* ``login_code`` is what the employee actually types — a short code the owner
  assigns ("JD", "07", "MARIA"). It exists only so the Square ID does not have
  to be typed, and it is the ``USERNAME_FIELD``.

The PIN lives in Django's own ``password`` field so it gets Argon2 hashing,
``set_password``/``check_password``, and the standard auth machinery for free. A
4-6 digit PIN has a tiny keyspace and hashing alone does not save it; what
bounds the damage is the django-axes lockout configured in settings.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    OWNER = "OWNER", "Owner"
    EMPLOYEE = "EMPLOYEE", "Employee"


login_code_validator = RegexValidator(
    regex=r"^[A-Z0-9]{2,12}$",
    message="Login code must be 2-12 characters, uppercase letters and digits only.",
)


class UserManager(BaseUserManager):
    """Manager keyed on login_code rather than a username or email."""

    use_in_migrations = True

    @staticmethod
    def _password_with_legacy_pin(password: str | None, extra: dict) -> str | None:
        """
        Accept the old ``pin=`` keyword without breaking Django's manager API.

        Django itself (notably ``createsuperuser``) always calls a user manager
        with ``password=``.  The project originally named this argument ``pin``,
        which caused Django's password to fall into ``extra`` and then be
        overwritten with an unusable password.  Keep ``pin=`` as a temporary
        compatibility alias for application callers, but make ``password`` the
        canonical interface.
        """
        legacy_pin = extra.pop("pin", None)
        if password is not None and legacy_pin is not None:
            raise TypeError("Pass password or pin, not both.")
        return password if password is not None else legacy_pin

    @staticmethod
    def _validate_role_flags(extra: dict) -> None:
        role = extra.get("role")
        is_staff = extra.get("is_staff", False)
        is_superuser = extra.get("is_superuser", False)

        if role == Role.OWNER and not is_staff:
            raise ValueError("An owner must have is_staff=True.")
        if role == Role.EMPLOYEE and (is_staff or is_superuser):
            raise ValueError("An employee cannot be staff or a superuser.")

    def _create_user(self, login_code: str, password: str | None, **extra):
        if not login_code:
            raise ValueError("A login code is required.")
        login_code = login_code.strip().upper()
        self._validate_role_flags(extra)
        user = self.model(login_code=login_code, **extra)
        # A user with no usable PIN cannot log in, which is the right default for
        # an account the owner has created but not yet handed out.
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.full_clean(exclude=["password", "last_login"])
        user.save(using=self._db)
        return user

    def create_user(self, login_code: str, password: str | None = None, **extra):
        password = self._password_with_legacy_pin(password, extra)
        role = extra.setdefault("role", Role.EMPLOYEE)
        # ``create_user(..., role=OWNER)`` existed in early project code. Keep it
        # working, but make the resulting owner internally valid.
        extra.setdefault("is_staff", role == Role.OWNER)
        extra.setdefault("is_superuser", False)
        return self._create_user(login_code, password, **extra)

    def create_superuser(self, login_code: str, password: str | None = None, **extra):
        password = self._password_with_legacy_pin(password, extra)
        extra.setdefault("role", Role.OWNER)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra["role"] != Role.OWNER:
            raise ValueError("A superuser must have role=OWNER.")
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError("A superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(login_code, password, **extra)

    def employees(self):
        return self.filter(role=Role.EMPLOYEE)

    def owners(self):
        return self.filter(role=Role.OWNER)


class User(AbstractBaseUser, PermissionsMixin):
    login_code = models.CharField(
        max_length=12,
        unique=True,
        validators=[login_code_validator],
        help_text="Short code the employee types to sign in. Assigned by the owner.",
    )
    display_name = models.CharField(
        max_length=80,
        help_text="Shown in the app and on the owner's review screens.",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.EMPLOYEE)

    square_team_member_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text=(
            "The employee's Square team member ID. Required before their drawer "
            "shifts or inventory pushes can be attributed in Square."
        ),
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Unset instead of deleting: submissions must keep pointing at a real person.",
    )
    is_staff = models.BooleanField(
        default=False,
        help_text="Access to the Django admin. Owners only.",
    )

    date_joined = models.DateTimeField(default=timezone.now)
    pin_changed_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "login_code"
    REQUIRED_FIELDS = ["display_name"]

    class Meta:
        ordering = ["display_name"]
        constraints = [
            # Owners need admin access; employees must never acquire either staff
            # or superuser privileges through a bulk update or data import.
            models.CheckConstraint(
                condition=(
                    models.Q(role=Role.OWNER, is_staff=True)
                    | models.Q(role=Role.EMPLOYEE, is_staff=False, is_superuser=False)
                ),
                name="role_matches_privilege_flags",
            ),
        ]
        indexes = [
            models.Index(fields=["square_team_member_id"]),
            models.Index(fields=["role", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.login_code})"

    def clean(self):
        super().clean()
        if self.login_code:
            self.login_code = self.login_code.strip().upper()

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER

    @property
    def is_employee(self) -> bool:
        return self.role == Role.EMPLOYEE

    def set_password(self, raw_password):
        super().set_password(raw_password)
        # Recorded so the owner can see a PIN was rotated; the value never is.
        self.pin_changed_at = timezone.now()
