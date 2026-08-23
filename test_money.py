"""Financial-correctness tests for CSV rupee-to-paise parsing (issue C7).

Money parsing uses `decimal.Decimal`, never binary floats. These tests pin
down the specific failure cases that motivated the fix: sub-paise rounding
direction, rejection of negative/non-finite input, and Indian comma-grouped
number formats merchants actually paste from Tally/Excel.
"""

import pytest

from api.main import _rupees_to_paise


def test_subpaise_precision_rounds_half_up():
    warnings = []
    assert _rupees_to_paise("1234.567", "amount", 2, warnings) == 123457
    assert warnings, "sub-paise rounding must be surfaced as a warning"


def test_subpaise_precision_rounds_down_to_zero():
    warnings = []
    assert _rupees_to_paise("0.001", "amount", 2, warnings) == 0
    assert warnings, "sub-paise rounding must be surfaced as a warning"


def test_negative_amount_rejected():
    with pytest.raises(ValueError):
        _rupees_to_paise("-500", "amount", 2)


def test_infinite_amount_rejected():
    with pytest.raises(ValueError):
        _rupees_to_paise("inf", "amount", 2)


def test_nan_amount_rejected():
    with pytest.raises(ValueError):
        _rupees_to_paise("nan", "amount", 2)


def test_indian_comma_grouped_format_parses_correctly():
    assert _rupees_to_paise("1,50,000", "amount", 2) == 15000000


def test_non_numeric_string_rejected():
    with pytest.raises(ValueError):
        _rupees_to_paise("not-a-number", "amount", 2)


def test_blank_amount_is_zero():
    assert _rupees_to_paise("", "amount", 2) == 0


def test_exact_two_decimal_places_no_warning():
    warnings = []
    assert _rupees_to_paise("1234.56", "amount", 2, warnings) == 123456
    assert warnings == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
