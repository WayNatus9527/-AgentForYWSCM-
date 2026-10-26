"""Durable Inbox and a local simulated source. No external network writes."""
from sqlalchemy import select, insert, update
from . import db, m4_schema as s, m4_models as models, fulfillment as f, inventory as inv
from .domain import uid, now, require_role, BusinessError
from .accounts import assert_current, active
from .m3_common import fail
from .m4_common import connection, scope, validate, command, anomaly, READ_ROLES, record


def members(c, actor, ids):
    if len(set(ids)) != len(ids): fail('INVALID_MEMBERS', '连接成员不可重复', 422)
    for identity in ids:
        row = c.execute(select(db.users).where(db.users.c.id == identity, db.users.c.tenant_id == actor['tenant_id'])).mappings().first()
        if not row or not active(c, identity) or row['role'] not in READ_ROLES:
            fail('INVALID_MEMBERS', '连接成员须为本商户有效的管理员、仓管、运营或分析人员', 422)


def create(engine, actor, body, key):
    require_role(actor, 'admin'); body = validate(models.Connection, body)
    def apply(c):
        members(c, actor, body['member_ids'])
        if len(set(body['warehouse_ids'])) != len(body['warehouse_ids']): fail('INVALID_SCOPE', '授权仓库不可重复', 422)
        for warehouse in body['warehouse_ids']:
            from .domain import get_warehouse
            get_warehouse(c, actor, warehouse, True)
        row = dict(id=uid(), tenant_id=actor['tenant_id'], **body, adapter='simulated', active=1, version=1, created_at=now())
        c.execute(insert(s.connections).values(**row))
        return row
    return command(engine, actor, 'connections.create', body, key, apply, lambda c: assert_current(c, actor))


def edit(engine, actor, identity, body, key):
    require_role(actor, 'admin'); body = validate(models.ConnectionUpdate, body)
    def apply(c):
        row = connection(c, actor, identity, active=False)
        if row['version'] != body['expected_version']: fail('VERSION_CONFLICT', '连接权限已变化')
        members(c, actor, body['member_ids'])
        before = dict(row)
        row.update(member_ids=body['member_ids'], active=int(body['active']), version=row['version'] + 1)
        c.execute(update(s.connections).where(s.connections.c.id == identity).values(**row))
        from .accounts import audit
        c.execute(insert(audit).values(id=uid(), tenant_id=actor['tenant_id'], actor_id=actor['id'], action='connector.update',
            target_id=identity, before=before, after=row, created_at=now()))
        return row
    return command(engine, actor, 'connections.edit:' + identity, body, key, apply, lambda c: connection(c, actor, identity, active=False))


def listing(engine, actor, table=s.connections, offset=0, limit=50):
    require_role(actor, *READ_ROLES)
    with engine.connect() as c:
        assert_current(c, actor)
        rows = c.execute(select(table).where(table.c.tenant_id == actor['tenant_id']).order_by(table.c.id)).mappings()
        visible = []
        for row in rows:
            try:
                account = connection(c, actor, row['id'] if table is s.connections else row['connection_id'], active=False)
                if table is not s.connections:
                    scope(c, actor, account, row['warehouse_id'], row['sku_id'])
                elif actor['role'] != 'admin' and not set(row['warehouse_ids']).intersection(actor['warehouse_ids']): continue
            except BusinessError as ex:
                if ex.code == 'NOT_FOUND': continue
                raise
            result = dict(row)
            if table is s.outbox:
                result['history'] = [dict(x) for x in c.execute(select(s.attempts).where(s.attempts.c.outbox_id == row['id']).order_by(s.attempts.c.created_at, s.attempts.c.id)).mappings()]
            if table is s.inbox:
                result['conflicts'] = [dict(x) for x in c.execute(select(s.conflicts).where(s.conflicts.c.inbox_id == row['id'])).mappings()]
            visible.append(result)
        return dict(items=visible[offset:offset + limit], total=len(visible), offset=offset, limit=limit, has_more=len(visible) > offset + limit)


def order_by_source(c, actor, account, entity):
    identity = c.scalar(select(s.orders.c.id).where(s.orders.c.tenant_id == actor['tenant_id'], s.orders.c.connection_id == account['id'], s.orders.c.source_order_id == entity))
    if not identity: fail('SOURCE_ORDER_UNKNOWN', '来源订单尚未建立，保留消息等待前序或核实')
    return f.get(c, actor, identity)


