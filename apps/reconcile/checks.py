"""
Arithmetic self-checks over extracted documents.

These are the app's real confidence signal. A vision model asked "how sure are
you?" will happily answer 0.95 about a digit it invented; a model that misreads
a 3 as an 8 cannot also make the column still add up. So instead of trusting a
self-reported score, we re-derive every total the document states and compare.

Every identity here was verified by hand against the store's own paperwork —
see docs/sample-documents.md. Nothing is asserted that has not been checked
against a real document, because a wrong identity is worse than no identity: it
fires on correct data and trains the owner to ignore the alerts.

Money is integer CENTS throughout. No floats, no Decimal, no rounding policy to
get wrong.

Pure functions, no I/O, no Django imports — so the whole module is trivially
unit-testable and can be reasoned about on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


class Severity(Enum):
    """
    How much a broken identity tells us.

    HARD means the document is internally contradictory: it cannot be a correct
    reading of a correctly-printed document, so either the extraction is wrong or
    the paperwork is. Either way a human must look.

    SOFT means the identity is expected to hold but has a legitimate reason to
    drift (a rounding display, a field the report omits).
    """

    HARD = "hard"
    SOFT = "soft"


@dataclass(frozen=True)
class CheckResult:
    name: str
    expected_cents: int
    actual_cents: int
    severity: Severity = Severity.HARD
    detail: str = ""

    @property
    def delta_cents(self) -> int:
        """Actual minus expected. Positive means the stated total is too high."""
        return self.actual_cents - self.expected_cents

    @property
    def passed(self) -> bool:
        return self.delta_cents == 0

    def __str__(self) -> str:
        status = "OK" if self.passed else f"OFF BY {fmt(self.delta_cents)}"
        return f"{self.name}: {status}"


def fmt(cents: int) -> str:
    """Render cents for a human. Negatives print with the sign before the dollar."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CategorySale:
    name: str
    quantity: int
    amount_cents: int


@dataclass(frozen=True)
class SalesReport:
    """
    The Square "SALES REPORT" thermal printout.

    `fees_cents` is stored signed exactly as printed — Square prints it as a
    negative (e.g. -$1.52) because it is deducted. Storing the sign as shown
    means the identity below reads the same way the paper does, and nobody has
    to remember whether this particular field was negated on the way in.
    """

    gross_sales_cents: int
    returns_cents: int
    discounts_cents: int
    net_sales_cents: int
    tax_cents: int
    tips_cents: int
    gift_card_sales_cents: int
    refunds_cents: int
    total_cents: int

    total_collected_cents: int
    card_cents: int
    cash_cents: int
    fees_cents: int
    net_total_cents: int

    category_sales: list[CategorySale] = field(default_factory=list)


@dataclass(frozen=True)
class DrawerSnapshot:
    """
    The Square "Current drawer" screen.

    `paid_in_out_cents` is a single signed net figure because that is all the
    screen shows. The API splits it into separate paid-in and paid-out totals,
    and those cannot be recovered from the net: $0.00 net could be nothing at
    all, or $50 in and $50 out. Anything needing the split must read the API.

    `counted_cash_cents` is None while the drawer is still open; it only exists
    once the drawer has been ended.
    """

    starting_cash_cents: int
    paid_in_out_cents: int
    cash_sales_cents: int
    cash_refunds_cents: int
    expected_in_drawer_cents: int
    counted_cash_cents: int | None = None

    @property
    def over_short_cents(self) -> int | None:
        """
        Counted minus expected. Positive is over, negative is short.

        None while the drawer is open — an unended drawer has no over/short, and
        returning 0 would read as "balanced".
        """
        if self.counted_cash_cents is None:
            return None
        return self.counted_cash_cents - self.expected_in_drawer_cents


# --------------------------------------------------------------------------
# Checks — Square sales report
# --------------------------------------------------------------------------


def check_net_sales(r: SalesReport) -> CheckResult:
    """Gross, less what was given back and discounted, is net."""
    return CheckResult(
        name="net_sales = gross_sales - returns - discounts",
        expected_cents=r.gross_sales_cents - r.returns_cents - r.discounts_cents,
        actual_cents=r.net_sales_cents,
    )


