from decimal import Decimal

import pytest

from apps.inventory.packs import (
    AmbiguousPack,
    UnparseablePack,
    calculate_received_units,
    is_unreceived_line,
    line_total_matches_rounded_unit_cost,
    parse_pack,
)


@pytest.mark.parametrize(
    ("text", "units"),
    [
        ("12/750ML", 12),
        ("24 x 12OZ", 24),
        ("6-1.75L", 6),
        ("24 EACH", 24),
    ],
)
def test_unambiguous_pack_sizes(text, units):
    assert parse_pack(text).units_per_case == units


@pytest.mark.parametrize("text", ["4/6/12OZ", "4/6PK", "12", "750ML", ""])
def test_ambiguous_or_incomplete_pack_sizes_are_never_guessed(text):
    with pytest.raises((AmbiguousPack, UnparseablePack)):
        parse_pack(text)


def test_case_quantity_includes_separately_printed_loose_bottles():
    assert calculate_received_units(
        cases=Decimal("1"), units_per_case=12, loose_units=Decimal("2")
    ) == Decimal("14")
    assert calculate_received_units(cases=Decimal("2"), units_per_case=6, loose_units=None) is None


@pytest.mark.parametrize(
    ("description", "cases", "loose", "received", "total", "expected"),
    [
        ("Sample Rum — 1 CASE BACKORDERED", 0, 0, None, 0, True),
        ("Sample Vodka", 0, 0, 0, 0, True),
        ("Sample Vodka", 0, 2, 2, 0, False),
        ("Sample Rum BACKORDERED", 1, 0, 12, 12_000, False),
        ("Sample Gin", 0, None, None, 0, False),
    ],
)
def test_unreceived_lines_require_backorder_or_explicit_zero_received(
    description, cases, loose, received, total, expected
):
    assert (
        is_unreceived_line(
            description,
            cases=cases,
            loose_units=loose,
            received_units=received,
            line_total_cents=total,
        )
        is expected
    )


def test_printed_unit_cost_allows_only_normal_per_bottle_cent_rounding():
    # The exact average is 170.833... cents, which appears as $1.71 on the
    # invoice while the independently printed extension remains $123.00.
    assert line_total_matches_rounded_unit_cost(
        quantity=72,
        unit_cost_cents=171,
        line_total_cents=12_300,
    )
    assert not line_total_matches_rounded_unit_cost(
        quantity=72,
        unit_cost_cents=171,
        line_total_cents=12_000,
    )
