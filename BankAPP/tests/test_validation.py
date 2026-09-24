from __future__ import annotations

from decimal import Decimal

import pytest

from bankapp.errors import ValidationError
from bankapp.services import (
    normalize_account_no,
    parse_amount,
    validate_email,
    validate_name,
    validate_phone,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100", Decimal("100.00")),
        (" 1,250.5 ", Decimal("1250.50")),
        ("0.01", Decimal("0.01")),
        ("1000000", Decimal("1000000.00")),
    ],
)
def test_parse_amount_valid(text: str, expected: Decimal) -> None:
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "required"),
        ("abc", "not a valid amount"),
        ("NaN", "not a valid amount"),
        ("Infinity", "not a valid amount"),
        ("0", "greater than zero"),
        ("-5", "greater than zero"),
        ("10.001", "2 decimal places"),
        ("1000000.01", "maximum"),
    ],
)
def test_parse_amount_invalid(text: str, fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment) as info:
        parse_amount(text)
    assert info.value.field == "amount"


def test_normalize_account_no() -> None:
    assert normalize_account_no(" acc100001 ") == "ACC100001"
    with pytest.raises(ValidationError, match="required"):
        normalize_account_no("  ")
    with pytest.raises(ValidationError, match="not a valid account number"):
        normalize_account_no("ACC12")


def test_validate_customer_fields() -> None:
    assert validate_name("  Jane   Doe ") == "Jane Doe"
    assert validate_email("Jane.Doe@Example.com") == "jane.doe@example.com"
    assert validate_phone("+1 555-010-1234") == "+1 555-010-1234"
    for fn, bad, field in [
        (validate_name, "J", "full_name"),
        (validate_name, "J4ne", "full_name"),
        (validate_email, "jane@", "email"),
        (validate_phone, "12ab", "phone"),
    ]:
        with pytest.raises(ValidationError) as info:
            fn(bad)
        assert info.value.field == field
