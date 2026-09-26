from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def existing_accounts():
    owner = User.objects.create_superuser(
        "ADMIN",
        "Existing!Owner#2026",
        display_name="Existing Owner",
        square_team_member_id="TM-owner-old",
    )
    employee = User.objects.create_user(
        "JD",
        "8888",
        display_name="Existing Employee",
        square_team_member_id="TM-employee-old",
    )
    employee.is_active = False
    employee.save(update_fields=["is_active"])
    return owner, employee


def test_rerun_leaves_existing_accounts_pin_and_status_unchanged(existing_accounts, monkeypatch):
    owner, employee = existing_accounts
    monkeypatch.setenv("SEED_OWNER_PASSWORD", "Unused!Owner#2026")
    monkeypatch.setenv("SEED_EMPLOYEE_PIN", "2222")

    output = StringIO()
    call_command(
        "seed_accounts",
        owner_name="Replacement Owner",
        employee_name="Replacement Employee",
        owner_square_id="TM-owner-new",
        employee_square_id="TM-employee-new",
        stdout=output,
    )

    owner.refresh_from_db()
    employee.refresh_from_db()
    assert owner.display_name == "Existing Owner"
    assert owner.square_team_member_id == "TM-owner-old"
    assert owner.check_password("Existing!Owner#2026")
    assert employee.display_name == "Existing Employee"
    assert employee.square_team_member_id == "TM-employee-old"
    assert employee.check_password("8888")
    assert not employee.is_active
    assert output.getvalue().count("unchanged:") == 2


def test_explicit_flags_update_rotate_and_reactivate(existing_accounts):
    owner, employee = existing_accounts

    call_command(
        "seed_accounts",
        owner_name="Updated Owner",
        employee_name="Updated Employee",
        owner_password="Rotated!Owner#2026",
        employee_pin="2222",
        owner_square_id="TM-owner-new",
        employee_square_id="TM-employee-new",
        update_existing=True,
        rotate_existing_credentials=True,
        reactivate_existing=True,
        stdout=StringIO(),
    )

    owner.refresh_from_db()
    employee.refresh_from_db()
    assert owner.display_name == "Updated Owner"
    assert owner.square_team_member_id == "TM-owner-new"
    assert owner.check_password("Rotated!Owner#2026")
    assert employee.display_name == "Updated Employee"
    assert employee.square_team_member_id == "TM-employee-new"
    assert employee.check_password("2222")
    assert employee.is_active


def test_existing_accounts_do_not_require_seed_pin_secrets(existing_accounts, monkeypatch):
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_EMPLOYEE_PIN", raising=False)

    call_command("seed_accounts", stdout=StringIO())


def test_new_accounts_still_require_valid_pins(monkeypatch):
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("SEED_EMPLOYEE_PIN", raising=False)

    with pytest.raises(CommandError, match="No owner password"):
        call_command("seed_accounts", stdout=StringIO())

    assert User.objects.count() == 0


def test_new_accounts_are_created_from_environment(monkeypatch):
    monkeypatch.setenv("SEED_OWNER_PASSWORD", "Created!Owner#2026")
    monkeypatch.setenv("SEED_EMPLOYEE_PIN", "5678")

    call_command("seed_accounts", stdout=StringIO())

    assert User.objects.get(login_code="ADMIN").check_password("Created!Owner#2026")
    assert User.objects.get(login_code="JD").check_password("5678")


def test_owner_and_employee_codes_must_be_distinct():
    with pytest.raises(CommandError, match="must be different"):
        call_command(
            "seed_accounts",
            owner_code="same",
            employee_code="SAME",
            stdout=StringIO(),
        )


def test_weak_owner_password_is_rejected(monkeypatch):
    monkeypatch.setenv("SEED_OWNER_PASSWORD", "1234")
    monkeypatch.setenv("SEED_EMPLOYEE_PIN", "5678")

    with pytest.raises(CommandError, match="owner password is not strong enough"):
        call_command("seed_accounts", stdout=StringIO())

    assert User.objects.count() == 0
