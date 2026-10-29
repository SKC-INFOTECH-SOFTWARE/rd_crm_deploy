"""Presentation-independent financial rules. No stock, profit or overdue calculations."""
from collections import defaultdict
from decimal import Decimal, localcontext
from typing import Any, Iterable
from rd_import.codec import decimal, loads

MISSING_CURRENCY = 'Unspecified'
COUNTS = ('fetched', 'inserted', 'updated', 'unchanged', 'quarantined', 'failed')
WARNINGS = {
    'zero_amount_with_nonzero_qty_rate': 'Source Amount is zero despite nonzero quantity and rate. Amount and LineAmount are retained separately; no replacement is calculated.',
    'missing_identity_NRVNO': 'Journal number NRVNO is missing. The record is quarantined; no current voucher was created.',
    'ambiguous_duplicate_identity': 'Multiple source records share the proposed identifier. They were quarantined rather than merged.',
    'possible_identity_field_edit_or_reused_number': 'This may be an edited identifier or a reused voucher number. Review the source before matching.',
    'known_reused_voucher_number_auto_accepted': 'This source is configured to treat a reused voucher number on a different date as a separate record.',
    'unresolved_cross_company': 'Individual references across source companies have not been verified. Historical issues may predate confirmation of the RDIC-to-RD business succession.',
    'related_company_unresolved_reference': 'RDIC and RD are confirmed predecessor/successor entities, but their individual product/party IDs have not been mapped.',
    'unresolved_source_reference': 'A source reference has no verified master-record link.',
}


def explain_issue(code: str) -> str:
    if code in WARNINGS:
        return WARNINGS[code]
    if code.startswith('missing_identity_'):
        return 'Required source identifier is missing: ' + code.removeprefix('missing_identity_')
    if code.startswith('invalid_'):
        return 'Source field or nested structure is invalid; review the preserved source values. (' + code + ')'
    return 'Review the preserved source record. Issue: ' + code


def currency_label(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else MISSING_CURRENCY


def money(value: Decimal | None, currency: Any) -> str:
    try:
        number = decimal(value)
    except ValueError:
        return str(value) + ' [unparsed source value; ' + currency_label(currency) + ']'
    return ('Not supplied' if number is None else format(number, 'f')) + ' [' + currency_label(currency) + ']'


def exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Raise Decimal precision to the operands; the default 28 digits can round source data."""
    items = list(values)
    if not items:
        return Decimal(0)
    precision = max(item.adjusted() for item in items) - min(item.as_tuple().exponent for item in items)
    with localcontext() as context:
        context.prec = max(28, precision + len(str(len(items))) + 3)
        return sum(items, Decimal(0))


def page_totals(rows: list[dict]) -> list[dict]:
    """Only displayed rows; separate source company, endpoint and effective currency.

    Missing currency and absent amounts are not totalled, nor are uncertain/cancelled records.
    These are source amount sums, never balances or economic-event deduplication.
    """
    groups: dict[tuple, list[Decimal]] = defaultdict(list)
    excluded: dict[tuple, int] = defaultdict(int)
    for row in rows:
        key = (row['company'], row['endpoint'], currency_label(row.get('resolved_currency') or row.get('currency')))
        if row['cancellation'] in ('cancelled', 'uncertain') or row['amount'] is None or key[2] == MISSING_CURRENCY:
            excluded[key] += 1
        else:
            groups[key].append(row['amount'])
    return [dict(company=key[0], endpoint=key[1], currency=key[2],
                 included=len(groups[key]), excluded=excluded[key],
                 total=money(exact_sum(groups[key]), key[2]) if groups[key] else 'Not totalled')
            for key in sorted(groups.keys() | excluded.keys())]


def payment_review(payment: dict, references: list[dict]) -> dict:
    """Expose source unadjusted values without assuming a missing currency."""
    payload = loads(payment['payload_json'])
    currency = payment.get('currency')
    unadjusted = next((payload[key] for key in ('UnadjustedAmount', 'UnallocatedAmount') if key in payload), None)
    try:
        amount = decimal(unadjusted)
    except ValueError:
        amount = None
    return dict(payment_id=payment['id'], currency=currency_label(currency),
                source_amount=money(payment['amount'], currency),
                source_unadjusted=money(amount, currency) if amount is not None else 'Not supplied — cannot establish from these references',
                reference_count=len(references),
                note='Unspecified currency remains unresolved; no invoice matching or outstanding balance is inferred.')
