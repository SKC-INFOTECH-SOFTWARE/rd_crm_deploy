"""Additive CRM schema, separate from imported accounting and stock tables."""
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (MetaData, Table, Column as C, Integer as I, Text as T,
                        ForeignKey as FK, UniqueConstraint as U, Index, inspect,
                        select, insert)

from rd_import.db import sync_lock
from rd_import import db as source_db
from rd_stock.database import local_engine

metadata = MetaData()


def table(name, *columns):
    return Table('crm_' + name, metadata, *columns)


versions = table('schema_versions', C('version', I, primary_key=True), C('applied_at', T, nullable=False))
clients = table('clients', C('id', I, primary_key=True), C('name', T, nullable=False),
                C('alias', T), C('country', T), C('location', T), C('owner', T),
                C('products_of_interest', T, nullable=False), C('notes', T),
                C('state', T), C('city', T), C('address', T), C('website', T),
                C('lead_source', T), C('tags', T), C('preferred_channel', T),
                C('commercial_preferences', T), C('reference_product', T),
                C('reference_purchase_date', T), C('reference_purchase_quantity', T),
                C('reference_purchase_unit', T), C('reference_purchase_source', T),
                C('created_at', T, nullable=False), C('updated_at', T, nullable=False))
contacts = table('contacts', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
                 C('name', T, nullable=False), C('role', T), C('phone', T), C('whatsapp', T), C('email', T),
                 C('is_primary', I, nullable=False, server_default='0'))
party_links = table('party_links', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
                    C('company', T, nullable=False), C('party_id', T, nullable=False),
                    C('confirmed_at', T, nullable=False), C('basis', T, nullable=False),
                    U('company', 'party_id'))
voucher_links = table('voucher_links', C('voucher_id', FK(source_db.vouchers.c.id), primary_key=True),
                      C('client_id', FK(clients.c.id), nullable=False),
                      C('source_hash', T, nullable=False), C('basis', T, nullable=False),
                      C('confirmed_at', T, nullable=False))
voucher_link_events = table('voucher_link_events', C('id', I, primary_key=True),
                             C('voucher_id', I, nullable=False), C('client_id', I, nullable=False),
                             C('source_hash', T, nullable=False), C('basis', T, nullable=False),
                             C('action', T, nullable=False), C('at', T, nullable=False))
opportunities = table('opportunities', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
                      C('kind', T, nullable=False), C('title', T, nullable=False), C('status', T, nullable=False),
                      C('notes', T), C('created_at', T, nullable=False), C('updated_at', T, nullable=False))
activities = table('activities', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
                   C('opportunity_id', FK(opportunities.c.id)), C('quotation_id', I),
                   C('channel', T, nullable=False), C('occurred_at', T, nullable=False),
                   C('outcome', T, nullable=False), C('next_action', T), C('created_at', T, nullable=False))
followups = table('followups', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
                  C('opportunity_id', FK(opportunities.c.id)), C('quotation_id', I),
                  C('owner', T, nullable=False), C('due_date', T, nullable=False), C('due_time', T),
                  C('channel', T, nullable=False), C('purpose', T, nullable=False),
                  C('status', T, nullable=False), C('outcome', T), C('next_action', T),
                  C('created_at', T, nullable=False), C('updated_at', T, nullable=False))
followup_events = table('followup_events', C('id', I, primary_key=True),
                        C('followup_id', FK(followups.c.id), nullable=False), C('action', T, nullable=False),
                        C('detail', T), C('at', T, nullable=False))
quotes = table('quotations', C('id', I, primary_key=True), C('client_id', FK(clients.c.id), nullable=False),
               C('opportunity_id', FK(opportunities.c.id)), C('kind', T, nullable=False),
               C('created_at', T, nullable=False))
