"""Money is stored as integer cents. Never use floats for amounts."""
from decimal import Decimal, InvalidOperation


def to_cents(value) -> int:
    """Parse "100", "100.5" or "100.50" into cents. Rejects fractions of a cent and non-numbers."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Not an amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"Not an amount: {value!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"Amounts can have at most 2 decimals: {value!r}")
    return int(cents)


def to_str(cents: int) -> str:
    """Format cents for the PayPal API: 10000 -> "100.00"."""
    if not isinstance(cents, int):
        raise TypeError("cents must be an int")
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
