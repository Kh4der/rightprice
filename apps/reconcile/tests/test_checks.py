"""
The arithmetic checks, tested against a fixed synthetic paperwork example.

The numbers in `real_sales_report()` and `real_drawer()` are transcribed from
the synthetic fixtures in tests/fixtures/documents/ and written up in
docs/sample-documents.md. They are the arithmetic ground truth for this module:
if a change makes these fail, the change is wrong, not the example.

The corruption tests matter as much as the happy path. An identity that passes
on good data but also passes on a transposed digit is worse than useless — it
produces a green tick over a wrong number.
"""

import pytest

from apps.reconcile.checks import (
    CategorySale,
    DrawerSnapshot,
    SalesReport,
    Severity,
    check_cash_agrees_across_documents,
    check_expected_in_drawer,
    failures,
    fmt,
    needs_human_review,
    run_daily_checks,
    run_sales_report_checks,
)


def real_sales_report(**overrides) -> SalesReport:
    """Synthetic Square SALES REPORT example dated 2026-09-26."""
    values = {
        "gross_sales_cents": 10_441,
        "returns_cents": 0,
        "discounts_cents": 0,
        "net_sales_cents": 10_441,
        "tax_cents": 783,
        "tips_cents": 0,
        "gift_card_sales_cents": 0,
        "refunds_cents": 0,
        "total_cents": 11_224,
        "total_collected_cents": 11_224,
        "card_cents": 7_733,
        "cash_cents": 3_491,
        # Printed as -$1.52; stored with the sign exactly as shown.
        "fees_cents": -152,
        "net_total_cents": 11_072,
        "category_sales": [
            CategorySale("50 ML MINI", 2, 598),
            CategorySale("LIQUOR", 5, 8_145),
            CategorySale("SODA", 1, 399),
            CategorySale("Vodka", 1, 1_299),
        ],
    }
    values.update(overrides)
    return SalesReport(**values)


def real_drawer(**overrides) -> DrawerSnapshot:
    """Square "Current drawer" screen, same day, drawer still open."""
    values = {
        "starting_cash_cents": 26_500,
        "paid_in_out_cents": 0,
        "cash_sales_cents": 3_491,
        "cash_refunds_cents": 0,
        "expected_in_drawer_cents": 29_991,
        "counted_cash_cents": None,
    }
    values.update(overrides)
    return DrawerSnapshot(**values)


# --------------------------------------------------------------------------
# The real documents must pass everything
# --------------------------------------------------------------------------


def test_the_real_documents_are_internally_consistent():
    results = run_daily_checks(real_sales_report(), real_drawer())

    assert failures(results, include_soft=True) == [], "\n".join(
        f"{c.name}: expected {fmt(c.expected_cents)}, got {fmt(c.actual_cents)}"
        for c in failures(results, include_soft=True)
    )
    assert not needs_human_review(results)


def test_every_check_actually_ran():
    """Guards against a check silently dropping out of the runner."""
    names = {c.name for c in run_daily_checks(real_sales_report(), real_drawer())}
    assert names == {
        "net_sales = gross_sales - returns - discounts",
        "total = net_sales + tax + tips + gift_cards - refunds",
        "total_collected = card + cash",
        "net_total = total_collected + fees",
        "sum(category_sales) = gross_sales",
        "expected = starting_cash + paid_in_out + cash_sales - cash_refunds",
        "sales_report.cash = drawer.cash_sales",
    }


def test_category_check_is_skipped_when_no_categories_were_read():
    """A report whose category block was cut off should not fail the check."""
    results = run_sales_report_checks(real_sales_report(category_sales=[]))
    assert all("category" not in c.name for c in results)


# --------------------------------------------------------------------------
# Corrupted readings must be caught
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field_name", "corrupt_value", "expected_broken_check"),
    [
        # 7.83 read as 7.88 — a plausible thermal-print misread.
        ("tax_cents", 788, "total = net_sales + tax + tips + gift_cards - refunds"),
        # 34.91 read as 84.91: a 3 mistaken for an 8, the classic.
        ("cash_cents", 8_491, "total_collected = card + cash"),
        # 77.33 transposed to 73.37.
        ("card_cents", 7_337, "total_collected = card + cash"),
        # Gross misread; breaks both the net-sales and category identities.
        ("gross_sales_cents", 10_541, "net_sales = gross_sales - returns - discounts"),
        # Fee sign dropped — stored positive instead of negative.
        ("fees_cents", 152, "net_total = total_collected + fees"),
    ],
)
def test_a_misread_figure_breaks_an_identity(field_name, corrupt_value, expected_broken_check):
    results = run_sales_report_checks(real_sales_report(**{field_name: corrupt_value}))
    broken = {c.name for c in failures(results, include_soft=True)}

    assert expected_broken_check in broken, (
        f"corrupting {field_name} to {corrupt_value} was not caught by any identity"
    )