quote_versions = table('quotation_versions', C('id', I, primary_key=True),
                       C('quote_id', FK(quotes.c.id), nullable=False), C('revision', I, nullable=False),
                       C('status', T, nullable=False), C('snapshot_json', T, nullable=False),
                       C('created_at', T, nullable=False), C('updated_at', T, nullable=False),
                       U('quote_id', 'revision'))
quote_events = table('quotation_events', C('id', I, primary_key=True),
                     C('version_id', FK(quote_versions.c.id), nullable=False), C('action', T, nullable=False),
                     C('detail', T), C('at', T, nullable=False))
cost_overrides = table('cost_overrides', C('id', I, primary_key=True), C('item_id', I, nullable=False),
                       C('amount', T, nullable=False), C('unit', T, nullable=False), C('currency', T, nullable=False),
                       C('effective_date', T, nullable=False), C('reason', T, nullable=False),
                       C('created_at', T, nullable=False))
currency_defaults = table('currency_defaults', C('company', T, primary_key=True),
                          C('endpoint', T, primary_key=True), C('currency', T, nullable=False),
                          C('basis', T, nullable=False), C('confirmed_at', T, nullable=False))
currency_resolutions = table('currency_resolutions', C('voucher_id', I, primary_key=True),
                             C('source_hash', T, nullable=False), C('source_currency', T),
                             C('resolved_currency', T), C('provenance', T, nullable=False),
                             C('refreshed_at', T, nullable=False))
settings = table('settings', C('key', T, primary_key=True), C('value', T, nullable=False))
users = table('users', C('id', I, primary_key=True), C('username', T, nullable=False, unique=True),
              C('display_name', T, nullable=False), C('password_hash', T, nullable=False),
              C('active', I, nullable=False), C('session_version', I, nullable=False),
              C('created_at', T, nullable=False), C('updated_at', T, nullable=False))
roles = table('roles', C('id', I, primary_key=True), C('name', T, nullable=False, unique=True),
              C('description', T, nullable=False))
role_permissions = table('role_permissions', C('role_id', FK(roles.c.id, ondelete='CASCADE'), primary_key=True),
                         C('permission', T, primary_key=True))
user_roles = table('user_roles', C('user_id', FK(users.c.id, ondelete='CASCADE'), primary_key=True),
                   C('role_id', FK(roles.c.id, ondelete='CASCADE'), primary_key=True))
client_assignments = table('client_assignments',
    C('client_id', FK(clients.c.id, ondelete='CASCADE'), primary_key=True),
    C('user_id', FK(users.c.id, ondelete='CASCADE'), primary_key=True),
    C('assigned_by', FK(users.c.id)), C('assigned_at', T, nullable=False))
payment_due_dates = table('payment_due_dates',
    C('client_id', FK(clients.c.id, ondelete='CASCADE'), primary_key=True),
    C('voucher_id', I, primary_key=True), C('due_date', T, nullable=False),
    C('updated_by', FK(users.c.id)), C('updated_at', T, nullable=False))
auth_events = table('auth_events', C('id', I, primary_key=True), C('actor_user_id', FK(users.c.id)),
                    C('target_user_id', FK(users.c.id)), C('action', T, nullable=False),
                    C('detail_json', T, nullable=False), C('at', T, nullable=False))
login_attempts = table('login_attempts', C('id', I, primary_key=True), C('username', T, nullable=False),
                       C('remote_fingerprint', T, nullable=False), C('success', I, nullable=False),
                       C('occurred_at', T, nullable=False))
login_captcha_challenges = table('login_captcha_challenges',
    C('id', T, primary_key=True), C('answer_hash', T, nullable=False),
    C('remote_fingerprint', T, nullable=False), C('created_at', T, nullable=False))

Index('crm_followups_due', followups.c.status, followups.c.due_date)
Index('crm_activities_client', activities.c.client_id, activities.c.occurred_at)
Index('crm_links_client', party_links.c.client_id)
Index('crm_voucher_links_client', voucher_links.c.client_id)
Index('crm_login_attempt_scope', login_attempts.c.username, login_attempts.c.remote_fingerprint,
      login_attempts.c.occurred_at)