def envelope_scope(c, actor, account, body):
    if body['run_id'] != c.engine.runtime_settings.run_id or body['tenant_id'] != actor['tenant_id']:
        fail('RUNTIME_MISMATCH', '消息不属于当前运行和商户', 403)
    kind, payload = body['kind'], body['payload']
    if kind == 'order.created':
        parsed = validate(models.OrderCreate, payload)
        if parsed['connection_id'] != account['id'] or parsed['source_order_id'] != body['entity_id']:
            fail('SOURCE_SCOPE_MISMATCH', '订单来源与连接或实体标识不一致', 422)
        for w in parsed['warehouse_ids']:
            for line in parsed['lines']: scope(c, actor, account, w, line['sku_id'])
        return parsed['warehouse_ids'][0], parsed['lines'][0]['sku_id']
    if kind == 'stock.delta':
        parsed = validate(models.StockDelta, payload)
        if body['entity_id'] != 'stock:' + parsed['warehouse_id'] + ':' + parsed['sku_id']:
            fail('SOURCE_SCOPE_MISMATCH', '库存实体标识不匹配', 422)
        scope(c, actor, account, parsed['warehouse_id'], parsed['sku_id'])
        return parsed['warehouse_id'], parsed['sku_id']
    if kind == 'shipment.delivered':
        if payload: fail('INVALID_INPUT', '签收事件不接受额外字段', 422)
        shipment = c.execute(select(s.shipments).where(s.shipments.c.id == body['entity_id'], s.shipments.c.tenant_id == actor['tenant_id'])).mappings().first()
        if not shipment: fail('NOT_FOUND', '发货批次不存在', 404)
        order = f.get(c, actor, shipment['order_id'])
        if order['connection_id'] != account['id']: fail('NOT_FOUND', '发货批次不属于此连接', 404)
        return shipment['warehouse_id'], shipment['sku_id']
    # Allow cancellation/completion to arrive before creation, without inventing
    # an order. An authorized explicit scope is retained in the receive API.
    if kind == 'order.cancelled': validate(models.IncomingCancel, payload)
    elif payload: fail('INVALID_INPUT', '渠道完成事件不接受额外字段', 422)
    try:
        order = order_by_source(c, actor, account, body['entity_id'])
    except BusinessError as ex:
        if ex.code != 'SOURCE_ORDER_UNKNOWN' or not body.get('warehouse_id') or not body.get('sku_id'): raise
        scope(c, actor, account, body['warehouse_id'], body['sku_id'])
        return body['warehouse_id'], body['sku_id']
    line = f.children(c, s.lines, order['id'])[0]
    return order['warehouse_ids'][0], line['sku_id']


def action_hash(body):
    return inv.fingerprint({k: body[k] for k in ('kind', 'entity_id', 'payload', 'occurred_at')})


def receive(engine, actor, identity, body):
    require_role(actor, 'admin')
    body = validate(models.Envelope, body)
    with db.write(engine, actor['tenant_id']) as c:
        account = connection(c, actor, identity)
        warehouse, sku = envelope_scope(c, actor, account, body)
        digest = inv.fingerprint(body)
        old = c.execute(select(s.inbox).where(s.inbox.c.tenant_id == actor['tenant_id'], s.inbox.c.connection_id == identity,
            s.inbox.c.run_id == body['run_id'], s.inbox.c.source_event_id == body['source_event_id'])).mappings().first()
        if old:
            if old['fingerprint'] == digest: return {**old, 'replayed': True}
            if not c.scalar(select(s.conflicts.c.id).where(s.conflicts.c.inbox_id == old['id'], s.conflicts.c.fingerprint == digest)):
                c.execute(insert(s.conflicts).values(id=uid(), tenant_id=actor['tenant_id'], connection_id=identity,
                    inbox_id=old['id'], fingerprint=digest, envelope=body, created_at=now()))
            if old['status'] not in ('processed', 'duplicate', 'stale'):
                c.execute(update(s.inbox).where(s.inbox.c.id == old['id']).values(status='conflict', error='EVENT_CONTENT_CONFLICT'))
            anomaly(c, actor, object_type='connector_inbox', object_id=old['id'], warehouse_id=old['warehouse_id'], sku_id=old['sku_id'], rule='event_conflict')
            return {**old, 'status': 'conflict', 'error': 'EVENT_CONTENT_CONFLICT'}
        row = dict(id=uid(), connection_id=identity, **{**body, 'warehouse_id': warehouse, 'sku_id': sku}, fingerprint=digest,
            received_at=now(), status='received', result={}, error=None)
        c.execute(insert(s.inbox).values(**row))
        return row


