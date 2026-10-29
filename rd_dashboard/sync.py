"""Sync and quality queries; full audit payloads are fetched only on selection."""
from sqlalchemy import select, func, true
from rd_import import db
from .store import ReadStore, Row, page_bounds


def latest_attempt_ids():
    ranked = select(db.attempts.c.id, func.row_number().over(
        partition_by=(db.attempts.c.company, db.attempts.c.endpoint),
        order_by=(db.attempts.c.started_at.desc(), db.attempts.c.id.desc())).label('rank')).subquery()
    return select(ranked.c.id).where(ranked.c.rank == 1)


def latest_attempts(store: ReadStore) -> list[Row]:
    columns = [db.attempts.c.id, db.attempts.c.company, db.attempts.c.endpoint, db.attempts.c.status,
               db.attempts.c.started_at, db.attempts.c.finished_at, db.attempts.c.counts_json, db.attempts.c.error_code]
    return store.rows(select(*columns).where(db.attempts.c.id.in_(latest_attempt_ids())).order_by(db.attempts.c.endpoint))


def checkpoints(store: ReadStore) -> list[Row]:
    return store.rows(select(db.checkpoints).order_by(db.checkpoints.c.company, db.checkpoints.c.endpoint))


def current_counts(store: ReadStore) -> list[Row]:
    from sqlalchemy import union_all
    return store.rows(union_all(*[
        select(table.c.company, table.c.endpoint, func.count().label('current_records')).group_by(table.c.company, table.c.endpoint)
        for table in (db.products, db.parties, db.vouchers)]))


def company_registry(store: ReadStore) -> list[Row]:
    return store.rows(select(db.source_companies).order_by(db.source_companies.c.code).limit(100))


def known_companies(store: ReadStore) -> list[str]:
    from sqlalchemy import union
    rows = store.rows(union(select(db.source_companies.c.code.label('company')),
                            select(db.attempts.c.company)).limit(100))
    return sorted(row['company'] for row in rows)


def issue_counts(store: ReadStore) -> list[Row]:
    issues = func.json_each(db.sources.c.issues_json).table_valued('key', 'value').alias('issues')
    return store.rows(select(issues.c.value.label('issue'), func.count().label('occurrences'))
        .select_from(db.sources.join(issues, true())).where(db.sources.c.attempt_id.in_(latest_attempt_ids()))
        .group_by(issues.c.value).order_by(func.count().desc()))


def run_page(store: ReadStore, limit: int = 20, page: int = 1) -> dict:
    size, offset = page_bounds(limit, page)
    return dict(total=store.scalar(select(func.count()).select_from(db.runs)), rows=store.rows(
        select(db.runs).order_by(db.runs.c.started_at.desc()).limit(size).offset(offset)))


def run_attempts(store: ReadStore, run_id: str) -> list[Row]:
    return store.rows(select(db.attempts.c.id, db.attempts.c.company, db.attempts.c.endpoint,
        db.attempts.c.started_at, db.attempts.c.finished_at, db.attempts.c.status,
        db.attempts.c.counts_json, db.attempts.c.error_code, db.attempts.c.request_json)
        .where(db.attempts.c.run_id == run_id).order_by(db.attempts.c.started_at).limit(200))


def quality_page(store: ReadStore, issue: str = '', disposition: str = '', endpoint: str = '',
                 latest: bool = True, limit: int = 50, page: int = 1) -> dict:
    clauses = [] if disposition else [db.sources.c.issues_json != '[]']
    if latest:
        clauses.append(db.sources.c.attempt_id.in_(latest_attempt_ids()))
    if issue:
        from rd_import.codec import dumps
        clauses.append(db.sources.c.issues_json.contains(dumps(issue), autoescape=True))
    if disposition:
        clauses.append(db.sources.c.disposition == disposition)
    if endpoint:
        clauses.append(db.attempts.c.endpoint == endpoint)
    joined = db.sources.join(db.attempts, db.sources.c.attempt_id == db.attempts.c.id)
    size, offset = page_bounds(limit, page)
    return dict(total=store.scalar(select(func.count()).select_from(joined).where(*clauses)), rows=store.rows(
        select(db.sources.c.id, db.sources.c.row_index, db.sources.c.disposition, db.sources.c.issues_json,
               db.attempts.c.company, db.attempts.c.endpoint, db.attempts.c.started_at)
        .select_from(joined).where(*clauses).order_by(db.attempts.c.started_at.desc(), db.sources.c.row_index)
        .limit(size).offset(offset)))


def source_detail(store: ReadStore, source_id: int) -> Row | None:
    return store.one(select(db.sources, db.attempts.c.company, db.attempts.c.endpoint,
        db.attempts.c.started_at, db.attempts.c.error_code).join(db.attempts, db.sources.c.attempt_id == db.attempts.c.id)
        .where(db.sources.c.id == source_id))
