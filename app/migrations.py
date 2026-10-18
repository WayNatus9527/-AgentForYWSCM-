"""Ordered, additive migrations. Existing phase-one databases are adopted as v1."""
from sqlalchemy import Table, Column, Integer, String, select, insert
from . import db

versions = Table('schema_migrations', db.metadata,
                 Column('version', Integer, primary_key=True), Column('name', String, nullable=False))
BASE = ('tenants','users','sessions','warehouses','skus','balances','documents','ledger','outbox','idempotency')
PHASE4 = ('agent_proposals','agent_action_audit')
PHASE3 = ('transfers','transfer_lines','transfer_events','transfer_claims','return_orders','quality_events','user_settings','admin_audit')
PHASE2 = ('import_batches','purchase_orders','purchase_lines','purchase_receipts','approval_requests','reversal_links')


def m1_tables():
    """Each new domain owns its additive tables; legacy migration lists stay fixed."""
    from . import events, catalog, inventory, commitments
    modules = (events, catalog, inventory, commitments)
    names = tuple(name for module in modules for name in module.TABLE_NAMES)
    if len(names) != len(set(names)) or any(name not in db.metadata.tables for name in names):
        raise RuntimeError('Invalid M1 additive schema registration')
    return names


def migrate(engine):
    from . import workflows, operations, accounts, agent_actions  # Register all additive schemas.
    from . import events, catalog
    from .config import bind_runtime
    m1 = m1_tables()
    with db.write(engine, '__schema_migration__') as c:
        versions.create(c, checkfirst=True)
        applied = set(c.execute(select(versions.c.version)).scalars())
        if applied - {1, 2, 3, 4, 5, 6}:
            raise RuntimeError('Database schema is newer than this application')
        # Versions 1-4 remain exactly the existing additive table migrations.
        for version, name, tables in [(1,'phase_one_baseline',BASE),(2,'imports_purchases_approvals',PHASE2),(3,'transfers_returns_trial',PHASE3),(4,'agent_confirmed_actions',PHASE4)]:
            if version in applied:
                continue
            for table in tables:
                db.metadata.tables[table].create(c, checkfirst=True)
            c.execute(insert(versions).values(version=version,name=name))
        if 5 not in applied:
            for table in m1:
                db.metadata.tables[table].create(c, checkfirst=True)
            events.install_append_only_guards(c)
            catalog.backfill_existing(c)
            c.execute(insert(versions).values(version=5, name='m1_runtime_catalog_inventory_events'))
        if 6 not in applied:
            from . import counting, exception_tasks, quality_control
            from .m3_common import append_only
            for module in (counting, exception_tasks, quality_control):
                for table in module.TABLE_NAMES:
                    db.metadata.tables[table].create(c, checkfirst=True)
            append_only(c, ('stock_count_observations', 'stock_count_history', 'exception_task_history', 'quality_action_history'))
            c.execute(insert(versions).values(version=6, name='m3_counts_quality_exceptions'))
        bind_runtime(engine, role='migration', connection=c)