Index('crm_login_captcha_created', login_captcha_challenges.c.created_at)
Index('crm_auth_events_at', auth_events.c.at)
Index('crm_client_assignments_user', client_assignments.c.user_id)
Index('crm_payment_due_dates_due', payment_due_dates.c.due_date)


PERMISSIONS = (
    'dashboard.view', 'clients.view', 'clients.identity_view', 'clients.edit',
    'clients.assign',
    'orders.view', 'financial.view', 'financial.export', 'followups.manage',
    'quotations.view', 'quotations.edit', 'quotations.price_edit',
    'costs.view', 'recipes.manage', 'stock.view', 'sync.run', 'users.manage',
    'data_review.manage',
)

DEFAULT_ROLES = {
    'Admin/Owner': set(PERMISSIONS),
    'Sales': {'dashboard.view', 'clients.view', 'clients.identity_view', 'clients.edit',
              'orders.view', 'financial.view',
              'followups.manage', 'quotations.view', 'quotations.edit',
              'quotations.price_edit'},
    'External Sales': {'dashboard.view', 'clients.view', 'clients.identity_view',
                       'clients.edit', 'orders.view', 'financial.view',
                       'followups.manage'},
    'Management': {'dashboard.view', 'clients.view', 'clients.identity_view',
                   'clients.edit', 'clients.assign', 'orders.view', 'financial.view',
                   'financial.export', 'followups.manage', 'quotations.view',
                   'quotations.edit', 'quotations.price_edit', 'costs.view',
                   'stock.view'},
    'Accounts': {'dashboard.view', 'clients.view', 'clients.identity_view',
                 'orders.view', 'financial.view', 'financial.export', 'quotations.view'},
    'Operations': {'dashboard.view', 'clients.view', 'clients.identity_view',
                   'clients.edit', 'stock.view', 'quotations.view'},
    'Read-only': {'dashboard.view', 'clients.view', 'quotations.view'},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ready(engine) -> bool:
    if not inspect(engine).has_table(versions.name):
        return False
    with engine.connect() as c:
        return c.execute(select(versions.c.version).where(versions.c.version == 11)).scalar() == 11


def _seed_roles(connection) -> None:
    for name, permissions in DEFAULT_ROLES.items():
        role_id = connection.execute(select(roles.c.id).where(roles.c.name == name)).scalar()
        if role_id is None:
            role_id = connection.execute(insert(roles).values(
                name=name, description=f'Built-in {name} role')).inserted_primary_key[0]
        existing = set(connection.execute(select(role_permissions.c.permission).where(
            role_permissions.c.role_id == role_id)).scalars())
        for permission in sorted(permissions - existing):
            connection.execute(insert(role_permissions).values(
                role_id=role_id, permission=permission))


def migrate(engine) -> None:
    with sync_lock(engine), engine.begin() as c:
        if not inspect(c).has_table('rd_parties'):
            raise ValueError('Initialize the importer database first.')
        party_links_existed = inspect(c).has_table(party_links.name)
        metadata.create_all(c)
        if c.execute(select(versions.c.version).where(versions.c.version == 1)).scalar() is None:
            c.execute(insert(versions).values(version=1, applied_at=now()))
        if c.execute(select(settings.c.value).where(settings.c.key == 'business_timezone')).scalar() is None:
            c.execute(insert(settings).values(key='business_timezone', value='Asia/Kolkata'))
        client_columns = {column['name'] for column in inspect(c).get_columns(clients.name)}
        for name in ('state', 'city', 'address', 'website', 'lead_source', 'tags',
                     'preferred_channel', 'commercial_preferences', 'reference_product',
                     'reference_purchase_date', 'reference_purchase_quantity',
                     'reference_purchase_unit', 'reference_purchase_source'):
            if name not in client_columns:
                c.exec_driver_sql(f'ALTER TABLE {clients.name} ADD COLUMN {name} TEXT')
        contact_columns = {column['name'] for column in inspect(c).get_columns(contacts.name)}
        if 'is_primary' not in contact_columns:
            c.exec_driver_sql(f'ALTER TABLE {contacts.name} ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0')
            c.exec_driver_sql('UPDATE crm_contacts SET is_primary = 1 WHERE id IN '
                '(SELECT MIN(id) FROM crm_contacts GROUP BY client_id)')
        if c.execute(select(versions.c.version).where(versions.c.version == 2)).scalar() is None:
            c.execute(insert(versions).values(version=2, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 3)).scalar() is None:
            # One client may legitimately have several legacy party masters in a company.
            # The source identity itself remains unique and every link is explicit.
            constraints = inspect(c).get_unique_constraints(party_links.name)
            legacy = party_links_existed and any(
                set(item.get('column_names') or ()) == {'client_id', 'company', 'party_id'}
                for item in constraints)
            if legacy:
                if c.dialect.name != 'sqlite':
                    raise ValueError('Legacy party-link constraint requires the documented staged migration.')
                c.exec_driver_sql('CREATE TABLE crm_party_links_v3 ('
                    'id INTEGER NOT NULL PRIMARY KEY, client_id INTEGER NOT NULL, '
                    'company TEXT NOT NULL, party_id TEXT NOT NULL, confirmed_at TEXT NOT NULL, '
                    'basis TEXT NOT NULL, FOREIGN KEY(client_id) REFERENCES crm_clients (id), '
                    'UNIQUE (company, party_id))')
                c.exec_driver_sql('INSERT INTO crm_party_links_v3 '
                    '(id, client_id, company, party_id, confirmed_at, basis) SELECT '
                    'id, client_id, company, party_id, confirmed_at, basis FROM crm_party_links')
                c.exec_driver_sql('DROP TABLE crm_party_links')
                c.exec_driver_sql('ALTER TABLE crm_party_links_v3 RENAME TO crm_party_links')
                c.exec_driver_sql('CREATE INDEX crm_links_client ON crm_party_links (client_id)')
            c.execute(insert(versions).values(version=3, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 4)).scalar() is None:
            c.exec_driver_sql('CREATE INDEX IF NOT EXISTS rd_party_company_id '
                              'ON rd_parties (company, party_id)')
            c.execute(insert(versions).values(version=4, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 5)).scalar() is None:
            c.execute(insert(versions).values(version=5, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 6)).scalar() is None:
            # Authentication is additive and never shares ownership with imported rows.
            metadata.create_all(c)
            _seed_roles(c)
            c.execute(insert(versions).values(version=6, applied_at=now()))
        else:
            _seed_roles(c)
        if c.execute(select(versions.c.version).where(versions.c.version == 7)).scalar() is None:
            # Sales ownership is an application relationship. Keep legacy owner
            # labels, and backfill only unambiguous exact matches to active Sales
            # or External Sales accounts.
            metadata.create_all(c)
            sales_users = list(c.execute(select(users.c.id, users.c.username,
                users.c.display_name).select_from(users.join(user_roles,
                    users.c.id == user_roles.c.user_id).join(roles,
                    user_roles.c.role_id == roles.c.id)).where(
                        users.c.active == 1,
                        roles.c.name.in_(('Sales', 'External Sales')))).mappings())
            for client in c.execute(select(clients.c.id, clients.c.owner).where(
                    clients.c.owner.is_not(None), clients.c.owner != '')).mappings():
                owner = str(client['owner']).strip().casefold()
                matches = [user for user in sales_users if owner in {
                    str(user['username']).strip().casefold(),
                    str(user['display_name']).strip().casefold()}]
                if len(matches) == 1 and c.execute(select(
                        client_assignments.c.client_id).where(
                            client_assignments.c.client_id == client['id'])).scalar() is None:
                    c.execute(insert(client_assignments).values(
                        client_id=client['id'], user_id=matches[0]['id'],
                        assigned_by=None, assigned_at=now()))
            c.execute(insert(versions).values(version=7, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 8)).scalar() is None:
            # A client may be shared by several internal/external sales users.
            # Preserve every existing assignment while widening the primary key.
            primary_key = inspect(c).get_pk_constraint(client_assignments.name)
            key_columns = set(primary_key.get('constrained_columns') or ())
            if key_columns != {'client_id', 'user_id'}:
                if c.dialect.name == 'sqlite':
                    c.exec_driver_sql('CREATE TABLE crm_client_assignments_v8 ('
                        'client_id INTEGER NOT NULL, user_id INTEGER NOT NULL, '
                        'assigned_by INTEGER, assigned_at TEXT NOT NULL, '
                        'PRIMARY KEY (client_id, user_id), '
                        'FOREIGN KEY(client_id) REFERENCES crm_clients (id) ON DELETE CASCADE, '
                        'FOREIGN KEY(user_id) REFERENCES crm_users (id) ON DELETE CASCADE, '
                        'FOREIGN KEY(assigned_by) REFERENCES crm_users (id))')
                    c.exec_driver_sql('INSERT INTO crm_client_assignments_v8 '
                        '(client_id, user_id, assigned_by, assigned_at) SELECT '
                        'client_id, user_id, assigned_by, assigned_at '
                        'FROM crm_client_assignments')
                    c.exec_driver_sql('DROP TABLE crm_client_assignments')
                    c.exec_driver_sql('ALTER TABLE crm_client_assignments_v8 '
                                      'RENAME TO crm_client_assignments')
                    c.exec_driver_sql('CREATE INDEX crm_client_assignments_user '
                                      'ON crm_client_assignments (user_id)')
                elif c.dialect.name == 'postgresql':
                    constraint_name = primary_key.get('name')
                    if not constraint_name:
                        raise ValueError('Could not identify the assignment primary key.')
                    quoted = c.dialect.identifier_preparer.quote(constraint_name)
                    c.exec_driver_sql('ALTER TABLE crm_client_assignments '
                                      f'DROP CONSTRAINT {quoted}')
                    c.exec_driver_sql('ALTER TABLE crm_client_assignments '
                                      'ADD PRIMARY KEY (client_id, user_id)')
                else:
                    raise ValueError('Assignment migration supports SQLite and PostgreSQL.')
            _seed_roles(c)
            c.execute(insert(versions).values(version=8, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 9)).scalar() is None:
            # External Sales is a client/follow-up role and must not be able to
            # list, open, edit, price, or export quotations.
            external_role_id = c.execute(select(roles.c.id).where(
                roles.c.name == 'External Sales')).scalar()
            if external_role_id is not None:
                c.execute(role_permissions.delete().where(
                    role_permissions.c.role_id == external_role_id,
                    role_permissions.c.permission.in_((
                        'quotations.view', 'quotations.edit',
                        'quotations.price_edit'))))
            c.execute(insert(versions).values(version=9, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 10)).scalar() is None:
            # Invoice payment dates are operational CRM data. They remain
            # separate from imported accounting rows and can be cleared without
            # changing the source voucher.
            metadata.create_all(c)
            c.execute(insert(versions).values(version=10, applied_at=now()))
        if c.execute(select(versions.c.version).where(versions.c.version == 11)).scalar() is None:
            # Login CAPTCHA challenges are short-lived, one-time authentication
            # records. Keeping them server-side prevents a solved challenge from
            # being replayed or its answer from being recovered from page markup.
            metadata.create_all(c)
            c.execute(insert(versions).values(version=11, applied_at=now()))


WRITE_LOCK_WAIT_SECONDS = 15


@contextmanager
def transaction(engine):
    with sync_lock(engine, WRITE_LOCK_WAIT_SECONDS), engine.begin() as connection:
        yield connection
