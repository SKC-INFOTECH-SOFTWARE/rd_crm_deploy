"""Exact quantities, explicit conversion factors and source sign policies."""
from datetime import date
from decimal import Decimal, localcontext, Inexact
from sqlalchemy import select
from rd_import.codec import decimal
from . import schema as s


class StockError(ValueError):
    """Actionable validation failure, safe to show locally."""


def quantity(value, positive: bool = False) -> Decimal:
    try:
        result = decimal(value)
    except ValueError as error:
        raise StockError(str(error)) from None
    if result is None or (positive and result <= 0):
        raise StockError('A finite positive quantity is required.' if positive else 'A finite quantity is required.')
    if len(result.as_tuple().digits) > 40 or abs(result.as_tuple().exponent) > 18:
        raise StockError('Use at most 40 significant digits and 18 decimal places.')
    return result


def exact(operation):
    """Reject nonterminating scaling instead of silently rounding a recipe."""
    with localcontext() as context:
        context.prec = 100
        context.traps[Inexact] = True
        try:
            return operation()
        except Inexact:
            raise StockError('Scaling needs rounding. Choose quantities with an exact decimal ratio.') from None


def day(value) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise StockError('Use a valid YYYY-MM-DD date.') from None


def required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StockError(label + ' is required.')
    return value.strip()


def ingredient_id(value) -> int:
    try:
        result = int(str(value))
    except (ValueError, TypeError):
        raise StockError('Ingredient item ID must be an integer from the configured item list.') from None
    if result < 1:
        raise StockError('Ingredient item ID must be positive.')
    return result


def item(c, item_id: int):
    row = c.execute(select(s.items).where(s.items.c.id == item_id)).mappings().first()
    if row is None or not row['tracking'] or row['classification'] == 'non-stock':
        raise StockError(f'Item {item_id} is not a configured stock item.')
    return row


def convert(c, item_id: int, value, unit: str) -> Decimal:
    product = item(c, item_id)
    unit = required(unit, 'Unit')
    value = quantity(value)
    if unit == product['base_unit']:
        return value
    factor = c.execute(select(s.conversions.c.factor).where(
        s.conversions.c.item_id == item_id, s.conversions.c.unit == unit)).scalar()
    if factor is None:
        raise StockError(f'unsupported_unit: item {item_id}, {unit} to {product["base_unit"]}; confirm a product-specific conversion.')
    return exact(lambda: value * factor)


DIRECTIONS = {'pbill': 1, 'sale': -1, 'preturn': -1, 'salereturn': 1, 'miscpbill': 1}


def signed_quantity(endpoint: str, value, convention: str | None) -> Decimal:
    value = quantity(value)
    if convention == 'positive_magnitude':
        if value < 0:
            raise StockError('unexpected_quantity_sign')
        return value if DIRECTIONS[endpoint] == 1 else value.copy_negate()
    if convention == 'negative_return' and endpoint in ('preturn', 'salereturn'):
        if value > 0:
            raise StockError('unexpected_quantity_sign')
        return value.copy_negate() if DIRECTIONS[endpoint] == 1 else value
    raise StockError('quantity_sign_rule_unconfirmed')
