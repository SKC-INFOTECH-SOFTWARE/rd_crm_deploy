"""Per-client access assignments for internal and external sales accounts."""
import json

from sqlalchemy import delete, insert, select

from . import db
from .clients import CRMError


SCOPED_ROLES = frozenset({'Sales', 'External Sales'})


def is_scoped_user(user) -> bool:
    return bool(user and SCOPED_ROLES.intersection(user.get('roles', ())))


def client_ids(engine, user_id: int) -> list[int]:
    with engine.connect() as connection:
        return list(connection.execute(select(db.client_assignments.c.client_id).where(
            db.client_assignments.c.user_id == user_id)).scalars())


def can_access(engine, user, client_id: int) -> bool:
    if not is_scoped_user(user):
        return True
    with engine.connect() as connection:
        return connection.execute(select(db.client_assignments.c.client_id).where(
            db.client_assignments.c.client_id == client_id,
            db.client_assignments.c.user_id == user['id'])).scalar() is not None


def sales_users(engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(select(db.users.c.id, db.users.c.username,
            db.users.c.display_name, db.roles.c.name.label('role_name')).select_from(
                db.users.join(db.user_roles,
                    db.users.c.id == db.user_roles.c.user_id).join(
                        db.roles, db.user_roles.c.role_id == db.roles.c.id)).where(
                            db.users.c.active == 1,
                            db.roles.c.name.in_(tuple(SCOPED_ROLES))).order_by(
                                db.users.c.display_name)).mappings()
        return [dict(row) for row in rows]


def details(engine, client_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(select(db.client_assignments.c.user_id,
            db.client_assignments.c.assigned_at, db.users.c.username,
            db.users.c.display_name, db.roles.c.name.label('role_name')).select_from(
                db.client_assignments.join(db.users,
                    db.client_assignments.c.user_id == db.users.c.id).join(
                        db.user_roles, db.users.c.id == db.user_roles.c.user_id).join(
                        db.roles, db.user_roles.c.role_id == db.roles.c.id)).where(
                                db.client_assignments.c.client_id == client_id,
                                db.roles.c.name.in_(tuple(SCOPED_ROLES))).order_by(
                                    db.roles.c.name.desc(),
                                    db.users.c.display_name)).mappings()
        return [dict(row) for row in rows]


def detail(engine, client_id: int):
    """Compatibility helper for callers that need the first assignment."""
    rows = details(engine, client_id)
    return rows[0] if rows else None


def assign_many(engine, client_id: int, user_ids: list[int],
                actor_user_id: int | None) -> None:
    selected = sorted(set(int(user_id) for user_id in user_ids))
    with db.transaction(engine) as connection:
        if connection.execute(select(db.clients.c.id).where(
                db.clients.c.id == client_id)).scalar() is None:
            raise CRMError('Client was not found.')
        targets = []
        if selected:
            rows = connection.execute(select(db.users.c.id,
                db.users.c.display_name).select_from(db.users.join(
                    db.user_roles, db.users.c.id == db.user_roles.c.user_id).join(
                        db.roles, db.user_roles.c.role_id == db.roles.c.id)).where(
                            db.users.c.id.in_(selected), db.users.c.active == 1,
                            db.roles.c.name.in_(tuple(SCOPED_ROLES)))).mappings()
            targets = list({row['id']: dict(row) for row in rows}.values())
            if {target['id'] for target in targets} != set(selected):
                raise CRMError('Choose only active Sales or External Sales users.')
        connection.execute(delete(db.client_assignments).where(
            db.client_assignments.c.client_id == client_id))
        stamp = db.now()
        for target in targets:
            connection.execute(insert(db.client_assignments).values(
                client_id=client_id, user_id=target['id'],
                assigned_by=actor_user_id, assigned_at=stamp))
        connection.execute(insert(db.auth_events).values(
            actor_user_id=actor_user_id,
            target_user_id=selected[0] if len(selected) == 1 else None,
            action='client_assignments_updated',
            detail_json=json.dumps({'client_id': client_id, 'user_ids': selected},
                                   sort_keys=True, separators=(',', ':')),
            at=stamp))


def assign(engine, client_id: int, user_id: int | None,
           actor_user_id: int | None) -> None:
    assign_many(engine, client_id, [user_id] if user_id is not None else [],
                actor_user_id)


def assign_to_self(engine, client_id: int, user) -> None:
    if is_scoped_user(user):
        current = [row['user_id'] for row in details(engine, client_id)]
        assign_many(engine, client_id, current + [user['id']], user['id'])