def check_total(r: SalesReport) -> CheckResult:
    """Net sales plus everything added at the register is the total."""
    return CheckResult(
        name="total = net_sales + tax + tips + gift_cards - refunds",
        expected_cents=(
            r.net_sales_cents
            + r.tax_cents
            + r.tips_cents
            + r.gift_card_sales_cents
            - r.refunds_cents
        ),
        actual_cents=r.total_cents,
    )


def check_tenders_sum_to_collected(r: SalesReport) -> CheckResult:
    """
    Card plus cash is everything collected.

    The strongest check on the page: it ties the two tender figures, one of which
    is the anchor for the entire cash reconciliation, to a third printed number.
    """
    return CheckResult(
        name="total_collected = card + cash",
        expected_cents=r.card_cents + r.cash_cents,
        actual_cents=r.total_collected_cents,
    )


def check_net_total(r: SalesReport) -> CheckResult:
    """Collected, less processing fees, is what the store nets."""
    return CheckResult(
        name="net_total = total_collected + fees",
        expected_cents=r.total_collected_cents + r.fees_cents,
        actual_cents=r.net_total_cents,
    )


def check_categories_sum_to_gross(r: SalesReport) -> CheckResult:
    """
    Category lines add to gross sales.

    SOFT: a report with categories truncated or a sale in no category would break
    this without the extraction being wrong.
    """
    return CheckResult(
        name="sum(category_sales) = gross_sales",
        expected_cents=sum(c.amount_cents for c in r.category_sales),
        actual_cents=r.gross_sales_cents,
        severity=Severity.SOFT,
        detail=f"{len(r.category_sales)} category lines",
    )


# --------------------------------------------------------------------------
# Checks — drawer screen
# --------------------------------------------------------------------------


def check_expected_in_drawer(d: DrawerSnapshot) -> CheckResult:
    """
    What Square thinks should be in the till.

    Verified against the real screen: 265.00 + 0.00 + 34.91 - 0.00 = 299.91.
    """
    return CheckResult(
        name="expected = starting_cash + paid_in_out + cash_sales - cash_refunds",
        expected_cents=(
            d.starting_cash_cents + d.paid_in_out_cents + d.cash_sales_cents - d.cash_refunds_cents
        ),
        actual_cents=d.expected_in_drawer_cents,
    )


# --------------------------------------------------------------------------
# Cross-document
# --------------------------------------------------------------------------


def check_cash_agrees_across_documents(r: SalesReport, d: DrawerSnapshot) -> CheckResult:
    """
    The join between the two Square documents.

    The Sales Report's "Cash" tender and the drawer screen's "Cash sales" are the
    same quantity reported twice, so they must be equal. This is the anchor the
    daily reconciliation hangs from: if these disagree, the two photos are of
    different days or different drawers, and nothing downstream is meaningful.
    """
    return CheckResult(
        name="sales_report.cash = drawer.cash_sales",
        expected_cents=r.cash_cents,
        actual_cents=d.cash_sales_cents,
        detail="the two Square documents must describe the same day and drawer",
    )


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------


def run_sales_report_checks(r: SalesReport) -> list[CheckResult]:
    checks = [
        check_net_sales(r),
        check_total(r),
        check_tenders_sum_to_collected(r),
        check_net_total(r),
    ]
    if r.category_sales:
        checks.append(check_categories_sum_to_gross(r))
    return checks


def run_drawer_checks(d: DrawerSnapshot) -> list[CheckResult]:
    return [check_expected_in_drawer(d)]


def run_daily_checks(r: SalesReport, d: DrawerSnapshot) -> list[CheckResult]:
    """Everything checkable from the two Square documents together."""
    return [
        *run_sales_report_checks(r),
        *run_drawer_checks(d),
        check_cash_agrees_across_documents(r, d),
    ]


def failures(results: list[CheckResult], *, include_soft: bool = False) -> list[CheckResult]:
    return [c for c in results if not c.passed and (include_soft or c.severity is Severity.HARD)]


def needs_human_review(results: list[CheckResult]) -> bool:
    """
    Any hard identity that does not hold sends the submission to a human.

    Deliberately not tolerance-based. These are identities printed on the same
    piece of paper, not measurements — they either add up or a number was read
    wrong. Cash variance tolerance is a separate question, applied later to the
    counted-versus-expected comparison, not to whether a document is self-consistent.
    """
    return bool(failures(results))
