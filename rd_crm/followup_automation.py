"""Explainable follow-up suggestions derived from confirmed client history."""
from datetime import date, timedelta
from decimal import Decimal
import hashlib

from sqlalchemy import select

from rd_import.codec import dumps
from . import db, followups, history


PURPOSE_PREFIXES = {
    'payment': 'Payment follow-up',
    'order': 'Order follow-up',
}


def _median_days(values: list[int]) -> int | None:
    ordered = sorted(value for value in values if 0 <= value <= 730)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle] + 1) // 2


def _confidence(samples: int, values: list[int]) -> str:
    if samples >= 4 and values:
        median = _median_days(values) or 0
        spread = max(abs(value - median) for value in values)
        if spread <= max(7, median // 4):
            return 'High'
    if samples >= 3:
        return 'Medium'
    return 'Low'


def _channel(value) -> str:
    supplied = str(value or '').strip().casefold()
    return next((item for item in followups.CHANNELS
                 if item.casefold() == supplied), 'call')


def _fingerprint(kind: str, evidence: dict) -> str:
    digest = hashlib.sha256(dumps(evidence).encode('utf-8')).hexdigest()[:20]
    return f'automation:{kind}:{digest}'


def _money(value: Decimal) -> str:
    return f'{value:,.2f}'


def recommendations(engine, client_ids, as_of: date | None = None,
                    positions: dict | None = None) -> dict[int, list[dict]]:
    """Suggest, but never create, payment and reorder tasks.

    Only strongly attributable, non-cancelled history from ``account_positions``
    is used. A prior approval with identical evidence and an existing open task
    of the same type both suppress a duplicate recommendation.
    """
    today = as_of or date.today()
    identifiers = sorted({int(value) for value in client_ids if value is not None})
    if not identifiers:
        return {}
    if positions is None:
        positions = history.account_positions(engine, identifiers)
    with engine.connect() as connection:
        client_rows = {row['id']: dict(row) for row in connection.execute(select(
            db.clients.c.id, db.clients.c.name, db.clients.c.alias, db.clients.c.owner,
            db.clients.c.preferred_channel).where(db.clients.c.id.in_(identifiers))).mappings()}
        task_rows = list(connection.execute(select(
            db.followups.c.client_id, db.followups.c.status, db.followups.c.purpose,
            db.followup_events.c.action, db.followup_events.c.detail).select_from(
                db.followups.outerjoin(db.followup_events,
                    db.followup_events.c.followup_id == db.followups.c.id)).where(
                db.followups.c.client_id.in_(identifiers))).mappings())

    open_kinds = {identifier: set() for identifier in identifiers}
    approved = {identifier: set() for identifier in identifiers}
    for row in task_rows:
        purpose = str(row['purpose'] or '').casefold()
        if row['status'] == 'Open':
            for kind, prefix in PURPOSE_PREFIXES.items():
                if purpose.startswith(prefix.casefold()):
                    open_kinds[row['client_id']].add(kind)
        detail = str(row['detail'] or '')
        if row['action'] == 'automation_suggestion_approved' and detail.startswith('automation:'):
            approved[row['client_id']].add(detail.split(' | ', 1)[0])

    result = {identifier: [] for identifier in identifiers}
    for client_id in identifiers:
        client = client_rows.get(client_id)
        position = positions.get(client_id)
        if not client or not position or not position['available']:
            continue
        sale_dates = [date.fromisoformat(value) for value in position.get('sale_dates', [])]
        receipt_dates = [date.fromisoformat(value) for value in position.get('receipt_dates', [])]
        owner = client.get('owner') or ''
        channel = _channel(client.get('preferred_channel'))
        client_name = client.get('alias') or client.get('name')

        if len(sale_dates) >= 2 and 'order' not in open_kinds[client_id]:
            intervals = [(current - previous).days
                         for previous, current in zip(sale_dates, sale_dates[1:])]
            cycle = _median_days([value for value in intervals if value > 0])
            if cycle is not None:
                predicted = sale_dates[-1] + timedelta(days=max(cycle, 3))
                due = max(today, predicted)
                evidence = {'sale_dates': [item.isoformat() for item in sale_dates],
                            'cycle_days': cycle}
                fingerprint = _fingerprint('order', evidence)
                if fingerprint not in approved[client_id]:
                    reason = (f'{len(sale_dates)} confirmed purchase dates; median reorder gap '
                              f'{cycle} days; last purchase {sale_dates[-1].isoformat()}; '
                              f'expected reorder window {predicted.isoformat()}.')
                    result[client_id].append(dict(kind='order', title='Order follow-up',
                        client_id=client_id, client_name=client_name, owner=owner,
                        channel=channel, due_date=due.isoformat(), predicted_date=predicted.isoformat(),
                        purpose=f'Order follow-up — {cycle}-day reorder pattern', reason=reason,
                        confidence=_confidence(len(sale_dates), intervals),
                        fingerprint=fingerprint))

        pending = [row for row in position.get('balances', [])
                   if row['pending'] >= (Decimal('100') if row['currency'] == 'INR'
                                         else Decimal('1'))]
        if pending and 'payment' not in open_kinds[client_id]:
            lags = []
            for receipt in receipt_dates:
                prior_sales = [sale for sale in sale_dates if sale < receipt]
                if not prior_sales:
                    prior_sales = [sale for sale in sale_dates if sale <= receipt]
                if prior_sales:
                    lags.append((receipt - prior_sales[-1]).days)
            cycle = _median_days(lags)
            latest_sale = sale_dates[-1] if sale_dates else None
            predicted = latest_sale + timedelta(days=cycle) if latest_sale and cycle is not None else today
            due = max(today, predicted)
            balances = {row['currency']: str(row['pending']) for row in pending}
            evidence = {'latest_sale': latest_sale.isoformat() if latest_sale else None,
                        'receipt_dates': [item.isoformat() for item in receipt_dates],
                        'pending': balances, 'cycle_days': cycle}
            fingerprint = _fingerprint('payment', evidence)
            if fingerprint not in approved[client_id]:
                pending_text = ', '.join(f'{row["currency"]} {_money(row["pending"])}'
                                         for row in pending)
                if cycle is None:
                    pattern = 'No reliable sale-to-receipt interval is available yet.'
                    purpose = 'Payment follow-up — pending balance review'
                else:
                    pattern = f'Median observed sale-to-receipt gap is {cycle} days.'
                    purpose = f'Payment follow-up — {cycle}-day collection pattern'
                reason = (f'Calculated pending payment: {pending_text}. {pattern} '
                          f'Based on {position["matched_records"]} matched accounting records.')
                result[client_id].append(dict(kind='payment', title='Payment follow-up',
                    client_id=client_id, client_name=client_name, owner=owner,
                    channel=channel, due_date=due.isoformat(), predicted_date=predicted.isoformat(),
                    purpose=purpose, reason=reason,
                    confidence=_confidence(len(lags), lags), fingerprint=fingerprint))

        result[client_id].sort(key=lambda item: (item['due_date'], item['kind']))
    return result


def event_detail(suggestion: dict) -> str:
    return f'{suggestion["fingerprint"]} | {suggestion["reason"]}'
