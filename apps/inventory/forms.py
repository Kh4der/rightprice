"""Forms for the human review portion of inventory receiving."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from django import forms

from .models import Delivery, DeliveryLine


class DeliveryHeaderForm(forms.ModelForm):
    invoice_total = forms.DecimalField(
        label="Invoice total",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )

    class Meta:
        model = Delivery
        fields = ("vendor_name_raw", "invoice_number", "invoice_date")
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "vendor_name_raw": forms.TextInput(attrs={"autocomplete": "organization"}),
            "invoice_number": forms.TextInput(attrs={"autocomplete": "off"}),
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels: ClassVar[dict[str, str]] = {
            "vendor_name_raw": "Distributor",
            "invoice_number": "Invoice number",
            "invoice_date": "Invoice date",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.invoice_total_cents is not None:
            self.initial["invoice_total"] = Decimal(self.instance.invoice_total_cents) / 100


class DeliveryLineForm(forms.ModelForm):
    """Editable invoice facts; Square matching is handled in a separate step."""

    unit_cost = forms.DecimalField(
        label="Invoice unit cost",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )
    line_total = forms.DecimalField(
        label="Invoice line total",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )

    class Meta:
        model = DeliveryLine
        fields = (
            "description",
            "vendor_sku",
            "upc",
            "pack_text",
            "cases",
            "units_per_case",
            "received_units",
            "unit_cost",
            "line_total",
            "included",
            "review_note",
        )
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "description": forms.TextInput(attrs={"autocomplete": "off"}),
            "vendor_sku": forms.TextInput(attrs={"autocomplete": "off"}),
            "upc": forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
            "pack_text": forms.TextInput(attrs={"autocomplete": "off"}),
            "cases": forms.NumberInput(attrs={"step": "0.001", "inputmode": "decimal"}),
            "units_per_case": forms.NumberInput(attrs={"min": "1", "inputmode": "numeric"}),
            "received_units": forms.NumberInput(
                attrs={"step": "1", "min": "0", "inputmode": "numeric"}
            ),
            "review_note": forms.TextInput(attrs={"autocomplete": "off"}),
        }
        labels: ClassVar[dict[str, str]] = {
            "pack_text": "Pack printed on invoice",
            "cases": "Cases received",
            "units_per_case": "Sellable bottles per case",
            "received_units": "Total sellable bottles",
            "included": "Add this line to Square inventory",
            "review_note": "Review note",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.unit_cost_cents is not None:
            self.initial["unit_cost"] = Decimal(self.instance.unit_cost_cents) / 100
        if self.instance and self.instance.line_total_cents is not None:
            self.initial["line_total"] = Decimal(self.instance.line_total_cents) / 100

    def clean_upc(self) -> str:
        return "".join(
            character for character in self.cleaned_data.get("upc", "") if character.isdigit()
        )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("included"):
            return cleaned

        cases = cleaned.get("cases")
        units_per_case = cleaned.get("units_per_case")
        received_units = cleaned.get("received_units")
        if cases is not None and cases < 0:
            self.add_error("cases", "Cases cannot be negative.")
        if received_units is not None and received_units <= 0:
            self.add_error("received_units", "Enter at least one sellable bottle.")
        if received_units is not None and received_units != received_units.to_integral_value():
            self.add_error("received_units", "Inventory must be a whole number of bottles.")
        if cases is not None and units_per_case is not None and received_units is not None:
            expected = cases * Decimal(units_per_case)
            if expected != received_units:
                self.add_error(
                    "received_units",
                    "This must equal cases received multiplied by bottles per case.",
                )
        unit_cost = cleaned.get("unit_cost")
        line_total = cleaned.get("line_total")
        if unit_cost is not None and received_units is not None and line_total is not None:
            expected_total = unit_cost * received_units
            if expected_total != line_total:
                self.add_error(
                    "line_total",
                    "This must equal invoice unit cost multiplied by received bottles.",
                )
        return cleaned

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        unit_cost = self.cleaned_data.get("unit_cost")
        line_total = self.cleaned_data.get("line_total")
        instance.unit_cost_cents = int(unit_cost * 100) if unit_cost is not None else None
        instance.line_total_cents = int(line_total * 100) if line_total is not None else None
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class CatalogSearchForm(forms.Form):
    q = forms.CharField(
        label="Search Square inventory",
        max_length=100,
        required=False,
        widget=forms.SearchInput(
            attrs={
                "placeholder": "Item name, SKU, or barcode",
                "autocomplete": "off",
            }
        ),
    )


class SquareMatchForm(forms.Form):
    variation_id = forms.CharField(max_length=64, widget=forms.HiddenInput)
    remember_mapping = forms.BooleanField(
        required=False,
        initial=True,
        label="Remember this match for future invoices from this vendor",
    )


class NewSquareItemForm(forms.Form):
    """Explicit owner input for a genuinely new sellable Square variation."""

    item_name = forms.CharField(
        label="Product name",
        max_length=300,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    variation_name = forms.CharField(
        label="Bottle size or variation",
        max_length=300,
        initial="Regular",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    sku = forms.CharField(
        label="Square SKU",
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    upc = forms.CharField(
        label="Barcode",
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    sale_price = forms.DecimalField(
        label="Retail sale price",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
        help_text="This is the customer price in Square, not the invoice cost.",
    )
    variable_price = forms.BooleanField(
        required=False,
        label="Use a variable price in Square",
    )
    confirm = forms.BooleanField(
        label=(
            "I searched the Square catalog and confirm this is a new product, "
            "not another size or spelling of an existing item."
        )
    )

    def clean_upc(self) -> str:
        value = "".join(
            character for character in self.cleaned_data.get("upc", "") if character.isdigit()
        )
        if value and not 8 <= len(value) <= 14:
            raise forms.ValidationError("Enter the complete 8-14 digit barcode.")
        return value

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("variable_price") and cleaned.get("sale_price") is None:
            self.add_error("sale_price", "Enter the retail price or choose variable price.")
        return cleaned


class SquarePushConfirmationForm(forms.Form):
    confirm = forms.BooleanField(
        label="I reviewed the item matches, case sizes, and projected Square counts.",
    )
