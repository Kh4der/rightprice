import datetime as dt

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.audit.services import record_event


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "OWNER",
        "owner-password",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TM-owner-audit",
    )


@pytest.fixture
def employee(db):
    return User.objects.create_user(
        "EMP01",
        "1234",
        display_name="Employee One",
        square_team_member_id="TM-audit-employee",
    )


def make_event(*, actor, action, created_at=None):
    kwargs = {}
    if created_at is not None:
        kwargs["created_at"] = created_at
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type="capture.submission",
        target_id="record-1",
        detail={"safe": True},
        **kwargs,
    )


def test_audit_log_is_owner_only(client, owner, employee):
    anonymous = client.get(reverse("audit:log"))
    assert anonymous.status_code == 302
    assert reverse("accounts:login") in anonymous.headers["Location"]

    client.force_login(employee)
    assert client.get(reverse("audit:log")).status_code == 403

    client.force_login(owner)
    assert client.get(reverse("audit:log")).status_code == 200


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-02-30", "2026-13-01"])
def test_invalid_date_filter_never_crashes_or_reaches_database_with_bad_value(
    client,
    owner,
    bad_date,
):
    make_event(actor=owner, action="submission.approved")
    client.force_login(owner)

    response = client.get(reverse("audit:log"), {"date": bad_date})

    assert response.status_code == 200
    assert response.context["selected_date"] == ""
    assert response.context["page"].paginator.count == 1


def test_audit_log_filters_by_valid_date_and_action(client, owner):
    first_day = dt.datetime(2026, 9, 25, 12, tzinfo=dt.UTC)
    second_day = dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC)
    matching = make_event(
        actor=owner,
        action="submission.approved",
        created_at=first_day,
    )
    make_event(actor=owner, action="submission.rejected", created_at=first_day)
    make_event(actor=owner, action="submission.approved", created_at=second_day)
    client.force_login(owner)

    response = client.get(
        reverse("audit:log"),
        {"date": "2026-09-25", "action": "submission.approved"},
    )

    assert response.status_code == 200
    assert list(response.context["page"].object_list) == [matching]
    assert response.context["selected_date"] == "2026-09-25"
    assert response.context["selected_action"] == "submission.approved"


def test_audit_log_paginates_without_losing_filters(client, owner):
    for _index in range(51):
        make_event(actor=owner, action="submission.approved")
    client.force_login(owner)

    response = client.get(
        reverse("audit:log"),
        {"action": "submission.approved", "page": 1},
    )

    assert response.status_code == 200
    assert len(response.context["page"].object_list) == 50
    assert response.context["page"].has_next()
    assert b"action=submission.approved" in response.content


def test_record_event_uses_actor_and_first_forwarded_ip(owner):
    request = RequestFactory().post(
        "/owner/action/",
        HTTP_X_FORWARDED_FOR="203.0.113.10, 10.0.0.5",
        REMOTE_ADDR="127.0.0.1",
    )
    request.user = owner

    event = record_event(request, "employee.updated", owner, {"field": "display_name"})

    assert event.actor == owner
    assert event.ip_address == "203.0.113.10"
    assert event.target_type == "accounts.user"
    assert event.target_id == str(owner.pk)
    assert event.detail == {"field": "display_name"}
