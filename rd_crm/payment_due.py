"""Manually maintained payment due dates for confirmed invoices and purchase bills."""
from datetime import date

from sqlalchemy import delete, insert, select, update

from . import db, history
from .clients import CRMError


def _valid_date(value: str) -> str:
    value = str(value or '').strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        raise CRMError('Choose a valid payment due date.') from None
    return value


def set_date(engine, client_id: int, voucher_id: int, due_date: str,
             updated_by: int | None = None) -> None:
    """Set or clear one invoice due date after confirming invoice ownership."""
    try:
        voucher = history.voucher_detail(engine, client_id, voucher_id)
    except CRMError:
        # Lazy import avoids making customer history depend on supplier services.
        from . import supplier_history
        voucher = supplier_history.voucher_detail(engine, client_id, voucher_id)
    if voucher['endpoint'] not in ('sale', 'pbill', 'miscpbill'):
        raise CRMError('Payment due dates can only be set on sales or supplier purchase invoices.')
    due_date = str(due_date or '').strip()
    if due_date:
        due_date = _valid_date(due_date)
    with db.transaction(engine) as connection:
        predicate = (
            db.payment_due_dates.c.client_id == client_id,
            db.payment_due_dates.c.voucher_id == voucher_id,
        )
        existing = connection.execute(select(db.payment_due_dates.c.voucher_id).where(
            *predicate)).scalar()
        if not due_date:
            if existing is not None:
                connection.execute(delete(db.payment_due_dates).where(*predicate))
            return
        values = dict(due_date=due_date, updated_by=updated_by, updated_at=db.now())
        if existing is None:
            connection.execute(insert(db.payment_due_dates).values(
                client_id=client_id, voucher_id=voucher_id, **values))
        else:
            connection.execute(update(db.payment_due_dates).where(
                *predicate).values(**values))


def for_client(engine, client_id: int) -> dict[int, str]:
    with engine.connect() as connection:
        rows = connection.execute(select(
            db.payment_due_dates.c.voucher_id,
            db.payment_due_dates.c.due_date,
        ).where(db.payment_due_dates.c.client_id == client_id)).mappings()
        return {row['voucher_id']: row['due_date'] for row in rows}


def earliest_for_clients(engine, client_ids) -> dict[int, str]:
    identifiers = sorted({int(value) for value in client_ids if value is not None})
    if not identifiers:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(select(
            db.payment_due_dates.c.client_id,
            db.payment_due_dates.c.due_date,
        ).where(db.payment_due_dates.c.client_id.in_(identifiers)).order_by(
            db.payment_due_dates.c.client_id,
            db.payment_due_dates.c.due_date)).mappings()
        result = {}
        for row in rows:
            result.setdefault(row['client_id'], row['due_date'])
        return result


def classify(due_date: str | None, today: date) -> str:
    if not due_date:
        return ''
    due = date.fromisoformat(due_date)
    if due < today:
        return 'Overdue'
    if due == today:
        return 'Due today'
    return 'Upcoming'