def test_a_misread_cash_figure_is_caught_even_though_it_is_self_consistent():
    """
    The subtle case.

    If the drawer screen's cash sales are misread but its own total is adjusted
    to match, the drawer document is internally consistent and every
    single-document check passes. Only the cross-document comparison against the
    Sales Report catches it — which is why that check exists.
    """
    drawer = real_drawer(cash_sales_cents=8_491, expected_in_drawer_cents=34_991)

    assert check_expected_in_drawer(drawer).passed, "drawer is self-consistent, as set up"

    cross = check_cash_agrees_across_documents(real_sales_report(), drawer)
    assert not cross.passed
    assert cross.delta_cents == 5_000  # 84.91 vs 34.91


def test_photos_of_two_different_days_are_rejected():
    """Mismatched documents must not silently reconcile."""
    other_day_drawer = real_drawer(cash_sales_cents=12_034, expected_in_drawer_cents=38_534)
    results = run_daily_checks(real_sales_report(), other_day_drawer)

    assert needs_human_review(results)
    assert "sales_report.cash = drawer.cash_sales" in {c.name for c in failures(results)}


# --------------------------------------------------------------------------
# Drawer specifics
# --------------------------------------------------------------------------


def test_expected_in_drawer_matches_the_real_screen():
    assert check_expected_in_drawer(real_drawer()).passed


def test_an_open_drawer_has_no_over_short():
    """
    An unended drawer has not been counted. Returning 0 would read as "balanced"
    and let a day be approved before the cash was ever counted.
    """
    assert real_drawer().over_short_cents is None


@pytest.mark.parametrize(
    ("counted", "expected_over_short"),
    [
        (29_991, 0),  # balanced
        (29_891, -100),  # a dollar short
        (30_091, 100),  # a dollar over
        (24_991, -5_000),  # fifty dollars short
    ],
)
def test_over_short_sign_convention(counted, expected_over_short):
    """Positive is over, negative is short. Getting this backwards inverts every report."""
    drawer = real_drawer(counted_cash_cents=counted)
    assert drawer.over_short_cents == expected_over_short


def test_paid_in_out_is_a_net_figure():
    """
    The screen shows one net number, so equal pay-ins and pay-outs are
    indistinguishable from no activity. The identity must still hold.
    """
    assert check_expected_in_drawer(real_drawer(paid_in_out_cents=0)).passed

    # $20 net paid in raises expected cash by exactly $20.
    assert check_expected_in_drawer(
        real_drawer(paid_in_out_cents=2_000, expected_in_drawer_cents=31_991)
    ).passed

    # $20 net paid out lowers it by $20.
    assert check_expected_in_drawer(
        real_drawer(paid_in_out_cents=-2_000, expected_in_drawer_cents=27_991)
    ).passed


# --------------------------------------------------------------------------
# Severity and formatting
# --------------------------------------------------------------------------


def test_a_soft_failure_alone_does_not_demand_human_review():
    """
    A truncated category block is a bad photo, not a bad number. It should not
    block the day when every hard identity holds.
    """
    results = run_sales_report_checks(
        real_sales_report(category_sales=[CategorySale("LIQUOR", 5, 8_145)])
    )
    soft = failures(results, include_soft=True)

    assert len(soft) == 1
    assert soft[0].severity is Severity.SOFT
    assert not needs_human_review(results)


@pytest.mark.parametrize(
    ("cents", "text"),
    [(0, "$0.00"), (5, "$0.05"), (152, "$1.52"), (-152, "-$1.52"), (29_991, "$299.91")],
)
def test_money_formatting(cents, text):
    assert fmt(cents) == text


def test_delta_sign_points_at_the_stated_total():
    """Positive delta means the document's printed total is higher than it should be."""
    result = check_expected_in_drawer(real_drawer(expected_in_drawer_cents=30_091))
    assert result.delta_cents == 100
