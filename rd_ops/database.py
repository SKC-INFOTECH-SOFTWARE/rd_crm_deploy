from rd_crm import db as crm_db
from rd_import import db as import_db
from rd_stock import database as stock_db


def migrate_all(engine, config=None) -> None:
    """Apply additive schemas in dependency order under crash-safe locks."""
    with import_db.sync_lock(engine):
        import_db.initialize(engine, config)
    stock_db.migrate(engine)
    crm_db.migrate(engine)


def ready(engine) -> bool:
    return crm_db.ready(engine) and stock_db.ready(engine)
