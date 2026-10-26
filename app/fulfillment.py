"""Sales and physical fulfillment through the shared inventory transaction boundary."""
from sqlalchemy import select, insert, update, func
from . import db, m4_schema as s, m4_models as models, commitments, inventory as inv, catalog
from .domain import require_role, uid, now, unallocated, BusinessError
from .m3_common import fail
from .m4_common import connection, scope, command, validate, record, anomaly, READ_ROLES, ORDER_ROLES
from .clock import business_now


def get(c, actor, identity):
    row = c.execute(select(s.orders).where(s.orders.c.id == identity, s.orders.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row:
        fail('NOT_FOUND', '销售订单不存在或未授权', 404)
    account = connection(c, actor, row['connection_id'])
    for warehouse in row['warehouse_ids']:
        scope(c, actor, account, warehouse)
    return dict(row)


def children(c, table, order_id):
    return [dict(x) for x in c.execute(select(table).where(table.c.order_id == order_id).order_by(table.c.id)).mappings()]


def view(c, actor, identity):
    row = get(c, actor, identity)
    row['source_version'] = c.scalar(select(s.source_states.c.source_version).where(s.source_states.c.tenant_id == actor['tenant_id'],
        s.source_states.c.connection_id == row['connection_id'], s.source_states.c.entity_id == 'order:' + row['source_order_id']))
    for key, table in [('lines', s.lines), ('batches', s.batches), ('tasks', s.tasks), ('shipments', s.shipments), ('supply_links', s.links)]:
        row[key] = children(c, table, identity)
    row['history'] = [dict(x) for x in c.execute(select(s.history).where(s.history.c.order_id == identity).order_by(s.history.c.created_at, s.history.c.id)).mappings()]
    return row


def listing(engine, actor, offset=0, limit=50):
    require_role(actor, *READ_ROLES)
    from .accounts import assert_current
    with engine.connect() as c:
        assert_current(c, actor)
        # Authorize the lightweight index in batches. Fetching every full order
        # before slicing made a 20-row page issue tens of thousands of queries.
        accounts={r['id']:dict(r) for r in c.execute(select(s.connections).where(s.connections.c.tenant_id==actor['tenant_id'])).mappings()
            if r['active'] and (actor['role']=='admin' or actor['id'] in r['member_ids'])}
        from . import db
        warehouses={r['id']:dict(r) for r in c.execute(select(db.warehouses).where(db.warehouses.c.tenant_id==actor['tenant_id'])).mappings()
            if actor['role']=='admin' or r['id'] in actor['warehouse_ids']}
        ids = c.execute(select(s.orders.c.id,s.orders.c.connection_id,s.orders.c.warehouse_ids).where(s.orders.c.tenant_id == actor['tenant_id']).order_by(s.orders.c.created_at.desc(), s.orders.c.id)).mappings()
        visible = []
        for row in ids:
            account=accounts.get(row['connection_id'])
            if not account or not set(row['warehouse_ids'])<=set(account['warehouse_ids']) or not set(row['warehouse_ids'])<=warehouses.keys():continue
            if any(warehouses[w]['authority']!='local' for w in row['warehouse_ids']):fail('READ_ONLY_WAREHOUSE','该仓仅有人工参考数据')
            visible.append(row['id'])
        return dict(items=[view(c, actor, x) for x in visible[offset:offset + limit]], offset=offset, limit=limit,
                    total=len(visible), has_more=len(visible) > offset + limit)


def create_in_transaction(c, actor, body, source=None):
    body = validate(models.OrderCreate, body)
    account = connection(c, actor, body['connection_id'])
    for warehouse in body['warehouse_ids']:
        for line in body['lines']: scope(c, actor, account, warehouse, line['sku_id'])
    digest = inv.fingerprint(body)
    old = c.execute(select(s.orders).where(s.orders.c.tenant_id == actor['tenant_id'], s.orders.c.connection_id == account['id'],
        s.orders.c.source_order_id == body['source_order_id'])).mappings().first()
    if old:
        if old['fingerprint'] != digest: fail('ORDER_CONTENT_CONFLICT', '来源订单号已用于不同内容')
        return {**view(c, actor, old['id']), 'business_replayed': True}
    row = dict(id=uid(), tenant_id=actor['tenant_id'], connection_id=account['id'], source_order_id=body['source_order_id'],
        warehouse_ids=body['warehouse_ids'], status='pending_confirmation', version=1, fingerprint=digest,
        planned_at=body['planned_at'], occurred_at=body['occurred_at'], channel_completed_at=None, created_at=now())
    c.execute(insert(s.orders).values(**row))
    for line in body['lines']:
        c.execute(insert(s.lines).values(id=uid(), tenant_id=actor['tenant_id'], order_id=row['id'], **line, reserved=0, shipped=0, cancelled=0))
    record(c, actor, row, 'created', body, lifecycle='queued', observed=False, occurred_at=body['occurred_at'], source=source)
    return view(c, actor, row['id'])


def create(engine, actor, body, key):
    require_role(actor, *ORDER_ROLES)
    body = validate(models.OrderCreate, body)
    def authorize(c):
        account = connection(c, actor, body['connection_id'])
        for warehouse in body['warehouse_ids']:
            for line in body['lines']: scope(c, actor, account, warehouse, line['sku_id'])
    return command(engine, actor, 'orders.create', body, key, lambda c: create_in_transaction(c, actor, body), authorize)


def item(c, table, order, identity):
    row = c.execute(select(table).where(table.c.id == identity, table.c.order_id == order['id'])).mappings().first()
    if not row: fail('NOT_FOUND', '订单行、批次或依赖不存在', 404)
    return dict(row)


def save(c, table, row):
    c.execute(update(table).where(table.c.id == row['id']).values(**row))


def refresh(c, order):
    rows = children(c, s.lines, order['id'])
    quantity = sum(x['quantity'] for x in rows)
    cancelled, shipped, reserved = (sum(x[k] for x in rows) for k in ('cancelled', 'shipped', 'reserved'))
    if cancelled == quantity: status = 'cancelled'
    elif shipped + cancelled == quantity: status = 'completed' if order['channel_completed_at'] else 'shipped'
    elif shipped: status = 'partially_shipped'
    elif order['status'] == 'pending_confirmation': status = 'pending_confirmation'
    elif any(t['status'] in ('started', 'completed') for t in children(c, s.tasks, order['id'])): status = 'processing'
    elif reserved + cancelled == quantity: status = 'reserved'
    else: status = 'pending_allocation'
    order.update(status=status, version=order['version'] + 1)
    save(c, s.orders, order)


def stock_known(c, actor, warehouse, sku):
    row = c.execute(select(catalog.assortments).where(catalog.assortments.c.tenant_id == actor['tenant_id'],
        catalog.assortments.c.warehouse_id == warehouse, catalog.assortments.c.sku_id == sku)).mappings().first()
    return bool(row and row['inventory_known'])


def allocate(c, actor, order, body):
    line = item(c, s.lines, order, body['line_id'])
    active_sku = c.scalar(select(catalog.sku_settings.c.active).where(catalog.sku_settings.c.sku_id == line['sku_id'], catalog.sku_settings.c.tenant_id == actor['tenant_id']))
    if active_sku == 0: fail('SKU_INACTIVE', '已停用商品不可新增分配')
    if order['status'] in ('pending_confirmation', 'cancelled', 'shipped', 'completed'):
        fail('INVALID_STATE', '请先确认订单，已结束订单不可分配')
    n = body['quantity']
    if n > line['quantity'] - line['reserved'] - line['shipped'] - line['cancelled']:
        fail('OVER_ALLOCATION', '分配数量超过订单行未分配量')
    warehouses = [body['warehouse_id']] if body['warehouse_id'] else order['warehouse_ids']
    if any(w not in order['warehouse_ids'] for w in warehouses): fail('INVALID_SCOPE', '分配仓库不在订单约定顺序内')
    if body['grant_id'] and not body['warehouse_id']: fail('INVALID_INPUT', '渠道额度分配须指定仓库', 422)
    account = connection(c, actor, order['connection_id'])
    plan = []; left = n; unknown = False
    for warehouse in warehouses:
        scope(c, actor, account, warehouse, line['sku_id'])
        if c.scalar(select(catalog.assortments.c.active).where(catalog.assortments.c.tenant_id == actor['tenant_id'],
            catalog.assortments.c.warehouse_id == warehouse, catalog.assortments.c.sku_id == line['sku_id'])) == 0:
            fail('ASSORTMENT_INACTIVE', '该仓库商品经营关系已停用')
        if not stock_known(c, actor, warehouse, line['sku_id']):
            unknown = True; continue
        stock = inv.load_balance(c, actor, warehouse, line['sku_id'])
        grant = None
        if body['grant_id']:
            grant = commitments._get(c, commitments.grants, actor, body['grant_id'])
            if (grant['source_account'], grant['warehouse_id'], grant['sku_id']) != (account['source_account'], warehouse, line['sku_id']):
                fail('GRANT_SCOPE_MISMATCH', '渠道额度与订单来源、仓库或商品不一致')
        take = min(left, grant['free'] if grant else max(0, unallocated(stock)))
        if take: plan.append((warehouse, stock, grant, take)); left -= take
        if not left: break
    if left:
        task = anomaly(c, actor, object_type='sales_order', object_id=order['id'], warehouse_id=warehouses[0], sku_id=line['sku_id'],
            rule='inventory_unknown' if unknown else 'shortage', quantity=left, instance=line['id'])
        record(c, actor, order, 'allocation_blocked', {**body, 'task_id': task['id'], 'unknown': unknown}, line=line, quantity=left)
        return {'allocation_result': 'unknown' if unknown else 'shortage', 'task_id': task['id']}
    for warehouse, stock, grant, take in plan:
        batch = dict(id=uid(), tenant_id=actor['tenant_id'], order_id=order['id'], line_id=line['id'], warehouse_id=warehouse,
            sku_id=line['sku_id'], commitment_id=None, quantity=take, reserved=take, shipped=0, cancelled=0, created_at=now())
        if grant:
            reservation = commitments.reserve_in_transaction(c, actor, grant['id'], dict(quantity=take,
                order_id=order['source_order_id'], line_id=batch['id'], action_id='m4-reserve-' + batch['id'],
                expected_version=stock['version'], grant_version=grant['version']))
            batch['commitment_id'] = reservation['reservation']['id']
        else:
            inv.movement(c, actor, stock, {'r': take}, 'order_reserve', order['id'], body['reason'], stock['version'], business_reference=body['action_id'])
        c.execute(insert(s.batches).values(**batch))
        for kind in ('pick', 'review'):
            c.execute(insert(s.tasks).values(id=uid(), tenant_id=actor['tenant_id'], order_id=order['id'], batch_id=batch['id'],
                kind=kind, status='queued' if kind == 'pick' else 'waiting', quantity=take, queued_at=business_now(c.engine)))
        record(c, actor, order, 'allocated', body, line=line, batch=batch, quantity=take, lifecycle='queued')
    line['reserved'] += n; save(c, s.lines, line)
    return {'allocation_result': 'reserved'}


def task_action(c, actor, order, body, action):
    batch = item(c, s.batches, order, body['batch_id'])
    if not batch['reserved']: fail('INVALID_STATE', '该批次已无待发数量')
    task = dict(c.execute(select(s.tasks).where(s.tasks.c.batch_id == batch['id'], s.tasks.c.kind == body['kind'])).mappings().one())
    expected = 'queued' if action == 'start' else 'started'
    if task['status'] != expected: fail('INVALID_STATE', '请依次开始并完成拣货和复核')
    task.update(status='started' if action == 'start' else 'completed', actor_id=actor['id'])
    task['started_at' if action == 'start' else 'completed_at'] = business_now(c.engine)
    task['quantity'] = batch['reserved']
    save(c, s.tasks, task)
    line = item(c, s.lines, order, batch['line_id'])
    record(c, actor, order, body['kind'] + '_' + action, body, line=line, batch=batch, quantity=batch['reserved'],
        lifecycle='started' if action == 'start' else 'completed', task_id=task['id'])
    if body['kind'] == 'pick' and action == 'complete':
        c.execute(update(s.tasks).where(s.tasks.c.batch_id == batch['id'], s.tasks.c.kind == 'review').values(status='queued', queued_at=business_now(c.engine)))
        record(c, actor, order, 'review_queued', body, line=line, batch=batch, quantity=batch['reserved'], lifecycle='queued')


def release_links(c, actor, order, line, reason):
    links = [x for x in children(c, s.links, order['id']) if x['line_id'] == line['id']]
    excess = sum(x['quantity'] - x['released'] for x in links) - (line['quantity'] - line['shipped'] - line['cancelled'])
    for link in reversed(links):
        take = min(max(0, excess), link['quantity'] - link['released'])
        if not take: continue
        link['released'] += take; save(c, s.links, link); excess -= take
        record(c, actor, order, 'supply_released', {'link_id': link['id'], 'reason': reason}, line=line, quantity=take,
            lifecycle='cancelled', dependencies=[{'type': link['source_type'], 'id': link['source_id'], 'quantity': take}])


def cancel(c, actor, order, body):
    line = item(c, s.lines, order, body['line_id']); n = body['quantity']
    if n > line['quantity'] - line['shipped'] - line['cancelled']: fail('OVER_CANCELLATION', '只能取消未发出且未取消的数量')
    available = line['quantity'] - line['reserved'] - line['shipped'] - line['cancelled']
    left = n if body.get('batch_id') else max(0, n - available)
    batches = [item(c, s.batches, order, body['batch_id'])] if body.get('batch_id') else children(c, s.batches, order['id'])
    if body.get('batch_id') and (batches[0]['line_id'] != line['id'] or n > batches[0]['reserved']):
        fail('OVER_CANCELLATION', '批次与行不匹配或取消超过批次待发量')
    for batch in batches:
        if batch['line_id'] != line['id'] or not left: continue
        take = min(left, batch['reserved'])
        if not take: continue
        stock = inv.load_balance(c, actor, batch['warehouse_id'], batch['sku_id'])
        if batch['commitment_id']:
            held = commitments._get(c, commitments.reservations, actor, batch['commitment_id'])
            grant = commitments._get(c, commitments.grants, actor, held['grant_id'])
            commitments.cancel_in_transaction(c, actor, held['id'], dict(quantity=take, action_id='m4-cancel-' + uid(), reason=body['reason'],
                expected_version=stock['version'], grant_version=grant['version'], reservation_version=held['version']), managed=True)
        else:
            inv.movement(c, actor, stock, {'r': -take}, 'order_cancel', order['id'], body['reason'], stock['version'], business_reference=body['action_id'])
        batch['reserved'] -= take; batch['cancelled'] += take; line['reserved'] -= take; left -= take
        save(c, s.batches, batch)
        if not batch['reserved']:
            c.execute(update(s.tasks).where(s.tasks.c.batch_id == batch['id'], s.tasks.c.status.in_(('waiting', 'queued', 'started'))).values(status='cancelled'))
        record(c, actor, order, 'batch_cancelled', body, line=line, batch=batch, quantity=take, lifecycle='cancelled')
    if left: fail('COMMITMENT_CONFLICT', '订单占用与履约批次不一致，须先核实')
    line['cancelled'] += n; save(c, s.lines, line)
    release_links(c, actor, order, line, 'order_cancelled')
    record(c, actor, order, 'cancelled', body, line=line, quantity=n, lifecycle='cancelled')
    return {}


def ship(c, actor, order, body):
    batch = item(c, s.batches, order, body['batch_id']); n = body['quantity']
    if n > batch['reserved']: fail('OVER_SHIPMENT', '交接量超过批次待发数量')
    task = c.execute(select(s.tasks).where(s.tasks.c.batch_id == batch['id'], s.tasks.c.kind == 'review')).mappings().one()
    if task['status'] != 'completed': fail('REVIEW_REQUIRED', '须先完成拣货与复核')
    old = c.execute(select(s.shipments).where(s.shipments.c.tenant_id == actor['tenant_id'], s.shipments.c.order_id == order['id'], s.shipments.c.reference == body['reference'])).mappings().first()
    if old: fail('SHIPMENT_REFERENCE_EXISTS', '该交接凭证已使用，请查看原批次')
    stock = inv.load_balance(c, actor, batch['warehouse_id'], batch['sku_id'])
    doc = inv.movement(c, actor, stock, {'g': -n, 'r': -n}, 'sale', order['id'], body['reason'], stock['version'],
        occurred_at=body['occurred_at'], business_reference=body['reference'])
    if batch['commitment_id']:
        held = commitments._get(c, commitments.reservations, actor, batch['commitment_id'])
        if n > held['reserved']: fail('COMMITMENT_CONFLICT', '订单占用与批次数量不一致')
        held.update(reserved=held['reserved'] - n, fulfilled=held['fulfilled'] + n, version=held['version'] + 1)
        save(c, commitments.reservations, held)
    batch['reserved'] -= n; batch['shipped'] += n; save(c, s.batches, batch)
    line = item(c, s.lines, order, batch['line_id'])
    line['reserved'] -= n; line['shipped'] += n; save(c, s.lines, line)
    shipment = dict(id=uid(), tenant_id=actor['tenant_id'], order_id=order['id'], batch_id=batch['id'], warehouse_id=batch['warehouse_id'],
        sku_id=batch['sku_id'], quantity=n, document_id=doc, reference=body['reference'], action_id=body['action_id'],
        fingerprint=inv.fingerprint({k: v for k, v in body.items() if k not in ('expected_version', 'action_id')}),
        occurred_at=body['occurred_at'], delivered_at=None, created_at=now())
    c.execute(insert(s.shipments).values(**shipment))
    from .delivery import enqueue
    job = enqueue(c, actor, order['connection_id'], order['id'], 'shipment.notify',
        dict(shipment_id=shipment['id'], source_order_id=order['source_order_id'], quantity=n, reference=body['reference']), batch['warehouse_id'], batch['sku_id'])
    record(c, actor, order, 'shipped', {**body, 'document_id': doc, 'shipment_id': shipment['id'], 'outbox_id': job['id']},
        line=line, batch=batch, quantity=n, lifecycle='completed', observed=False, occurred_at=body['occurred_at'])
    release_links(c, actor, order, line, 'demand_shipped')
    return {'shipment_id': shipment['id'], 'document_id': doc, 'outbox_id': job['id']}


def supply(c, actor, order, body, release=False):
    if release:
        link = item(c, s.links, order, body['link_id']); line = item(c, s.lines, order, link['line_id'])
        if body['quantity'] > link['quantity'] - link['released']: fail('OVER_RELEASE', '解除数量超过当前关联量')
        link['released'] += body['quantity']; save(c, s.links, link)
    else:
        from . import workflows as w, operations as op, quality_control as qc
        line = item(c, s.lines, order, body['line_id']); kind = body['source_type']
        if kind == 'purchase_line':
            source = c.execute(select(w.lines).where(w.lines.c.id == body['source_id'])).mappings().first()
            parent = w.get_order(c, actor, source['order_id']) if source else None
            capacity = source['received'] if parent and parent['status'] == 'closed' else source['ordered'] if source else 0
            warehouse = parent['warehouse_id'] if parent else None
        elif kind == 'transfer_line':
            source = c.execute(select(op.transfer_lines).where(op.transfer_lines.c.id == body['source_id'])).mappings().first()
            parent = c.execute(select(op.transfers).where(op.transfers.c.id == source['transfer_id'], op.transfers.c.tenant_id == actor['tenant_id'])).mappings().first() if source else None
            warehouse = parent['destination_id'] if parent else None; capacity = source['quantity'] if source and parent and parent['status'] != 'cancelled' else 0
        else:
            source = c.execute(select(qc.sessions).where(qc.sessions.c.id == body['source_id'], qc.sessions.c.tenant_id == actor['tenant_id'])).mappings().first()
            warehouse = source['warehouse_id'] if source else None
            capacity = (source['quantity'] if source['status'] == 'started' else source['good']) if source and source['status'] != 'cancelled' else 0
        if source and kind in ('purchase_line','transfer_line'):
            from .m5_schema import lines as supply_lines, batches as supply_batches
            planned=c.execute(select(supply_lines).where(supply_lines.c.source_line_id==source['id'],supply_lines.c.tenant_id==actor['tenant_id'],supply_lines.c.kind==('purchase' if kind=='purchase_line' else 'transfer'))).mappings().first()
            if planned:
                lost=c.scalar(select(func.coalesce(func.sum(supply_batches.c.lost),0)).where(supply_batches.c.supply_line_id==planned['id']))
                capacity=planned['quantity']-planned['closed_quantity']-lost
        if not source or warehouse not in order['warehouse_ids'] or source['sku_id'] != line['sku_id']: fail('INVALID_SUPPLY', '供给与订单商品或授权仓库不匹配')
        if kind == 'transfer_line' and not parent: fail('INVALID_SUPPLY', '供给不存在')
        scoped = connection(c, actor, order['connection_id']); scope(c, actor, scoped, warehouse, line['sku_id'])
        all_links = c.execute(select(s.links).where(s.links.c.tenant_id == actor['tenant_id'])).mappings().all()
        used = sum(x['quantity'] - x['released'] for x in all_links if x['source_type'] == kind and x['source_id'] == body['source_id'])
        demand = sum(x['quantity'] - x['released'] for x in all_links if x['line_id'] == line['id'])
        if used + body['quantity'] > capacity or demand + body['quantity'] > line['quantity'] - line['shipped'] - line['cancelled']:
            fail('OVER_SUPPLY_LINK', '关联量超过剩余供给或未履约需求')
        link = dict(id=uid(), tenant_id=actor['tenant_id'], order_id=order['id'], line_id=line['id'], source_type=kind, source_id=body['source_id'],
            warehouse_id=warehouse, sku_id=line['sku_id'], quantity=body['quantity'], released=0, created_at=now())
        c.execute(insert(s.links).values(**link))
    record(c, actor, order, 'supply_released' if release else 'supply_linked', {**body, 'link_id': link['id']}, line=line,
        quantity=body['quantity'], lifecycle='cancelled' if release else 'registered',
        dependencies=[{'type': link['source_type'], 'id': link['source_id'], 'quantity': body['quantity']}])
    return {'link_id': link['id']}


CONTRACTS = {'confirm': models.Action, 'allocate': models.Allocate, 'cancel': models.Cancel, 'ship': models.Ship,
    'start': models.TaskAction, 'complete': models.TaskAction, 'link': models.Supply, 'release': models.Release}


def act_in_transaction(c, actor, identity, action, body):
    body = validate(CONTRACTS[action], body)
    order = get(c, actor, identity)
    digest = inv.fingerprint({'order_id': identity, 'action': action, 'body': {k: v for k, v in body.items() if k != 'expected_version'}})
    old = c.execute(select(s.actions).where(s.actions.c.tenant_id == actor['tenant_id'], s.actions.c.connection_id == order['connection_id'], s.actions.c.action_id == body['action_id'])).mappings().first()
    if old:
        if old['fingerprint'] != digest: fail('BUSINESS_ACTION_CONFLICT', '业务动作编号已用于其他内容')
        return {**old['response'], 'business_replayed': True}
    if action == 'ship':
        shipped = c.execute(select(s.shipments).where(s.shipments.c.order_id == identity, s.shipments.c.reference == body['reference'])).mappings().first()
        if shipped:
            if shipped['fingerprint'] != inv.fingerprint({k: v for k, v in body.items() if k not in ('expected_version', 'action_id')}):
                fail('SHIPMENT_REFERENCE_CONFLICT', '交接凭证内容与原记录不一致')
            response = c.scalar(select(s.actions.c.response).where(s.actions.c.connection_id == order['connection_id'], s.actions.c.action_id == shipped['action_id']))
            return {**response, 'business_replayed': True}
    if body['expected_version'] != order['version']: fail('VERSION_CONFLICT', '订单已变化，请刷新后核对')
    extra = {}
    if action == 'confirm':
        if order['status'] != 'pending_confirmation': fail('INVALID_STATE', '订单已确认或已取消')
        order['status'] = 'pending_allocation'; record(c, actor, order, 'confirmed', body, lifecycle='queued')
    elif action == 'allocate': extra = allocate(c, actor, order, body)
    elif action == 'cancel': extra = cancel(c, actor, order, body)
    elif action == 'ship': extra = ship(c, actor, order, body)
    elif action in ('start', 'complete'): task_action(c, actor, order, body, action)
    else: extra = supply(c, actor, order, body, release=action == 'release')
    refresh(c, order)
    result = {**view(c, actor, identity), **extra}
    c.execute(insert(s.actions).values(id=uid(), tenant_id=actor['tenant_id'], connection_id=order['connection_id'], action_id=body['action_id'],
        fingerprint=digest, response=result, created_at=now()))
    return result


def act(engine, actor, identity, action, body, key):
    require_role(actor, *(('admin', 'warehouse') if action in ('ship', 'start', 'complete') else ORDER_ROLES))
    if action not in CONTRACTS: fail('INVALID_ACTION', '不支持此订单动作', 422)
    body = validate(CONTRACTS[action], body)
    return command(engine, actor, 'orders.' + action + ':' + identity, body, key,
        lambda c: act_in_transaction(c, actor, identity, action, body), lambda c: get(c, actor, identity))
