"""
Standing store configuration.

The starting drawer float is the case that shapes this whole module. Every
drawer opens with the same bank — $265 at this store — so it is a setting, not
something to re-read off a photograph each morning.

But it cannot be a single mutable number. If the owner raises the float from
$265 to $300 today, every past day must still reconcile against the $265 that
was actually in the till at the time. Overwriting one value would silently
rewrite history and turn months of balanced days into apparent shortages.

So the float is an append-only, effective-dated series: changing it adds a row,
and any day looks up the row that was in force on that date. Nothing is ever
edited or deleted, which also means the owner can always see who changed the
bank, when, and why.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# Used only when nothing has been configured yet, and equal to the float the
# store actually runs, so a fresh install reconciles correctly on day one.
DEFAULT_FLOAT_CENTS = 26_500


class DrawerFloatQuerySet(models.QuerySet):
    def in_force_on(self, day: dt.date):
        """The float that applied on `day` — the latest one effective by then."""
        return self.filter(effective_from__lte=day).order_by(
            "-effective_from", "-created_at", "-pk"
        )

    def update(self, **kwargs):
        raise TypeError("DrawerFloat entries are append-only; create a new effective-dated row.")

    def delete(self):
        raise TypeError("DrawerFloat entries are append-only and cannot be deleted.")


class DrawerFloat(models.Model):
    """
    The cash a drawer is opened with, as of a date.

    Append-only: to change the float, add a row. There is deliberately no edit
    or delete path, because a money figure that can be rewritten after the fact
    is not evidence of anything.
    """

    amount_cents = models.BigIntegerField(
        help_text="The starting bank, in cents. 26500 is $265.00."
    )
    effective_from = models.DateField(
        help_text="The first business day this float applies to.",
    )

    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="drawer_floats",
    )
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = DrawerFloatQuerySet.as_manager()

    class Meta:
        ordering = ["-effective_from", "-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0),
                name="drawer_float_amount_nonnegative",
            ),
        ]
        indexes = [models.Index(fields=["-effective_from"])]

    def __str__(self) -> str:
        return f"${self.amount_cents / 100:.2f} from {self.effective_from}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise TypeError(
                "DrawerFloat entries are append-only; create a new effective-dated row."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("DrawerFloat entries are append-only and cannot be deleted.")

    # ------------------------------------------------------------------ API

    @classmethod
    def cents_for(cls, day: dt.date) -> int:
        """
        The starting float for one business day.

        Falls back to the default when the store has not configured one yet,
        rather than returning zero — a zero float would make every day look
        wildly over.
        """
        row = cls.objects.in_force_on(day).first()
        return row.amount_cents if row else DEFAULT_FLOAT_CENTS

    @classmethod
    def current(cls, today: dt.date | None = None):
        """The row in force now, or None if the store has never set one."""
        if today is None:
            # Django itself runs in UTC; store operations do not. Reuse the same
            # cutoff-aware mapping as Square queries so a float scheduled for the
            # next business day never becomes active early near UTC midnight.
            from apps.squareapi.client import business_day_for

            today = business_day_for(timezone.now())
        return cls.objects.in_force_on(today).first()

    @classmethod
    def set_to(cls, *, amount_cents: int, effective_from: dt.date, user, note: str = ""):
        """
        Record a new float.

        Setting the same amount again from the same date is a no-op rather than
        a duplicate row, so a double-submitted form does not clutter the history.
        """
        if amount_cents < 0:
            raise ValidationError({"amount_cents": "The starting drawer float cannot be negative."})

        existing = cls.objects.filter(effective_from=effective_from).first()
        if existing and existing.amount_cents == amount_cents:
            return existing

        entry = cls(
            amount_cents=amount_cents,
            effective_from=effective_from,
            set_by=user,
            note=note,
        )
        entry.full_clean()
        entry.save()
        return entry
