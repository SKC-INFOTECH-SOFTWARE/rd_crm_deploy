"""Portable SQL expressions over preserved source JSON."""
from sqlalchemy import String, case, func, or_, and_
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.elements import ColumnElement

from rd_import import db


class _JsonText(FunctionElement):
    type = String()
    inherit_cache = True


@compiles(_JsonText, 'sqlite')
def _sqlite_json_text(element, compiler, **kw):
    column, key = list(element.clauses)
    return f"json_extract({compiler.process(column, **kw)}, '$.' || {compiler.process(key, **kw)})"


@compiles(_JsonText, 'postgresql')
def _postgres_json_text(element, compiler, **kw):
    column, key = list(element.clauses)
    return (f"jsonb_extract_path_text(CAST({compiler.process(column, **kw)} AS JSONB), "
            f"{compiler.process(key, **kw)})")


class _VoucherDate(FunctionElement):
    type = String()
    inherit_cache = True


def _date_candidate(sql: str, dialect: str) -> str:
    length = 'length' if dialect == 'sqlite' else 'char_length'
    return (f"CASE WHEN {length}({sql}) = 8 THEN "
            f"substr({sql}, 1, 4) || '-' || substr({sql}, 5, 2) || '-' || substr({sql}, 7, 2) "
            f"ELSE substr({sql}, 1, 10) END")


@compiles(_VoucherDate, 'sqlite')
def _sqlite_voucher_date(element, compiler, **kw):
    sql = compiler.process(list(element.clauses)[0], **kw)
    candidate = _date_candidate(sql, 'sqlite')
    return (f"CASE WHEN strftime('%Y-%m-%d', {candidate}, '+0 days') = {candidate} "
            f"THEN {candidate} ELSE NULL END")


@compiles(_VoucherDate, 'postgresql')
def _postgres_voucher_date(element, compiler, **kw):
    sql = compiler.process(list(element.clauses)[0], **kw)
    candidate = _date_candidate(sql, 'postgresql')
    # Nested CASE expressions prevent the date cast from seeing invalid strings.
    return (f"CASE WHEN {candidate} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' THEN "
            f"CASE WHEN pg_input_is_valid({candidate}, 'date') THEN "
            f"CASE WHEN to_char(CAST({candidate} AS date), 'YYYY-MM-DD') = {candidate} "
            f"THEN {candidate} ELSE NULL END ELSE NULL END ELSE NULL END")


def source_field(column: ColumnElement, key: str) -> ColumnElement:
    return _JsonText(column, key)


def source_text(column: ColumnElement, key: str) -> ColumnElement:
    return source_field(column, key)


def normalized_name(column: ColumnElement) -> ColumnElement:
    return func.lower(func.trim(func.coalesce(column, '')))


def voucher_date() -> ColumnElement:
    """Accept observed ISO and journal YYYYMMDD; invalid dates become null."""
    return _VoucherDate(db.vouchers.c.date)


def cancellation() -> ColumnElement:
    """Never interpret an unfamiliar source cancellation value as active."""
    payload = db.vouchers.c.payload_json
    flags = [normalized_name(source_text(payload, key)) for key in
             ('CancelTag', 'IsCancelled', 'IsCanceled', 'Cancelled', 'Canceled', 'IsDeleted', 'Deleted')]
    true_values = ('1', 'true', 'yes', 'y', 'cancelled', 'canceled', 'deleted')
    false_values = ('0', 'false', 'no', 'n', 'active')
    status = normalized_name(source_text(payload, 'Status'))
    action = normalized_name(source_text(payload, 'ACTIONNAME'))
    positive = or_(*(flag.in_(true_values) for flag in flags),
                   status.in_(('cancelled', 'canceled', 'deleted')),
                   action.in_(('cancel', 'cancelled', 'canceled', 'delete', 'deleted')))
    unknown = or_(*(and_(flag != '', ~flag.in_(true_values + false_values)) for flag in flags))
    negative = or_(*(flag.in_(false_values) for flag in flags))
    return case((positive, 'cancelled'), (unknown, 'uncertain'),
                (negative, 'not_cancelled'), else_='not_reported')
