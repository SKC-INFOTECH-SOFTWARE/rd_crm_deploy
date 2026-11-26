"""Shared presentation helpers for Indian-style number grouping.

These helpers are display-only. Stored values and calculations continue to use
``Decimal`` so adding commas cannot affect accounting logic or form inputs.
"""
from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal_text(value: Any, decimal_places: int | None) -> str | None:
    if value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if decimal_places is None:
        return format(number, 'f')
    places = max(0, int(decimal_places))
    return f'{number:.{places}f}'


def indian_number(value: Any, decimal_places: int | None = None) -> str:
    """Group the integer part as crores/lakhs while preserving precision.

    ``decimal_places=None`` keeps all supplied decimal places. Passing a number
    rounds only the displayed value to that many places.
    """
    rendered = _decimal_text(value, decimal_places)
    if rendered is None:
        return ''
    if not rendered or rendered[0] not in '+-0123456789':
        return rendered

    sign = ''
    if rendered[0] in '+-':
        sign, rendered = rendered[0], rendered[1:]
    whole, separator, fraction = rendered.partition('.')
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ','.join((*groups, tail))
    return sign + whole + (separator + fraction if separator else '')


def indian_amount(value: Any) -> str:
    """Render a monetary value with Indian grouping and two decimals."""
    return indian_number(value, 2)


def indian_signed_amount(value: Any) -> str:
    """Render an adjustment amount with an explicit plus or minus sign."""
    rendered = indian_amount(value)
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return rendered
    return rendered if rendered.startswith('-') else '+' + rendered
