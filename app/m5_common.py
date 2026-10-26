"""Shared supply authorization, business vouchers and immutable evidence."""
from sqlalchemy import select, insert
from . import db, inventory as inv, workflows as w, m5_schema as s, catalog
from .accounts import assert_current
from .domain import require_role, get_warehouse, get_sku, uid, now
from .m3_common import fail, version
from .m4_common import validate
from .clock import business_now
from .events import record_event, request_context

READ = ('admin', 'warehouse', 'operations', 'analyst')
WRITE = ('admin', 'warehouse')

def scope(c, actor, warehouse, sku=None):
    assert_current(c, actor)
    get_warehouse(c, actor, warehouse, True)
    if sku: get_sku(c, actor, sku)

def supplier(c, actor, identity, sku=None):
    row = c.execute(select(catalog.suppliers).where(catalog.suppliers.c.id==identity, catalog.suppliers.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row or not row['active']: fail('SUPPLIER_UNAVAILABLE', '供应商不存在或已停用', 422)
    if sku and not c.scalar(select(catalog.supplier_skus.c.id).where(catalog.supplier_skus.c.tenant_id==actor['tenant_id'], catalog.supplier_skus.c.supplier_id==identity, catalog.supplier_skus.c.sku_id==sku)):
        fail('SUPPLIER_SKU_UNAVAILABLE', '商品不在供应商供货范围', 422)
    return dict(row)

def command(engine, actor, operation, body, key, apply, authorize):
    if not isinstance(key, str) or not 8<=len(key)<=160: fail('INVALID_KEY', '请求编号须为8至160个字符', 422)
    return w.command(engine, actor, 'm5.'+operation, body,
        'm5:'+inv.fingerprint({'operation':operation,'key':key}), apply, authorize=authorize)

def voucher(c, actor, operation, reference, body, apply):
    payload = {k:v for k,v in body.items() if k not in ('expected_version','expected_balance_version')}
    digest = inv.fingerprint(payload)
    old = c.execute(select(s.actions).where(s.actions.c.tenant_id==actor['tenant_id'], s.actions.c.operation==operation, s.actions.c.reference==reference)).mappings().first()
    if old:
        if old['fingerprint']!=digest: fail('VOUCHER_CONFLICT', '业务凭证已用于不同内容')
        return {**old['response'], 'business_replayed':True}
    result = apply()
    c.execute(insert(s.actions).values(id=uid(),tenant_id=actor['tenant_id'],operation=operation,reference=reference,fingerprint=digest,response=result))
    return result

def record(c, actor, object_type, row, action, payload, *, quantity=None, lifecycle='registered', occurred_at=None, observed=True, dependencies=None):
    event_id = record_event(c, actor, event_type=object_type+'.'+action, object_type=object_type, object_id=row['id'],
        object_line_id=row.get('supply_line_id'), process_id=row.get('order_id',row['id']), batch_id=row['id'] if object_type=='supply_batch' else None,
        warehouse_id=row.get('warehouse_id'), sku_id=row.get('sku_id'), planned_at=row.get('expected_at',row.get('latest_due')),
        occurred_at=business_now(c.engine) if observed else occurred_at, time_quality='observed' if observed else ('reported' if occurred_at else 'unknown'),
        quantity=quantity, lifecycle=lifecycle, source_system='local_m5', request_id=request_context.get(),
        business_reference=payload.get('reference',row.get('code')), rule_version='supply-v1',
        reason_code='operation', evidence_refs=[row['id']], dependencies=dependencies or [], payload=payload)
    c.execute(insert(s.history).values(id=uid(),tenant_id=actor['tenant_id'],object_type=object_type,object_id=row['id'],
        action=action,actor_id=actor['id'],payload=payload,event_id=event_id,created_at=now()))

def history(c, kind, identity):
    return [dict(r) for r in c.execute(select(s.history).where(s.history.c.object_type==kind,s.history.c.object_id==identity).order_by(s.history.c.created_at,s.history.c.id)).mappings()]

def anomaly(c, actor, line, rule, quantity, *, due_at=None, object_type='supply_line'):
    from .exception_tasks import upsert
    from .m3_models import Details
    return upsert(c,actor,dict(object_type=object_type,object_id=line['id'],warehouse_id=line['warehouse_id'],sku_id=line['sku_id'],
        rule=rule,rule_version='supply-v1',instance=line['id'],type='m5_'+rule,priority='normal',impact_quantity=quantity,
        due_at=due_at,requires_review=False,details=Details(reason_code='supplier' if rule=='purchase_late' else 'transport',
        opinion='请核实交期与剩余供给；到货后更新影响量，任务关闭不修改库存',evidence_refs=[line['id']]).model_dump()),automatic=True)
