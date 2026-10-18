"""Shared M3 validation and evidence, with authorization before cached replies."""
from sqlalchemy import select
from . import db, workflows
from .accounts import assert_current
from .clock import business_now
from .domain import BusinessError, get_warehouse, get_sku
from .events import record_event, request_context

REASONS = ('supplier', 'transport', 'queue', 'operation', 'quality', 'data_delay', 'mapping', 'system', 'unknown')
DEFECTS = ('none', 'damaged', 'missing', 'wrong_item', 'packaging', 'functional', 'unknown')


def fail(code, message, status=409):
    raise BusinessError(code, message, status)


def scope(c, actor, warehouse, sku):
    get_warehouse(c, actor, warehouse, True)
    get_sku(c, actor, sku)


def version(row, body):
    if row['version'] != body['expected_version']:
        fail('VERSION_CONFLICT', '记录已变化，请刷新后重新确认')


def command(engine, actor, kind, body, key, apply, authorize):
    if not isinstance(key, str) or not 8 <= len(key) <= 160:
        fail('INVALID_KEY', '请求编号须为8至160个字符', 422)
    return workflows.command(engine, actor, 'm3.'+kind, body, key, apply, authorize=authorize)


def read_actor(c, actor):
    assert_current(c, actor)


def evidence(c, actor, kind, row, action, *, lifecycle='registered', quantity=None, payload=None):
    meta = row.get('details') or {}
    return record_event(c, actor, event_type=f'{kind}.{action}', object_type=kind,
        object_id=row['id'], object_line_id=row.get('line_id'), lifecycle=lifecycle, occurred_at=business_now(c.engine),
        planned_at=row.get('due_at'), request_id=request_context.get(), business_reference=row.get('code') or row.get('reference'),
        time_quality='observed', warehouse_id=row.get('warehouse_id'), sku_id=row.get('sku_id'),
        task_id=row['id'] if kind == 'exception_task' else None,
        quantity=quantity, rule_version=row.get('rule_version') or 'm3-v1',
        reason_code=meta.get('reason_code'), evidence_refs=meta.get('evidence_refs', []),
        payload=payload or {}, source_system='local_m3')


def append_only(c, names):
    """Historical observations and decisions cannot be overwritten by normal SQL."""
    for name in names:  # Static schema names only, never request text.
        if c.dialect.name == 'sqlite':
            for verb in ('UPDATE', 'DELETE'):
                c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {name}_no_{verb.lower()} BEFORE {verb} ON {name} "
                    f"BEGIN SELECT RAISE(ABORT, '{name} is append only'); END")
        else:
            c.exec_driver_sql(f'CREATE TRIGGER {name}_append_only BEFORE UPDATE OR DELETE ON {name} '
                             'FOR EACH ROW EXECUTE FUNCTION reject_business_event_mutation()')
