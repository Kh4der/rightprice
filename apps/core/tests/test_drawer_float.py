"""
The starting drawer float.

The behaviour that actually matters here is what happens to *old* days when the
float changes. A single mutable setting would silently rewrite history: raise
the bank from $265 to $300 and every previously balanced day becomes an
apparent $35 shortage, with no record of why. These tests pin that down.
"""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.accounts.models import Role, User
from apps.core.models import DEFAULT_FLOAT_CENTS, DrawerFloat

TODAY = dt.date(2026, 9, 26)
YESTERDAY = TODAY - dt.timedelta(days=1)
LAST_MONTH = TODAY - dt.timedelta(days=30)


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        "ADMIN",
        "2893",
        display_name="Store Owner",
        role=Role.OWNER,
        square_team_member_id="TMtest-owner",
    )


def test_default_applies_before_anything_is_configured(db):
    """
    A fresh install must not treat the float as zero.

    Zero would make every day look wildly over, which is worse than being
    slightly wrong, because it is not obviously wrong.
    """
    assert DrawerFloat.cents_for(TODAY) == DEFAULT_FLOAT_CENTS
    assert DrawerFloat.current(TODAY) is None


def test_configured_float_is_used(db, owner):
    DrawerFloat.set_to(amount_cents=26_500, effective_from=LAST_MONTH, user=owner)
    assert DrawerFloat.cents_for(TODAY) == 26_500


def test_raising_the_float_does_not_rewrite_history(db, owner):
    """The whole reason this is effective-dated rather than a single value."""
    DrawerFloat.set_to(amount_cents=26_500, effective_from=LAST_MONTH, user=owner)
    DrawerFloat.set_to(amount_cents=30_000, effective_from=TODAY, user=owner)

    assert DrawerFloat.cents_for(YESTERDAY) == 26_500, (
        "Changing the float retroactively altered a past day, which would turn "
        "every balanced day into an apparent shortage."
    )
    assert DrawerFloat.cents_for(TODAY) == 30_000


def test_a_day_before_any_entry_falls_back_to_the_default(db, owner):
    DrawerFloat.set_to(amount_cents=30_000, effective_from=TODAY, user=owner)
    assert DrawerFloat.cents_for(LAST_MONTH) == DEFAULT_FLOAT_CENTS


def test_the_latest_effective_entry_wins(db, owner):
    DrawerFloat.set_to(amount_cents=20_000, effective_from=LAST_MONTH, user=owner)
    DrawerFloat.set_to(amount_cents=26_500, effective_from=YESTERDAY, user=owner)
    DrawerFloat.set_to(amount_cents=30_000, effective_from=TODAY, user=owner)

    assert DrawerFloat.cents_for(LAST_MONTH) == 20_000
    assert DrawerFloat.cents_for(YESTERDAY) == 26_500
    assert DrawerFloat.cents_for(TODAY) == 30_000
    assert DrawerFloat.cents_for(TODAY + dt.timedelta(days=90)) == 30_000


def test_setting_the_same_amount_again_is_a_no_op(db, owner):
    """A double-submitted form should not clutter the audit history."""
    first = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)
    again = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)

    assert first.pk == again.pk
    assert DrawerFloat.objects.count() == 1


def test_changing_the_amount_for_the_same_date_adds_a_row(db, owner):
    """
    Correcting today's float is still an append, not an edit.

    The owner should be able to see that the figure was changed, not just what
    it ended up as.
    """
    DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)
    DrawerFloat.set_to(amount_cents=30_000, effective_from=TODAY, user=owner)

    assert DrawerFloat.objects.count() == 2
    assert DrawerFloat.cents_for(TODAY) == 30_000


def test_who_changed_it_is_recorded(db, owner):
    entry = DrawerFloat.set_to(
        amount_cents=30_000, effective_from=TODAY, user=owner, note="Holiday weekend"
    )
    assert entry.set_by == owner
    assert entry.note == "Holiday weekend"


def test_float_is_stored_in_integer_cents(db, owner):
    """No floats in the money path, here least of all."""
    entry = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)
    assert isinstance(entry.amount_cents, int)
    assert entry.amount_cents == 26_500


def test_negative_float_is_rejected_by_the_domain_api(db, owner):
    with pytest.raises(ValidationError, match="cannot be negative"):
        DrawerFloat.set_to(amount_cents=-1, effective_from=TODAY, user=owner)


def test_database_constraint_rejects_negative_float_from_bulk_insert(db, owner):
    with pytest.raises(IntegrityError), transaction.atomic():
        DrawerFloat.objects.bulk_create(
            [DrawerFloat(amount_cents=-1, effective_from=TODAY, set_by=owner)]
        )


def test_existing_float_cannot_be_edited(db, owner):
    entry = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)
    entry.amount_cents = 30_000

    with pytest.raises(TypeError, match="append-only"):
        entry.save()
    with pytest.raises(TypeError, match="append-only"):
        DrawerFloat.objects.filter(pk=entry.pk).update(amount_cents=30_000)


def test_existing_float_cannot_be_deleted(db, owner):
    entry = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)

    with pytest.raises(TypeError, match="append-only"):
        entry.delete()
    with pytest.raises(TypeError, match="append-only"):
        DrawerFloat.objects.filter(pk=entry.pk).delete()


@override_settings(
    STORE_TIMEZONE="America/New_York",
    BUSINESS_DAY_CUTOFF=dt.time(4, 0),
)
def test_current_uses_store_business_day_not_utc_date(db, owner, monkeypatch):
    # 05:00 UTC is 01:00 local: after UTC midnight, but before the store's 04:00
    # cutoff. The next day's float must not become active three hours early.
    moment = dt.datetime(2026, 9, 27, 5, 0, tzinfo=dt.UTC)
    monkeypatch.setattr("apps.core.models.timezone.now", lambda: moment)
    old = DrawerFloat.set_to(amount_cents=26_500, effective_from=TODAY, user=owner)
    DrawerFloat.set_to(
        amount_cents=30_000,
        effective_from=TODAY + dt.timedelta(days=1),
        user=owner,
    )

    assert DrawerFloat.current() == old
