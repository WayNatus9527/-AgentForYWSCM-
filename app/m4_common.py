"""M4 scope, idempotency and evidence shared by APIs and workers."""
from pydantic import ValidationError
from sqlalchemy import select, insert
from . import m4_schema as s, workflows, inventory
from .domain import require_role, get_warehouse, get_sku, uid, now
from .accounts import assert_current
from .m3_common import fail
from .clock import business_now
from .events import record_event, request_context

READ_ROLES = ('admin', 'warehouse', 'operations', 'analyst')
ORDER_ROLES = ('admin', 'warehouse', 'operations')


def validate(model, body):
    try:
        return model.model_validate(body).model_dump()
    except ValidationError as exc:
        fail('INVALID_INPUT', '; '.join('.'.join(map(str, x['loc'])) + ': ' + x['msg'] for x in exc.errors()), 422)


def connection(c, actor, identity, *, active=True):
    require_role(actor, *READ_ROLES)
    assert_current(c, actor)
    row = c.execute(select(s.connections).where(s.connections.c.id == identity, s.connections.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row or (actor['role'] != 'admin' and actor['id'] not in row['member_ids']):
        fail('NOT_FOUND', '连接账号不存在或未授权', 404)
    if active and not row['active']:
        fail('CONNECTION_DISABLED', '模拟连接已停用')
    return dict(row)


def scope(c, actor, account, warehouse, sku=None):
    if warehouse not in account['warehouse_ids']:
        fail('NOT_FOUND', '仓库不在连接授权范围内', 404)
    get_warehouse(c, actor, warehouse, True)
    if sku:
        get_sku(c, actor, sku)


def command(engine, actor, operation, body, key, apply, authorize):
    if not isinstance(key, str) or not 8 <= len(key) <= 160:
        fail('INVALID_KEY', '请求编号须为8至160个字符', 422)
    return workflows.command(engine, actor, 'm4.' + operation, body,
        'm4:' + inventory.fingerprint({'operation': operation, 'key': key}), apply, authorize=authorize)


def event(c, actor, order, action, *, line=None, batch=None, quantity=None, lifecycle='registered', payload=None,
          occurred_at=None, observed=True, dependencies=None, source=None, task_id=None):
    account = connection(c, actor, order['connection_id'])
    return record_event(c, actor, event_type='fulfillment.' + action, object_type='sales_order', object_id=order['id'],
        process_id=order['id'], object_line_id=line['id'] if line else None, batch_id=batch['id'] if batch else None, task_id=task_id,
        warehouse_id=batch['warehouse_id'] if batch else None, sku_id=line['sku_id'] if line else None,
        lifecycle=lifecycle, quantity=quantity, planned_at=order['planned_at'],
        occurred_at=business_now(c.engine) if observed else occurred_at,
        time_quality='observed' if observed else ('reported' if occurred_at else 'unknown'),
        received_at=source.get('received_at') if source else None, source_system='local_m4',
        source_account=account['source_account'], source_version=str(source['source_version']) if source and source.get('source_version') else None,
        business_reference=order['source_order_id'], request_id=request_context.get(), rule_version='fulfillment-v1',
        dependencies=dependencies or [], payload=payload or {})


def record(c, actor, order, action, body, **kw):
    event_id = event(c, actor, order, action, payload=body, **kw)
    c.execute(insert(s.history).values(id=uid(), tenant_id=actor['tenant_id'], order_id=order['id'], action=action,
        actor_id=actor['id'], payload=body, event_id=event_id, created_at=now()))


def anomaly(c, actor, *, object_type, object_id, warehouse_id, sku_id, rule, quantity=0, evidence=None, instance='current'):
    from .exception_tasks import upsert
    from .m3_models import Details
    return upsert(c, actor, dict(object_type=object_type, object_id=object_id, warehouse_id=warehouse_id, sku_id=sku_id,
        rule=rule, rule_version='m4-v1', instance=instance, type='m4_' + rule, priority='high', impact_quantity=quantity,
        due_at=None, requires_review=True, details=Details(reason_code='system', opinion='请核实业务状态及关联证据后处置',
        evidence_refs=evidence or [object_id]).model_dump()), automatic=True)
