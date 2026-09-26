"""
Pack configuration: turning "12/750ML" into a number of sellable units.

This is the most dangerous arithmetic in the application. Distributor invoices
are written in CASES; Square stocks SELLABLE UNITS. Get the conversion wrong and
the store's stock counts are quietly corrupted, and an ADJUSTMENT already pushed
to Square is not trivially undone.

So the rule here is: **refuse rather than guess.** Every function in this module
either returns a configuration it is confident in, or raises. There is no
fallback, no default of 1, and no "probably a case of 12". A loud failure costs
the owner thirty seconds; a wrong guess costs them a stock count they cannot
easily reconstruct.

Two distinct failure modes are kept separate, because the owner's remedy differs:

* **Unparseable** — we cannot read a pack size at all. The owner supplies one.
* **Ambiguous** — we can read the structure but it does not determine the
  sellable unit. "4/6/12OZ" is four six-packs of twelve-ounce cans: twenty-four
  cans, or four six-packs, depending on how the *store* sells it. That is a fact
  about the store's Square catalogue, not about the invoice, so it must be
  resolved from the item mapping rather than inferred here.

Pure functions, no I/O, no Django imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Volume units that may trail a pack spec. Present so "12/750ML" is recognised
# as a pack of a sized product, and a bare "750ML" is recognised as a size with
# no pack information at all.
SIZE_UNIT = r"(?:ML|L|LTR|LITER|LITRE|OZ|FLOZ|G|KG|GAL|PK|CT)"
VOLUME_OR_WEIGHT_UNIT = r"(?:ML|L|LTR|LITER|LITRE|OZ|FLOZ|G|KG|GAL)"

# "12/750ML", "24/12OZ", "6/1.75L"
TWO_PART = re.compile(
    rf"^\s*(\d{{1,4}})\s*/\s*(\d+(?:\.\d+)?)\s*({VOLUME_OR_WEIGHT_UNIT})\s*$",
    re.IGNORECASE,
)

# "4/6PK" — either four sellable six-packs or twenty-four singles.
COUNT_PACK = re.compile(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*(?:PK|CT)\s*$", re.IGNORECASE)

# "4/6/12OZ" — cases of packs of units.
THREE_PART = re.compile(
    rf"^\s*(\d{{1,3}})\s*/\s*(\d{{1,3}})\s*/\s*(\d+(?:\.\d+)?)\s*({SIZE_UNIT})\s*$",
    re.IGNORECASE,
)

# "24 LOOSE", "24 EACH", "24 SINGLES" — explicitly individual units.
LOOSE = re.compile(r"^\s*(\d{1,4})\s*(?:LOOSE|EACH|EA|SINGLES?|UNITS?)\s*$", re.IGNORECASE)

# A bare size with no pack count: "750ML". Recognised specifically so it can be
# refused with a useful message rather than falling through to "unparseable".
BARE_SIZE = re.compile(rf"^\s*(\d+(?:\.\d+)?)\s*({SIZE_UNIT})\s*$", re.IGNORECASE)

# A bare number: "12". Could be a case of 12 or 12 singles. Refused.
BARE_NUMBER = re.compile(r"^\s*(\d{1,4})\s*$")

MAX_UNITS_PER_CASE = 1000


class PackError(ValueError):
    """Base for every refusal. Carries a message written for the store owner."""


class UnparseablePack(PackError):
    """No pack size could be read."""


class AmbiguousPack(PackError):
    """
    A pack size was read, but it does not determine the sellable unit.

    Carries the candidate interpretations so the UI can offer them as a choice
    rather than making the owner work it out.
    """

    def __init__(self, message: str, *, candidates: list[int]):
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class PackConfig:
    """How many sellable units one case contains."""

    units_per_case: int
    source_text: str
    #: Set when the figure came from a stored mapping rather than the invoice
    #: text, so the UI can say where it got the number.
    from_mapping: bool = False

    def units_for(self, cases: int) -> int:
        """
        Sellable units received for a number of cases.

        Negative case counts are allowed and meaningful: a credit or a return is
        a negative delivery, and it must reduce stock by the same conversion.
        """
        return cases * self.units_per_case


def parse_pack(text: str | None) -> PackConfig:
    """
    Read a pack configuration off an invoice line.

    Raises UnparseablePack or AmbiguousPack rather than guessing. Callers are
    expected to route both to the owner.
    """
    if text is None or not text.strip():
        raise UnparseablePack(
            "No pack size on this line. Enter how many sellable units are in one case."
        )

    raw = text.strip()
    normalised = raw.upper().replace("-", "/").replace("X", "/")
    # Collapse repeated separators produced by the substitutions above.
    normalised = re.sub(r"/{2,}", "/", normalised)

    if (m := LOOSE.match(normalised)) is not None:
        return _config(int(m.group(1)), raw)

    if (m := COUNT_PACK.match(normalised)) is not None:
        outer, inner = int(m.group(1)), int(m.group(2))
        outer_config = _config(outer, raw)
        singles_config = _config(outer * inner, raw)
        candidates = sorted({outer_config.units_per_case, singles_config.units_per_case})
        if len(candidates) == 1:
            return outer_config
        raise AmbiguousPack(
            f"{raw!r} could be {outer * inner} singles or {outer} multipacks. "
            "Choose which one this product is sold as.",
            candidates=candidates,
        )

    if (m := TWO_PART.match(normalised)) is not None:
        return _config(int(m.group(1)), raw)

    if (m := THREE_PART.match(normalised)) is not None:
        outer, inner = int(m.group(1)), int(m.group(2))
        outer_config = _config(outer, raw)
        singles_config = _config(outer * inner, raw)
        candidates = sorted({outer_config.units_per_case, singles_config.units_per_case})
        if len(candidates) == 1:
            return outer_config
        # Both readings are defensible and they differ by a factor of `inner`.
        # Which is right depends on whether the store sells the six-pack or the
        # can, which only the Square catalogue knows.
        raise AmbiguousPack(
            f"{raw!r} could be {outer * inner} singles or {outer} multipacks. "
            "Choose which one this product is sold as.",
            candidates=candidates,
        )

    if (m := BARE_SIZE.match(normalised)) is not None:
        raise UnparseablePack(
            f"{raw!r} is a bottle size, not a pack size. Enter how many bottles are in one case."
        )

    if BARE_NUMBER.match(normalised) is not None:
        raise UnparseablePack(
            f"{raw!r} on its own could mean a case of {normalised.strip()} or "
            f"{normalised.strip()} singles. Enter the units per case explicitly."
        )

    raise UnparseablePack(
        f"Could not read a pack size from {raw!r}. Enter how many sellable units are in one case."
    )


def _config(units: int, source: str) -> PackConfig:
    if units <= 0:
        raise UnparseablePack(f"A case cannot contain {units} units ({source!r}).")
    if units > MAX_UNITS_PER_CASE:
        # Almost always a misread quantity that has run into the pack column.
        raise UnparseablePack(
            f"{units} units per case from {source!r} is implausible and was more likely "
            "misread. Enter the correct pack size."
        )
    return PackConfig(units_per_case=units, source_text=source)


# --------------------------------------------------------------------------
# Line-level arithmetic
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineCheck:
    """One arithmetic identity over an invoice line, in the style of reconcile.checks."""

    name: str
    expected: int
    actual: int

    @property
    def delta(self) -> int:
        return self.actual - self.expected

    @property
    def passed(self) -> bool:
        return self.delta == 0


def check_units(*, cases: int, units_per_case: int, stated_units: int) -> LineCheck:
    """Units received must be the case count times the pack size."""
    return LineCheck("units = cases x units_per_case", cases * units_per_case, stated_units)


def check_line_total(*, quantity: int, unit_cost_cents: int, stated_total_cents: int) -> LineCheck:
    """
    Extended cost must be quantity times unit cost.

    Integer cents throughout: quantity * unit_cost is exact, so any discrepancy
    is a misread rather than a rounding artefact.
    """
    return LineCheck(
        "line_total = quantity x unit_cost", quantity * unit_cost_cents, stated_total_cents
    )


def check_invoice_total(*, line_totals_cents: list[int], stated_total_cents: int) -> LineCheck:
    """
    The lines must sum to the invoice total printed on the page.

    The strongest check available on a delivery: it ties every line to a figure
    the extractor read independently, so a single mistyped quantity breaks it.
    """
    return LineCheck("sum(line_totals) = invoice_total", sum(line_totals_cents), stated_total_cents)


def is_non_stock_line(description: str) -> bool:
    """
    Lines that are a charge, not stock, and must never reach Square inventory.

    Bottle deposits are the common one: they appear as ordinary lines with a
    quantity and a cost, and pushing them creates phantom stock of a product
    that does not exist.
    """
    if not description:
        return False
    text = description.upper()
    markers = (
        "DEPOSIT",
        "BOTTLE DEP",
        "CRV",  # California Redemption Value and similar container fees
        "FUEL SURCHARGE",
        "DELIVERY CHARGE",
        "FREIGHT",
        "PALLET",
        "KEG DEP",
    )
    return any(marker in text for marker in markers)