def source_fixture(engine, actor, identity, body):
    """Registers a simulated source fact separately from delivering its envelope."""
    require_role(actor, 'admin'); body = validate(models.Envelope, body)
    if body['source_version'] is None: fail('VERSION_REQUIRED', '模拟源核实事实须有版本', 422)
    with db.write(engine, actor['tenant_id']) as c:
        account = connection(c, actor, identity); envelope_scope(c, actor, account, body)
        old = c.execute(select(s.source_events).where(s.source_events.c.connection_id == identity,
            s.source_events.c.tenant_id == actor['tenant_id'], s.source_events.c.source_event_id == body['source_event_id'])).mappings().first()
        if old:
            if old['envelope'] != body: fail('SOURCE_FACT_CONFLICT', '模拟源事实已保存，不能改写')
            return dict(old)
        row = dict(id=uid(), tenant_id=actor['tenant_id'], connection_id=identity, source_event_id=body['source_event_id'], envelope=body, created_at=now())
        c.execute(insert(s.source_events).values(**row)); return row


def inbox_get(c, actor, identity):
    row = c.execute(select(s.inbox).where(s.inbox.c.id == identity, s.inbox.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND', '消息不存在', 404)
    account = connection(c, actor, row['connection_id']); scope(c, actor, account, row['warehouse_id'], row['sku_id'])
    return dict(row), account


def apply_event(c, actor, row, account):
    kind, payload = row['kind'], row['payload']
    if kind == 'order.created':
        if payload.get('occurred_at') and row['occurred_at'] and payload['occurred_at'] != row['occurred_at']:
            fail('SOURCE_TIME_CONFLICT', '来源事件与订单实际时间不一致')
        return f.create_in_transaction(c, actor, {**payload, 'occurred_at': payload.get('occurred_at') or row['occurred_at']}, source=row)
    if kind == 'stock.delta':
        data = validate(models.StockDelta, payload)
        if data['sequence'] != row['source_version']: fail('DELTA_VERSION_CONFLICT', '增量序号与来源版本不一致')
        old = c.execute(select(s.deltas).where(s.deltas.c.connection_id == account['id'], s.deltas.c.warehouse_id == data['warehouse_id'],
            s.deltas.c.sku_id == data['sku_id'], s.deltas.c.sequence == data['sequence'])).mappings().first()
        if old: fail('DELTA_SEQUENCE_CONFLICT', '库存增量序号已被其他业务动作使用')
        c.execute(insert(s.deltas).values(id=uid(), tenant_id=actor['tenant_id'], connection_id=account['id'],
            warehouse_id=data['warehouse_id'], sku_id=data['sku_id'], sequence=data['sequence'], delta={k: data[k] for k in ('g', 'q', 'd')},
            inbox_id=row['id'], occurred_at=row['occurred_at']))
        return {'observation_only': True}
    if kind == 'shipment.delivered':
        if not row['occurred_at']: fail('ACTUAL_TIME_REQUIRED', '签收实际时间未知，须查询核实')
        shipment = dict(c.execute(select(s.shipments).where(s.shipments.c.id == row['entity_id'], s.shipments.c.tenant_id == actor['tenant_id'])).mappings().one())
        order = f.get(c, actor, shipment['order_id'])
        if shipment['delivered_at']:
            if shipment['delivered_at'] != row['occurred_at']: fail('RECEIPT_CONFLICT', '签收时间已登记，请追加更正证据')
        else:
            c.execute(update(s.shipments).where(s.shipments.c.id == shipment['id']).values(delivered_at=row['occurred_at']))
            batch = f.item(c, s.batches, order, shipment['batch_id']); line = f.item(c, s.lines, order, batch['line_id'])
            record(c, actor, order, 'delivered', {'inbox_id': row['id'], 'shipment_id': shipment['id']}, line=line, batch=batch,
                lifecycle='completed', quantity=shipment['quantity'], observed=False, occurred_at=row['occurred_at'], source=row)
        return {'shipment_id': shipment['id']}
    order = order_by_source(c, actor, account, row['entity_id'])
    if kind == 'order.cancelled':
        parsed = validate(models.IncomingCancel, payload)
        line = next((x for x in f.children(c, s.lines, order['id']) if x['source_line_id'] == parsed['source_line_id']), None)
        if not line: fail('NOT_FOUND', '源订单行不存在', 404)
        return f.act_in_transaction(c, actor, order['id'], 'cancel', dict(expected_version=order['version'],
            action_id='inbox-' + row['id'], reason=parsed['reason'], line_id=line['id'], quantity=parsed['quantity']))
    rows = f.children(c, s.lines, order['id'])
    if any(x['quantity'] != x['shipped'] + x['cancelled'] for x in rows) or not sum(x['shipped'] for x in rows):
        fail('PHYSICAL_SHIPMENT_REQUIRED', '尚有未履约数量，渠道完成不能代替实物发出')
    if not row['occurred_at']: fail('ACTUAL_TIME_REQUIRED', '渠道完成实际时间未知，须核实')
    if order['channel_completed_at'] and order['channel_completed_at'] != row['occurred_at']: fail('COMPLETION_CONFLICT', '渠道完成时间已登记')
    order['channel_completed_at'] = row['occurred_at']; f.refresh(c, order)
    record(c, actor, order, 'channel_completed', {'inbox_id': row['id']}, lifecycle='completed', observed=False, occurred_at=row['occurred_at'], source=row)
    return {'order_id': order['id'], 'status': order['status']}


def process(engine, actor, identity, *, verify=False):
    require_role(actor, 'admin')
    with db.write(engine, actor['tenant_id']) as c:
        row, account = inbox_get(c, actor, identity)
        if row['status'] in ('processed', 'duplicate', 'stale', 'conflict'): return row
        effective = dict(row)
        verification = None
        if verify and account['supports_query']:
            # Query the original source event. Never repair its version by guessing.
            from .delivery import SimulatedAdapter
            fact = SimulatedAdapter(engine, actor).query_event(account['id'], row['source_event_id'])
            if fact and action_hash(fact['envelope']) == action_hash(row):
                effective['source_version'] = fact['envelope']['source_version']; verification = fact['id']
        result = {}; error = None; status = 'processed'
        try:
            with c.begin_nested():
                digest = action_hash(row)
                duplicate = c.execute(select(s.inbox_actions).where(s.inbox_actions.c.tenant_id == actor['tenant_id'],
                    s.inbox_actions.c.connection_id == account['id'], s.inbox_actions.c.business_action_id == row['business_action_id'])).mappings().first()
                entity_key = ('stock:' if row['kind'] == 'stock.delta' else 'shipment:' if row['kind'] == 'shipment.delivered' else 'order:') + row['entity_id']
                state = c.execute(select(s.source_states).where(s.source_states.c.tenant_id == actor['tenant_id'],
                    s.source_states.c.connection_id == account['id'], s.source_states.c.entity_id == entity_key)).mappings().first()
                version = effective['source_version']
                if duplicate:
                    if duplicate['fingerprint'] != digest: fail('BUSINESS_ACTION_CONFLICT', '业务动作编号对应内容冲突')
                    status = 'duplicate'; result = {'original_inbox_id': duplicate['inbox_id']}
                elif version is None: fail('SOURCE_VERSION_UNKNOWN', '缺少来源版本，等待查询核实')
                elif state and row['kind'] != 'stock.delta' and version <= state['source_version']: status = 'stale'
                elif row['kind'] != 'stock.delta' and version != (state['source_version'] + 1 if state else 1):
                    fail('SOURCE_VERSION_GAP', '来源版本缺失，等待前序消息或核实')
                else:
                    result = apply_event(c, actor, effective, account)
                    c.execute(insert(s.inbox_actions).values(id=uid(), tenant_id=actor['tenant_id'], connection_id=account['id'],
                        business_action_id=row['business_action_id'], fingerprint=digest, inbox_id=row['id']))
                    # Stock deltas are independent numbered observations; late ones
                    # must be retained for watermark reconciliation, not discarded.
                    if row['kind'] != 'stock.delta':
                        if state: c.execute(update(s.source_states).where(s.source_states.c.id == state['id']).values(source_version=version))
                        else: c.execute(insert(s.source_states).values(id=uid(), tenant_id=actor['tenant_id'], connection_id=account['id'], entity_id=entity_key, source_version=version))
        except BusinessError as ex:
            error = ex.code
            status = 'conflict' if ex.code in ('BUSINESS_ACTION_CONFLICT', 'ORDER_CONTENT_CONFLICT', 'DELTA_SEQUENCE_CONFLICT') else 'unknown' if ex.code in ('SOURCE_VERSION_UNKNOWN', 'SOURCE_VERSION_GAP', 'ACTUAL_TIME_REQUIRED', 'SOURCE_ORDER_UNKNOWN') else 'failed'
            result = {'message': ex.message}
            anomaly(c, actor, object_type='connector_inbox', object_id=row['id'], warehouse_id=row['warehouse_id'], sku_id=row['sku_id'], rule='inbox_' + status)
        if verification: result = {**result, 'verification_id': verification, 'verified_version': effective['source_version']}
        c.execute(update(s.inbox).where(s.inbox.c.id == identity).values(status=status, error=error, result=result))
        return {**row, 'status': status, 'error': error, 'result': result}
