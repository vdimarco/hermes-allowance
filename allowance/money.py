"""Money helpers. All amounts are Decimal with 2 places, never float."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


def money(value) -> Decimal:
    """Parse a number or string into a 2-place Decimal. Raises ValueError on bad input."""
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            d = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError) as exc:
            raise ValueError(f"not a money amount: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value) -> str:
    return f"{money(value):.2f}"
