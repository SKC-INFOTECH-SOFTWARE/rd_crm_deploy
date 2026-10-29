import hashlib
from decimal import Decimal, InvalidOperation

import simplejson
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator


def loads(value):
    return simplejson.loads(value, use_decimal=True, allow_nan=False)


def dumps(value):
    return simplejson.dumps(value, use_decimal=True, allow_nan=False,
                            ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def decimal(value):
    if value is None or value == '':
        return None
    if isinstance(value, (float, bool)):
        raise ValueError('Binary floats and booleans are not source decimal values')
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid decimal') from None
    if not result.is_finite():
        raise ValueError('Nonfinite decimal')
    return result


class ExactDecimal(TypeDecorator):
    """Text on SQLite AND PostgreSQL: no float conversion or implicit rounding.

    Read as Decimal in Python. SQL arithmetic requires an explicit numeric cast
    on PostgreSQL; never use SQLite SUM on these columns for money.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        value = decimal(value)
        return str(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None
