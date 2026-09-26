from decimal import Decimal

from django import forms


class MoneyAmountForm(forms.Form):
    """Strict dollar input shared by owner-side cash confirmations."""

    amount = forms.DecimalField(
        min_value=0,
        max_digits=10,
        decimal_places=2,
    )

    @staticmethod
    def initial_from_cents(cents):
        return f"{Decimal(cents) / Decimal(100):.2f}"


class DailyCashCountForm(MoneyAmountForm):
    note = forms.CharField(required=False, max_length=300)


# Kept as an import alias for deployments upgrading from the original
# same-day collection screen. New code and UI call this Daily cash.
CashCollectionForm = DailyCashCountForm


class PayoutAmountForm(MoneyAmountForm):
    pass
