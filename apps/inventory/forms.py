"""Forms for the human review portion of inventory receiving."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from django import forms
from django.forms import formset_factory

from .models import Delivery, DeliveryLine
from .packs import calculate_received_units, line_total_matches_rounded_unit_cost
from .pricing import LIQUOR_PRICING_CATEGORIES


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
        fields = (
            "vendor_name_raw",
            "invoice_number",
            "invoice_date",
            "printed_total_cases",
            "printed_total_loose_units",
            "printed_total_physical_units",
        )
        widgets: ClassVar[dict[str, forms.Widget]] = {
            "vendor_name_raw": forms.TextInput(attrs={"autocomplete": "organization"}),
            "invoice_number": forms.TextInput(attrs={"autocomplete": "off"}),
            "invoice_date": forms.DateInput(attrs={"type": "date"}),
            "printed_total_cases": forms.NumberInput(
                attrs={"step": "0.001", "min": "0", "inputmode": "decimal"}
            ),
            "printed_total_loose_units": forms.NumberInput(
                attrs={"step": "0.001", "min": "0", "inputmode": "decimal"}
            ),
            "printed_total_physical_units": forms.NumberInput(
                attrs={"step": "0.001", "min": "0", "inputmode": "decimal"}
            ),
        }
        labels: ClassVar[dict[str, str]] = {
            "vendor_name_raw": "Distributor",
            "invoice_number": "Invoice number",
            "invoice_date": "Invoice date",
            "printed_total_cases": "Footer: total cases",
            "printed_total_loose_units": "Footer: loose bottles/cans",
            "printed_total_physical_units": "Footer: total physical bottles/cans",
        }
        help_texts: ClassVar[dict[str, str]] = {
            "printed_total_cases": "Use TOTAL CASES or the case side of TOTAL CS/BTLS.",
            "printed_total_loose_units": "Use TOTAL BOT or the loose side of TOTAL CS/BTLS.",
            "printed_total_physical_units": "Use TOTAL BOTTLES only; leave blank if not printed.",
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
            "loose_units",
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
            "loose_units": forms.NumberInput(
                attrs={"step": "1", "min": "0", "inputmode": "numeric"}
            ),
            "units_per_case": forms.NumberInput(attrs={"min": "1", "inputmode": "numeric"}),
            "received_units": forms.NumberInput(
                attrs={"step": "1", "min": "0", "inputmode": "numeric"}
            ),
            "review_note": forms.TextInput(attrs={"autocomplete": "off"}),
        }
        labels: ClassVar[dict[str, str]] = {
            "pack_text": "Pack printed on invoice",
            "cases": "Cases received",
            "loose_units": "Loose bottles/cans (enter 0 if none)",
            "units_per_case": "Square units per case",
            "received_units": "Total units to add",
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
        loose_units = cleaned.get("loose_units")
        units_per_case = cleaned.get("units_per_case")
        received_units = cleaned.get("received_units")
        if cases is not None and cases < 0:
            self.add_error("cases", "Cases cannot be negative.")
        if cases is not None and loose_units is None:
            self.add_error(
                "loose_units",
                "Enter the loose bottle/can count shown on the invoice, or enter 0 if none.",
            )
        if loose_units is not None and loose_units < 0:
            self.add_error("loose_units", "Loose units cannot be negative.")
        if loose_units is not None and loose_units != loose_units.to_integral_value():
            self.add_error("loose_units", "Loose units must be a whole number.")
        if received_units is not None and received_units <= 0:
            self.add_error("received_units", "Enter at least one Square unit.")
        if received_units is not None and received_units != received_units.to_integral_value():
            self.add_error("received_units", "Inventory must be a whole number of Square units.")
        expected = calculate_received_units(
            cases=cases,
            units_per_case=units_per_case,
            loose_units=loose_units,
        )
        if expected is not None and received_units is not None and received_units != expected:
            self.add_error(
                "received_units",
                ("This must equal cases multiplied by Square units per case, plus loose units."),
            )
        unit_cost = cleaned.get("unit_cost")
        line_total = cleaned.get("line_total")
        if (
            unit_cost is not None
            and received_units is not None
            and line_total is not None
            and not line_total_matches_rounded_unit_cost(
                quantity=received_units,
                unit_cost_cents=int(unit_cost * 100),
                line_total_cents=int(line_total * 100),
            )
        ):
            self.add_error(
                "line_total",
                (
                    "This total does not match the received units and printed "
                    "unit cost, allowing normal one-cent unit-price rounding."
                ),
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


class DeliveryPricingRulesForm(forms.Form):
    """One all-products markup plus optional liquor-category overrides."""

    default_markup_percent = forms.DecimalField(
        label="All products on this invoice",
        required=False,
        min_value=0,
        max_value=1000,
        max_digits=7,
        decimal_places=3,
        widget=forms.NumberInput(
            attrs={
                "step": "0.1",
                "min": "0",
                "max": "1000",
                "inputmode": "decimal",
                "placeholder": "Example: 25",
            }
        ),
        help_text="Leave blank if you only want to change selected categories or products.",
    )

    def __init__(self, *args, category_rules=None, **kwargs):
        super().__init__(*args, **kwargs)
        rules = category_rules if isinstance(category_rules, dict) else {}
        for index, category in enumerate(LIQUOR_PRICING_CATEGORIES):
            field_name = self.category_field_name(index)
            self.fields[field_name] = forms.DecimalField(
                label=category,
                required=False,
                min_value=0,
                max_value=1000,
                max_digits=7,
                decimal_places=3,
                widget=forms.NumberInput(
                    attrs={
                        "step": "0.1",
                        "min": "0",
                        "max": "1000",
                        "inputmode": "decimal",
                        "placeholder": "Use all-products %",
                    }
                ),
            )
            if category in rules:
                self.initial[field_name] = rules[category]

    @staticmethod
    def category_field_name(index: int) -> str:
        return f"category_{index}"

    def category_rate_fields(self):
        return [
            (category, self[self.category_field_name(index)])
            for index, category in enumerate(LIQUOR_PRICING_CATEGORIES)
        ]

    def cleaned_category_rules(self) -> dict[str, str]:
        """Return JSON-safe explicit overrides, preserving an entered zero."""

        return {
            category: str(value)
            for index, category in enumerate(LIQUOR_PRICING_CATEGORIES)
            if (value := self.cleaned_data.get(self.category_field_name(index))) is not None
        }


class PricingProductRuleForm(forms.Form):
    """Owner category correction and optional one-product percentage."""

    variation_id = forms.CharField(widget=forms.HiddenInput)
    category = forms.ChoiceField(
        label="Price category",
        required=False,
        choices=(
            ("", "Use the Square category"),
            *((category, category) for category in LIQUOR_PRICING_CATEGORIES),
        ),
    )
    markup_percent = forms.DecimalField(
        label="Only this product (%)",
        required=False,
        min_value=0,
        max_value=1000,
        max_digits=7,
        decimal_places=3,
        widget=forms.NumberInput(
            attrs={
                "step": "0.1",
                "min": "0",
                "max": "1000",
                "inputmode": "decimal",
                "placeholder": "Optional",
            }
        ),
    )


PricingProductRuleFormSet = formset_factory(
    PricingProductRuleForm,
    extra=0,
    can_delete=False,
)


class SquarePriceConfirmationForm(forms.Form):
    preview_hash = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(
        label="I checked the products, current Square prices, and new prices shown above.",
    )
