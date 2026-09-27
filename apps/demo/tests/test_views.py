import datetime as dt

import pytest
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from apps.accounts.management.commands.seed_demo_account import (
    DEFAULT_DEMO_PASSWORD,
    DEMO_LOGIN_CODE,
)
from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent
from apps.capture.models import Submission, SubmissionKind
from apps.capture.views import _queue
from apps.inventory.catalog_creation import CatalogWritesDisabled, create_square_catalog_item
from apps.inventory.models import Delivery, DeliveryLine
from apps.inventory.services import (
    DeliveryNotReady,
    InventoryWritesDisabled,
    push_delivery_to_square,
    refresh_square_counts,
)
from apps.reconcile.models import DailyReconciliation
from apps.reconcile.square_sync import sync_daily_reconciliation


@pytest.fixture
def demo_owner(db):
    return User.objects.create_user(
        DEMO_LOGIN_CODE,
        DEFAULT_DEMO_PASSWORD,
        display_name="Demo Owner",
        role=Role.OWNER,
        is_demo=True,
        is_staff=False,
        is_superuser=False,
    )


def test_seed_demo_account_is_idempotent_and_never_gets_admin_access(db):
    call_command("seed_demo_account", verbosity=0)
    call_command("seed_demo_account", verbosity=0)

    user = User.objects.get(login_code=DEMO_LOGIN_CODE)
    assert user.check_password(DEFAULT_DEMO_PASSWORD)
    assert user.role == Role.OWNER
    assert user.is_demo is True
    assert user.is_staff is False
    assert user.is_superuser is False


def test_seed_demo_account_never_converts_a_live_account(db):
    live_user = User.objects.create_user(
        DEMO_LOGIN_CODE,
        "1234",
        display_name="Real employee",
    )

    with pytest.raises(CommandError, match="live account"):
        call_command("seed_demo_account", verbosity=0)

    live_user.refresh_from_db()
    assert live_user.is_demo is False
    assert live_user.role == Role.EMPLOYEE


def test_one_click_demo_login_starts_an_isolated_owner_workspace(client, demo_owner):
    response = client.post(reverse("demo:start"))

    assert response.status_code == 302
    assert response.headers["Location"] == reverse("demo:dashboard")
    dashboard = client.get(response.headers["Location"])
    assert dashboard.status_code == 200
    assert b"Learn the owner jobs" in dashboard.content
    assert b"Practice mode" in dashboard.content


def test_demo_login_and_logout_do_not_write_to_the_store_audit_log(client, demo_owner):
    login_response = client.post(
        reverse("accounts:login"),
        {"login_code": "admin_demo", "pin": DEFAULT_DEMO_PASSWORD},
    )
    assert login_response.headers["Location"] == reverse("demo:dashboard")

    logout_response = client.post(reverse("accounts:logout"))

    assert logout_response.status_code == 302
    assert AuditEvent.objects.count() == 0


def test_demo_owner_cannot_enter_real_routes_or_django_admin(client, demo_owner):
    client.force_login(demo_owner)

    assert client.get(reverse("core:home")).headers["Location"] == reverse("demo:dashboard")
    assert client.get("/admin/").headers["Location"] == reverse("demo:dashboard")
    assert client.post(reverse("capture:daily"), {}).status_code == 403
    assert Submission.objects.count() == 0


def test_real_owner_cannot_open_demo_workspace(client, db):
    owner = User.objects.create_user(
        "LIVEOWNER",
        "owner-password",
        display_name="Live Owner",
        role=Role.OWNER,
    )
    client.force_login(owner)

    assert client.get(reverse("demo:dashboard")).status_code == 403


def test_demo_daily_cash_shows_dates_equation_and_visible_entry_field(client, demo_owner):
    client.force_login(demo_owner)

    response = client.get(reverse("demo:daily-cash"))
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["daily_cash_count"] == 5
    assert "Find a sample date" in content
    assert "Monday," in content
    assert "All cash in register" in content
    assert "Leave in drawer" in content
    assert "Cash this pouch should have" in content
    assert content.count("Cash you counted for this day") == 5
    assert "Save this day's cash" in content


def test_demo_cash_count_is_session_only_and_can_be_corrected(client, demo_owner):
    client.force_login(demo_owner)
    initial = client.get(reverse("demo:daily-cash"))
    monday = initial.context["rows"][0]
    action = reverse("demo:daily-cash-count", args=[monday["id"]])

    response = client.post(action, {"amount": "619.00", "note": "First practice count"})
    assert response.status_code == 302
    changed = client.get(reverse("demo:daily-cash"))
    changed_monday = changed.context["rows"][0]
    assert changed_monday["counted_cents"] == 61_900
    assert changed_monday["state"] == "issue"

    client.post(action, {"amount": "620.00", "note": "Counted again"})
    corrected = client.get(reverse("demo:daily-cash"))
    corrected_monday = corrected.context["rows"][0]
    assert corrected_monday["counted_cents"] == 62_000
    assert corrected_monday["state"] == "matched"
    assert corrected_monday["explanation"] == "Corrected after another count"
    assert len(corrected_monday["history"]) == 3
    assert Submission.objects.count() == 0


