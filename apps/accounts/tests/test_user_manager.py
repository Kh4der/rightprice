import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.accounts.models import Role, User

pytestmark = pytest.mark.django_db


def test_standard_password_keyword_creates_a_usable_user():
    user = User.objects.create_user(
        login_code="JD",
        password="1234",
        display_name="Jamie Doe",
    )

    assert user.has_usable_password()
    assert user.check_password("1234")


def test_standard_password_keyword_creates_a_usable_superuser():
    owner = User.objects.create_superuser(
        login_code="ADMIN",
        password="LongerOwnerPassword123!",
        display_name="Store Owner",
    )

    assert owner.check_password("LongerOwnerPassword123!")
    assert owner.role == Role.OWNER
    assert owner.is_staff
    assert owner.is_superuser


def test_django_createsuperuser_command_creates_a_usable_owner(monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "CommandOwnerPassword123!")

    call_command(
        "createsuperuser",
        interactive=False,
        login_code="CMDOWNER",
        display_name="Command Owner",
        verbosity=0,
    )

    owner = User.objects.get(login_code="CMDOWNER")
    assert owner.check_password("CommandOwnerPassword123!")
    assert owner.role == Role.OWNER
    assert owner.is_staff
    assert owner.is_superuser


def test_legacy_pin_keyword_remains_supported():
    user = User.objects.create_user(
        login_code="MK",
        pin="5678",
        display_name="Mo Khan",
    )

    assert user.check_password("5678")


def test_password_and_legacy_pin_cannot_both_be_passed():
    with pytest.raises(TypeError, match="not both"):
        User.objects.create_user(
            login_code="AB",
            password="1234",
            pin="5678",
            display_name="Ambiguous",
        )


def test_create_user_with_owner_role_gets_valid_staff_flags():
    owner = User.objects.create_user(
        login_code="OWNER2",
        password="1234",
        display_name="Second Owner",
        role=Role.OWNER,
    )

    assert owner.is_staff
    assert not owner.is_superuser


def test_employee_cannot_be_created_as_staff_or_superuser():
    with pytest.raises(ValueError, match="employee"):
        User.objects.create_user(
            login_code="BAD1",
            password="1234",
            display_name="Bad Employee",
            is_staff=True,
        )

    with pytest.raises(ValueError, match="employee"):
        User.objects.create_user(
            login_code="BAD2",
            password="1234",
            display_name="Bad Employee",
            is_superuser=True,
        )


def test_demo_account_must_be_an_owner():
    with pytest.raises(ValueError, match="owner role"):
        User.objects.create_user(
            login_code="BADDEMO",
            password="1234",
            display_name="Bad demo employee",
            is_demo=True,
        )


def test_database_constraint_rejects_privilege_escalation_via_bulk_update():
    employee = User.objects.create_user("EMP01", "1234", display_name="Employee")

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=employee.pk).update(is_superuser=True)


def test_database_constraint_rejects_demo_employee_via_bulk_update():
    employee = User.objects.create_user("EMP02", "1234", display_name="Employee")

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=employee.pk).update(is_demo=True)
