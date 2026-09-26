import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "OWNER",
        "owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner-views",
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "EMP01",
        "1234",
        display_name="Employee One",
        square_team_member_id="TM-employee-one",
    )


@pytest.mark.parametrize(
    "url_name",
    ["accounts:employees", "accounts:employee-create"],
)
def test_employee_cannot_open_owner_account_pages(client, employee, url_name):
    client.force_login(employee)

    response = client.get(reverse(url_name))

    assert response.status_code == 403


def test_employee_cannot_edit_another_employee(client, employee, db):
    other = User.objects.create_user(
        "EMP02",
        "5678",
        display_name="Employee Two",
        square_team_member_id="TM-employee-two",
    )
    client.force_login(employee)

    response = client.get(reverse("accounts:employee-edit", args=[other.pk]))

    assert response.status_code == 403


def test_owner_can_create_employee_without_granting_privileges(client, owner):
    client.force_login(owner)

    response = client.post(
        reverse("accounts:employee-create"),
        {
            "display_name": "New Cashier",
            "login_code": "NEW01",
            "square_team_member_id": "TM-new-cashier",
            "is_active": "on",
            "pin": "2468",
        },
    )

    assert response.status_code == 302
    employee = User.objects.get(login_code="NEW01")
    assert employee.role == Role.EMPLOYEE
    assert employee.is_staff is False
    assert employee.is_superuser is False
    assert employee.check_password("2468")
    assert AuditEvent.objects.filter(
        action="employee.created",
        actor=owner,
        target_id=str(employee.pk),
    ).exists()


def test_employee_creation_rolls_back_if_audit_write_fails(client, owner, monkeypatch):
    client.force_login(owner)
    monkeypatch.setattr(
        "apps.accounts.views.record_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        client.post(
            reverse("accounts:employee-create"),
            {
                "display_name": "Unaudited Cashier",
                "login_code": "NOAUDIT",
                "square_team_member_id": "TM-no-audit",
                "is_active": "on",
                "pin": "2468",
            },
        )

    assert not User.objects.filter(login_code="NOAUDIT").exists()


def test_blank_pin_on_edit_keeps_existing_password(client, owner, employee):
    old_password = employee.password
    client.force_login(owner)

    response = client.post(
        reverse("accounts:employee-edit", args=[employee.pk]),
        {
            "display_name": "Renamed Employee",
            "login_code": employee.login_code,
            "square_team_member_id": employee.square_team_member_id,
            "is_active": "on",
            "pin": "",
        },
    )

    assert response.status_code == 302
    employee.refresh_from_db()
    assert employee.display_name == "Renamed Employee"
    assert employee.password == old_password
    assert employee.check_password("1234")


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example/steal",
    ],
)
def test_login_rejects_external_or_backslash_next_urls(client, employee, unsafe_next):
    response = client.post(
        reverse("accounts:login"),
        {"login_code": employee.login_code, "pin": "1234", "next": unsafe_next},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("core:home")


def test_login_accepts_a_local_next_url(client, employee):
    next_url = reverse("capture:list") + "?from=login"

    response = client.post(
        reverse("accounts:login"),
        {"login_code": employee.login_code, "pin": "1234", "next": next_url},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == next_url


def test_logout_is_post_only_and_post_ends_the_session(client, employee):
    client.force_login(employee)

    assert client.get(reverse("accounts:logout")).status_code == 405
    response = client.post(reverse("accounts:logout"))

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("accounts:login")
    assert "_auth_user_id" not in client.session
    assert AuditEvent.objects.filter(action="session.logout", actor=employee).exists()


def test_owner_can_log_in_with_a_password_longer_than_employee_pin_limit(client, owner):
    password = "A-long-owner-password!2026"
    owner.set_password(password)
    owner.save(update_fields=["password", "pin_changed_at"])

    response = client.post(
        reverse("accounts:login"),
        {"login_code": owner.login_code, "pin": password},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("core:home")
