import pytest

from apps.inventory.packs import AmbiguousPack, UnparseablePack, parse_pack


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
