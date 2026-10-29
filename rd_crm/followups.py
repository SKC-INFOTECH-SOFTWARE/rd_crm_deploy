"""Internal tasks and separately recorded client interactions."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select, insert, update

from . import db, ignored_clients
from .clients import CRMError, required

CHANNELS = ('call', 'WhatsApp', 'email', 'meeting')
STATUSES = ('Open', 'Completed', 'Cancelled')


def timezone(engine) -> ZoneInfo:
    with engine.connect() as c:
        name = c.execute(select(db.settings.c.value).where(
            db.settings.c.key == 'business_timezone')).scalar() or 'Asia/Kolkata'
    return ZoneInfo(name)


def set_timezone(engine, name: str) -> None:
    try:
        ZoneInfo(name)
    except (KeyError, ValueError):
        raise CRMError('Choose a valid IANA timezone.') from None
    with db.transaction(engine) as c:
        c.execute(update(db.settings).where(db.settings.c.key == 'business_timezone').values(value=name))


def classify(due_date: str, due_time: str | None, now: datetime) -> str:
    """Date-only tasks turn overdue on the next calendar day."""
    day = date.fromisoformat(due_date)
    if due_time:
        due = datetime.combine(day, time.fromisoformat(due_time), tzinfo=now.tzinfo)
        if due < now:
            return 'Overdue'
    elif day < now.date():
        return 'Overdue'
    return 'Today' if day == now.date() else 'Upcoming'


def schedule(engine, client_id: int, owner: str, due_date: str, due_time: str | None,
             channel: str, purpose: str, opportunity_id: int | None = None,
             quotation_id: int | None = None, event_action: str = 'scheduled',
             event_detail: str | None = None) -> int:
    date.fromisoformat(due_date)
    if due_time:
        time.fromisoformat(due_time)
    if channel not in CHANNELS:
        raise CRMError('Choose a supported follow-up channel.')
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        if opportunity_id is not None and c.execute(select(db.opportunities.c.id).where(
                db.opportunities.c.id == opportunity_id,
                db.opportunities.c.client_id == client_id)).scalar() is None:
            raise CRMError('Opportunity does not belong to this client.')
        if quotation_id is not None and c.execute(select(db.quotes.c.id).where(
                db.quotes.c.id == quotation_id,
                db.quotes.c.client_id == client_id)).scalar() is None:
            raise CRMError('Quotation does not belong to this client.')
        stamp = db.now()
        identifier = c.execute(insert(db.followups).values(client_id=client_id,
            opportunity_id=opportunity_id, quotation_id=quotation_id,
            owner=required(owner, 'Assigned owner'), due_date=due_date, due_time=due_time,
            channel=channel, purpose=required(purpose, 'Purpose'), status='Open',
            created_at=stamp, updated_at=stamp)).inserted_primary_key[0]
        c.execute(insert(db.followup_events).values(followup_id=identifier,
            action=event_action, detail=event_detail, at=stamp))
        return identifier


def _validate_links(connection, client_id: int, opportunity_id: int | None,
                    quotation_id: int | None) -> None:
    if opportunity_id is not None and connection.execute(select(db.opportunities.c.id).where(
            db.opportunities.c.id == opportunity_id,
            db.opportunities.c.client_id == client_id)).scalar() is None:
        raise CRMError('Opportunity does not belong to this client.')
    if quotation_id is not None and connection.execute(select(db.quotes.c.id).where(
            db.quotes.c.id == quotation_id,
            db.quotes.c.client_id == client_id)).scalar() is None:
        raise CRMError('Quotation does not belong to this client.')


def _interaction_time(engine, value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(engine))
    return stamp.isoformat()


def change(engine, followup_id: int, action: str, outcome: str = '', next_action: str = '',
           due_date: str | None = None, due_time: str | None = None) -> None:
    if action not in ('complete', 'reschedule', 'cancel'):
        raise CRMError('Unsupported follow-up action.')
    if action == 'reschedule':
        if not due_date:
            raise CRMError('New due date is required.')
        date.fromisoformat(due_date)
        if due_time:
            time.fromisoformat(due_time)
    with db.transaction(engine) as c:
        row = c.execute(select(db.followups).where(db.followups.c.id == followup_id)).mappings().first()
        if row is None or row['status'] != 'Open':
            raise CRMError('Only an open follow-up can be changed.')
        values = dict(updated_at=db.now())
        if action == 'reschedule':
            values.update(due_date=due_date, due_time=due_time)
        else:
            values.update(status='Completed' if action == 'complete' else 'Cancelled',
                          outcome=outcome.strip(), next_action=next_action.strip())
        c.execute(update(db.followups).where(db.followups.c.id == followup_id).values(**values))
        c.execute(insert(db.followup_events).values(followup_id=followup_id, action=action,
            detail=next_action.strip() or outcome.strip() or None, at=db.now()))


def log_interaction(engine, client_id: int, channel: str, occurred_at: str,
                    outcome: str, next_action: str = '', opportunity_id: int | None = None,
                    quotation_id: int | None = None) -> int:
    if channel not in CHANNELS:
        raise CRMError('Choose a supported interaction channel.')
    occurred_at = _interaction_time(engine, occurred_at)
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        _validate_links(c, client_id, opportunity_id, quotation_id)
        return c.execute(insert(db.activities).values(client_id=client_id,
            opportunity_id=opportunity_id, quotation_id=quotation_id, channel=channel,
            occurred_at=occurred_at, outcome=required(outcome, 'Interaction outcome'),
            next_action=next_action.strip(), created_at=db.now())).inserted_primary_key[0]


def log_and_schedule(engine, client_id: int, channel: str, occurred_at: str,
                     outcome: str, next_action: str, owner: str,
                     due_date: str, due_time: str | None = None,
                     opportunity_id: int | None = None,
                     quotation_id: int | None = None) -> tuple[int, int]:
    """One local transaction records contact and the next task together."""
    if channel not in CHANNELS:
        raise CRMError('Choose a supported interaction channel.')
    occurred_at = _interaction_time(engine, occurred_at)
    date.fromisoformat(due_date)
    if due_time:
        time.fromisoformat(due_time)
    with db.transaction(engine) as c:
        if c.execute(select(db.clients.c.id).where(db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client not found.')
        _validate_links(c, client_id, opportunity_id, quotation_id)
        activity_id = c.execute(insert(db.activities).values(client_id=client_id,
            opportunity_id=opportunity_id, quotation_id=quotation_id, channel=channel,
            occurred_at=occurred_at, outcome=required(outcome, 'Interaction outcome'),
            next_action=next_action.strip(), created_at=db.now())).inserted_primary_key[0]
        task_id = c.execute(insert(db.followups).values(client_id=client_id,
            opportunity_id=opportunity_id, quotation_id=quotation_id,
            owner=required(owner, 'Assigned owner'), due_date=due_date, due_time=due_time,
            channel=channel, purpose=required(next_action, 'Next action'),
            status='Open', created_at=db.now(), updated_at=db.now())).inserted_primary_key[0]
        c.execute(insert(db.followup_events).values(followup_id=task_id,
            action='scheduled_after_interaction', detail=f'activity:{activity_id}', at=db.now()))
        return activity_id, task_id


def views(engine, now: datetime | None = None,
          allowed_client_ids: list[int] | None = None) -> dict[str, list[dict]]:
    zone = timezone(engine)
    current = now.astimezone(zone) if now else datetime.now(zone)
    result = {'Today': [], 'Overdue': [], 'Upcoming': []}
    with engine.connect() as c:
        query = (select(db.followups, db.clients.c.name.label('client_name'))
            .join(db.clients, db.clients.c.id == db.followups.c.client_id)
            .where(db.followups.c.status == 'Open',
                   ~ignored_clients.name_is_ignored(db.clients.c.name)))
        if allowed_client_ids is not None:
            query = query.where(db.followups.c.client_id.in_(
                sorted(set(allowed_client_ids))))
        rows = c.execute(query.order_by(
                db.followups.c.due_date, db.followups.c.due_time)).mappings()
        for row in rows:
            item = dict(row)
            result[classify(item['due_date'], item['due_time'], current)].append(item)
    return result