def test_public_login_explains_and_links_to_owner_practice(client, demo_owner):
    response = client.get(reverse("accounts:login"))
    content = response.content.decode()

    assert response.status_code == 200
    assert "Open owner practice" in content
    assert DEMO_LOGIN_CODE in content
    assert DEFAULT_DEMO_PASSWORD in content


def test_demo_state_is_different_for_each_browser_session(client, demo_owner):
    other_client = client.__class__()
    client.force_login(demo_owner)
    other_client.force_login(demo_owner)
    first = client.get(reverse("demo:daily-cash"))
    monday = first.context["rows"][0]

    client.post(
        reverse("demo:daily-cash-count", args=[monday["id"]]),
        {"amount": "1.00"},
    )

    first_after = client.get(reverse("demo:daily-cash")).context["rows"][0]
    other_after = other_client.get(reverse("demo:daily-cash")).context["rows"][0]
    assert first_after["counted_cents"] == 100
    assert other_after["counted_cents"] == 62_000


def test_demo_reset_restores_cash_payout_and_inventory_examples(client, demo_owner):
    client.force_login(demo_owner)
    inventory = client.get(reverse("demo:inventory"))
    suggested = next(line for line in inventory.context["lines"] if line["status"] == "suggested")
    client.post(reverse("demo:inventory-match", args=[suggested["id"]]))
    payouts = client.get(reverse("demo:payouts"))
    waiting = next(row for row in payouts.context["payouts"] if row["status"] == "waiting")
    client.post(reverse("demo:payout-reimburse", args=[waiting["id"]]))

    response = client.post(reverse("demo:reset"))

    assert response.status_code == 302
    reset_inventory = client.get(reverse("demo:inventory"))
    assert reset_inventory.context["unresolved_count"] == 1
    reset_payouts = client.get(reverse("demo:payouts"))
    assert sum(row["status"] == "waiting" for row in reset_payouts.context["payouts"]) == 1


def test_demo_dates_are_real_dates_from_one_completed_week(client, demo_owner):
    client.force_login(demo_owner)

    rows = client.get(reverse("demo:daily-cash")).context["rows"]
    dates = [row["business_day"] for row in rows]

    assert all(isinstance(day, dt.date) for day in dates)
    assert dates == [dates[0] + dt.timedelta(days=offset) for offset in range(5)]
    assert dates[0].weekday() == 0


def test_demo_date_navigation_stays_inside_the_sample_week(client, demo_owner):
    client.force_login(demo_owner)

    response = client.get(reverse("demo:daily-cash"), {"date": "2099-01-01"})
    content = response.content.decode()

    assert response.context["selected_date"] == response.context["sample_week_start"]
    assert response.context["week_start"] == response.context["sample_week_start"]
    assert response.context["week_end"] == response.context["sample_week_end"]
    assert "Previous 7 days" not in content
    assert "Next 7 days" not in content
    assert "Sample week:" in content


def test_demo_records_fail_closed_before_ai_or_square_services(demo_owner):
    daily_submission = Submission.objects.create(
        kind=SubmissionKind.DAILY_REPORT,
        submitted_by=demo_owner,
    )
    reconciliation = DailyReconciliation.objects.create(submission=daily_submission)
    inventory_submission = Submission.objects.create(
        kind=SubmissionKind.INVENTORY,
        submitted_by=demo_owner,
    )
    delivery = Delivery.objects.create(submission=inventory_submission)
    line = DeliveryLine.objects.create(
        delivery=delivery,
        position=1,
        description="Practice bottle",
    )

    with pytest.raises(PermissionDenied, match="AI service"):
        _queue(daily_submission)
    with pytest.raises(PermissionDenied, match="never reads"):
        sync_daily_reconciliation(reconciliation)
    with pytest.raises(DeliveryNotReady, match="never contacts"):
        refresh_square_counts(delivery, actor=demo_owner)
    with pytest.raises(InventoryWritesDisabled, match="never changes"):
        push_delivery_to_square(delivery, actor=demo_owner)
    with pytest.raises(CatalogWritesDisabled, match="never creates"):
        create_square_catalog_item(
            line,
            actor=demo_owner,
            item_name="Practice bottle",
            variation_name="750 mL",
            sale_price_cents=1_999,
            variable_price=False,
        )
