from datetime import date, datetime, timedelta, timezone
import uuid

from sqlalchemy import select, insert, update, delete, and_
from . import db
from .api import ENDPOINTS, allows_reused_voucher_number, company_for, error_code
from .codec import dumps
from .mapping import prepare, target
from .companies import companies_for, validate_company_code


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid.uuid4())


def api_date(value):
    months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
    return f'{value.day:02}/{months[value.month-1]}/{value.year}'


class Importer:
    def __init__(self, engine, config, client, overlap_days=2, today=date.today, fault=None):
        if not 1 <= overlap_days <= 365:
            raise ValueError('overlap_days must be 1..365')
        self.engine, self.config, self.client = engine, config, client
        self.overlap_days, self.today, self.fault = overlap_days, today, fault

    def run(self, endpoints=ENDPOINTS, incremental=False, progress=None, company: str | None = None):
        if not endpoints or len(set(endpoints)) != len(endpoints) or any(e not in ENDPOINTS for e in endpoints):
            raise ValueError('Invalid endpoint selection')
        # Normalize selection to the required sequence, regardless of caller order.
        endpoints = [ep for ep in ENDPOINTS if ep in endpoints]
        if company is not None:
            validate_company_code(company)
            if len(endpoints) != 1:
                raise ValueError('A company override requires exactly one endpoint')
        # Resolve every scope before taking the lock or making a request. Keep all
        # companies for an endpoint together in the required endpoint sequence.
        scopes = [(endpoint, code) for endpoint in endpoints
                  for code in ((company,) if company is not None else companies_for(self.config, endpoint))]
        with db.sync_lock(self.engine):
            db.initialize(self.engine, self.config)
            run_id = uid()
            with self.engine.begin() as connection:
                connection.execute(insert(db.runs).values(id=run_id, started_at=timestamp(), status='running'))
            results = []
            for endpoint, source_company in scopes:
                if progress:
                    progress({'endpoint': endpoint, 'company': source_company, 'status': 'fetching'})
                results.append(self.import_endpoint(run_id, endpoint, incremental, source_company))
                if progress:
                    progress(results[-1])
            status = 'partial_failure' if any(r['status'] != 'success' for r in results) else 'success'
            with self.engine.begin() as connection:
                connection.execute(update(db.runs).where(db.runs.c.id == run_id).values(status=status, finished_at=timestamp()))
            return {'run_id': run_id, 'status': status, 'endpoints': results}

    def import_endpoint(self, run_id, endpoint, incremental, company=None):
        company = company if company is not None else company_for(self.config, endpoint)
        attempt_id, start_date = uid(), self.today()
        scope = and_(db.checkpoints.c.company == company, db.checkpoints.c.endpoint == endpoint)
        with self.engine.connect() as connection:
            checkpoint = connection.execute(select(db.checkpoints).where(scope)).mappings().first()
        since = date.fromisoformat(checkpoint['successful_date']) - timedelta(days=self.overlap_days) if checkpoint and incremental else None
        payload = {'compshrtname': company, 'fetchall': 'N' if since else 'Y', 'lastsyncdt': api_date(since or start_date)}
        counts = dict(fetched=0, inserted=0, updated=0, unchanged=0, quarantined=0, failed=0)
        with self.engine.begin() as connection:
            connection.execute(insert(db.attempts).values(id=attempt_id, run_id=run_id, company=company,
                endpoint=endpoint, started_at=timestamp(), request_json=dumps(payload), status='running'))

        def observe(route, body, status):
            with self.engine.begin() as connection:
                connection.execute(insert(db.responses).values(attempt_id=attempt_id, route=route,
                    status=status, received_at=timestamp(), body=body))

        def save_decrypted(value):
            with self.engine.begin() as connection:
                connection.execute(update(db.attempts).where(db.attempts.c.id == attempt_id).values(decrypted_json=dumps(value)))

        stage = 'fetch'
        try:
            rows = self.client.fetch(endpoint, payload, observe, save_decrypted)
            counts['fetched'] = len(rows)
            stage = 'mapping'
            prepared = prepare(endpoint, rows, company, self.config)
            observed_keys = {item['identity'] for item in prepared if not item.get('quarantine')}
            stage = 'database'
            # Audit is a separate durable transaction, retained on current-data rollback.
            with self.engine.begin() as connection:
                for item in prepared:
                    item['source_id'] = connection.execute(insert(db.sources).values(attempt_id=attempt_id,
                        row_index=item['index'], identity=item['identity'], payload_json=dumps(item['row']),
                        source_hash=item['hash'], disposition='pending', issues_json=dumps(item['issues']))).inserted_primary_key[0]
            with self.engine.begin() as connection:
                for item in prepared:
                    disposition = self.apply(connection, run_id, endpoint, company, item, observed_keys)
                    counts[disposition] += 1
                    connection.execute(update(db.sources).where(db.sources.c.id == item['source_id']).values(
                        disposition=disposition, issues_json=dumps(item['issues'])))
                    if self.fault:
                        self.fault(endpoint, item['index'])
                status = 'quarantined' if counts['quarantined'] else 'success'
                if status == 'success':
                    values = dict(successful_date=max(start_date.isoformat(), checkpoint['successful_date']) if checkpoint else start_date.isoformat(),
                                  attempt_id=attempt_id, committed_at=timestamp())
                    if checkpoint:
                        connection.execute(update(db.checkpoints).where(scope).values(**values))
                    else:
                        connection.execute(insert(db.checkpoints).values(company=company, endpoint=endpoint, **values))
                connection.execute(update(db.attempts).where(db.attempts.c.id == attempt_id).values(
                    status=status, finished_at=timestamp(), counts_json=dumps(counts)))
            return dict(endpoint=endpoint, company=company, status=status, **counts)
        except Exception as error:
            # Endpoint transaction has rolled back; no inserted/updated counts survive.
            counts.update(inserted=0, updated=0, unchanged=0, quarantined=0, failed=counts['fetched'])
            code = error_code(error)
            if stage != 'fetch':
                code = stage + '_' + code
            with self.engine.begin() as connection:
                connection.execute(update(db.sources).where(db.sources.c.attempt_id == attempt_id).values(disposition='failed'))
                connection.execute(update(db.attempts).where(db.attempts.c.id == attempt_id).values(
                    status='failed', finished_at=timestamp(), counts_json=dumps(counts), error_code=code))
            return dict(endpoint=endpoint, company=company, status='failed', error_code=code, **counts)

    def apply(self, connection, run_id, endpoint, company, item, observed_keys,
              allow_reused_number=False):
        if item.get('quarantine'):
            return 'quarantined'
        table = target(endpoint)
        scope = and_(table.c.company == company, table.c.endpoint == endpoint)
        existing = connection.execute(select(table).where(scope, table.c.identity == item['identity'])).mappings().first()
        if existing and existing['identity_json'] != dumps(item['key']):
            item['issues'].append('identity_hash_collision')
            return 'quarantined'
        if table is db.vouchers and not existing:
            # A renumbered/date/branch/type edit cannot safely be distinguished
            # from a second voucher. Hold possible matches for explicit review.
            # Distinct composite keys coexisting in the same source response are
            # separate records. If the old key is absent, a date/branch edit is
            # indistinguishable from a new voucher reusing its number: quarantine.
            candidates = connection.execute(select(table.c.identity).where(scope,
                table.c.number == item['data']['number'], table.c.voucher_type == item['data']['voucher_type'])).scalars()
            if any(key not in observed_keys for key in candidates):
                configured = allows_reused_voucher_number(
                    self.config, company, endpoint)
                if not (allow_reused_number or configured):
                    item['issues'].append('possible_identity_field_edit_or_reused_number')
                    return 'quarantined'
                if configured:
                    item['issues'].append('known_reused_voucher_number_auto_accepted')
        if existing and existing['source_hash'] == item['hash']:
            return 'unchanged'
        now = timestamp()
        values = dict(company=company, endpoint=endpoint, identity=item['identity'], identity_json=dumps(item['key']),
            source_hash=item['hash'], payload_json=dumps(item['row']), source_record_id=item['source_id'],
            import_run_id=run_id, updated_at=now, **item['data'])
        if existing:
            record_id = existing['id']
            connection.execute(update(table).where(table.c.id == record_id).values(**values))
            if table is db.vouchers:
                for child in (db.allocations, db.payments, db.lines, db.ledgers):
                    connection.execute(delete(child).where(child.c.voucher_id == record_id))
        else:
            record_id = connection.execute(insert(table).values(created_at=now, **values)).inserted_primary_key[0]
        if table is db.vouchers:
            children = item['children']
            for name, child in (('lines', db.lines), ('ledgers', db.ledgers)):
                for values in children[name]:
                    connection.execute(insert(child).values(voucher_id=record_id, **values))
            for payment, refs in children['payments']:
                payment_id = connection.execute(insert(db.payments).values(voucher_id=record_id, **payment)).inserted_primary_key[0]
                for ref in refs:
                    connection.execute(insert(db.allocations).values(voucher_id=record_id, payment_id=payment_id, **ref))
        return 'updated' if existing else 'inserted'
