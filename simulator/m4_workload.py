"""Runs M4 contracts inside the OS-restricted business process."""
import time
from sqlalchemy import select, func
from app import db, m4_schema as s, connectors as cn, fulfillment as f, delivery as dl, reconciliation as rc, commitments
from app.domain import BusinessError


def execute(driver, masters, case):
    engine = driver.engine; trace = []
    def check(name, value):
        trace.append(dict(name=name, passed=bool(value)))
        if not value: raise AssertionError(name)
    for master in masters:
        a = driver.actor(master['tenant_id']); w = master['warehouses'][0]['id']; sku = master['skus'][0]['id']
        account = cn.create(engine, a, dict(source_account='shared-channel', name='模拟订单渠道', warehouse_ids=[w], member_ids=[],
            supports_query=case['fault'] != 'no_query_timeout'), 'm4-connection')
        body = dict(connection_id=account['id'], source_order_id='shared-order', warehouse_ids=[w],
            lines=[dict(source_line_id='1', sku_id=sku, quantity=case['quantity'])])
        def envelope(event_id, kind, version, payload, entity='shared-order'):
            return dict(run_id=engine.runtime_settings.run_id, tenant_id=a['tenant_id'], source_event_id=event_id,
                business_action_id=event_id, entity_id=entity, source_version=version, kind=kind,
                occurred_at='2026-10-02T01:00:00+00:00', payload=payload)
        event_body = envelope('source-order', 'order.created', 1, body)
        message = cn.receive(engine, a, account['id'], event_body)
        check(a['id'] + ':inbox-durable-before-effect', message['status'] == 'received' and not f.listing(engine, a)['items'])
        processed = cn.process(engine, a, message['id']); r = processed['result']
        check(a['id'] + ':received-order', processed['status'] == 'processed')
        check(a['id'] + ':event-retry', cn.receive(engine, a, account['id'], event_body)['replayed'])
        duplicate = cn.receive(engine, a, account['id'], {**event_body, 'source_event_id': 'duplicate-delivery'})
        check(a['id'] + ':business-duplicate', cn.process(engine, a, duplicate['id'])['status'] == 'duplicate')
        def act(row, action, **extra):
            key = 'm4-' + action + '-' + str(row['version'])
            return f.act(engine, a, row['id'], action, dict(expected_version=row['version'], action_id=key, reason='独立模拟业务执行', **extra), key)
        r = act(r, 'confirm'); grant_id = None
        if case['grant']:
            grant_id = commitments.authorize(engine, a, dict(warehouse_id=w, sku_id=sku, quantity=case['quantity'] + 7,
                source_account='shared-channel', reference='shared-grant', action_id='authorize-grant', restore_on_cancel=case['restore'], expected_version=1), 'm4-grant')['grant']['id']
        r = act(r, 'allocate', line_id=r['lines'][0]['id'], quantity=case['quantity'], warehouse_id=w, grant_id=grant_id)
        for kind in ('pick', 'review'):
            for action in ('start', 'complete'): r = act(r, action, batch_id=r['batches'][0]['id'], kind=kind)
        ship_body = dict(expected_version=r['version'], action_id='ship-first', reason='模拟实物交接', batch_id=r['batches'][0]['id'],
            quantity=case['shipped'], reference='first-handover', occurred_at='2026-10-02T02:00:00+00:00')
        r = f.act(engine, a, r['id'], 'ship', ship_body, 'm4-ship-first')
        check(a['id'] + ':shipment-business-dedup', f.act(engine, a, r['id'], 'ship', ship_body, 'm4-shipment-new-request')['business_replayed'])
        if case['inbox_cancel']:
            incoming = cn.receive(engine, a, account['id'], envelope('cancel-source', 'order.cancelled', 2,
                dict(source_line_id='1', quantity=case['cancelled'], reason='来源取消未发部分')))
            result = cn.process(engine, a, incoming['id']); check(a['id'] + ':incoming-cancel', result['status'] == 'processed'); r = result['result']
        else: r = act(r, 'cancel', line_id=r['lines'][0]['id'], quantity=case['cancelled'])
        if case['finish']:
            r = act(r, 'ship', batch_id=r['batches'][0]['id'], quantity=case['remaining'], reference='last-handover', occurred_at='2026-10-02T03:00:00+00:00')
            incoming = cn.receive(engine, a, account['id'], envelope('complete-source', 'order.completed', 3 if case['inbox_cancel'] else 2, {}))
            check(a['id'] + ':separate-channel-completion', cn.process(engine, a, incoming['id'])['status'] == 'processed')
        check(a['id'] + ':partial-quantities', r['lines'][0]['cancelled'] == case['cancelled'] and r['lines'][0]['shipped'] == case['shipped'] + (case['remaining'] if case['finish'] else 0))
        try:
            cn.receive(engine, a, account['id'], {**event_body, 'run_id': 'foreign-run'})
        except BusinessError as ex: check(a['id'] + ':cross-run-rejected', ex.code == 'RUNTIME_MISMATCH')
        else: check(a['id'] + ':cross-run-rejected', False)
        # These are synthetic source facts, not read-backs of the local balance.
        observed = master['skus'][0]['opening'] - case['shipped'] - (case['remaining'] if case['finish'] else 0)
        snapshot_body = dict(snapshot_id='shared-snapshot', warehouse_id=w, sku_id=sku,
            covered_watermark=None if case['snapshot_order'] == 'no_watermark' else 10,
            snapshot_at='2026-10-02T04:00:00+00:00', quantities=dict(g=observed, q=0, d=0))
        def delta():
            incoming = cn.receive(engine, a, account['id'], envelope('late-stock', 'stock.delta', 9,
                dict(warehouse_id=w, sku_id=sku, sequence=9, g=-case['shipped']), entity='stock:' + w + ':' + sku))
            check(a['id'] + ':late-delta-accepted', cn.process(engine, a, incoming['id'])['status'] == 'processed')
        if case['snapshot_order'] == 'delta_first': delta()
        snap = rc.snapshot(engine, a, account['id'], snapshot_body, 'm4-snapshot')
        if case['snapshot_order'] != 'delta_first': delta()
        comparison = rc.compare(engine, a, snap['id'], 'm4-compare')
        check(a['id'] + ':watermark-result', comparison['status'] == ('unknown' if case['snapshot_order'] == 'no_watermark' else 'matched'))
        if case['fault'] != 'worker_crash':
            wall = [time.time()]
            fault = 'timeout_after' if case['fault'] == 'no_query_timeout' else case['fault']
            adapter = dl.SimulatedAdapter(engine, a, [] if fault == 'success' else [fault])
            for _ in range(10):
                result = dl.tick(engine, a, adapter=adapter, clock=lambda: wall[0]); wall[0] += 400
                if result['status'] == 'idle': break
    if case['fault'] != 'worker_crash': trace.extend(delivery_checks(driver, masters, case))
    with engine.connect() as c:
        try: f.get(c, driver.actor(masters[0]['tenant_id']), r['id'])
        except BusinessError as ex: check('cross-tenant-order-hidden', ex.code == 'NOT_FOUND')
        else: check('cross-tenant-order-hidden', False)
    snapshot = driver.snapshot()
    check('ledger-atomic-links', snapshot['ledger_consistent'])
    return dict(trace=trace, snapshot=snapshot)


def delivery_checks(driver, masters, case):
    trace = []
    with driver.engine.connect() as c:
        for master in masters:
            tenant = master['tenant_id']; jobs = c.execute(select(s.outbox).where(s.outbox.c.tenant_id == tenant)).mappings().all()
            effects = c.scalar(select(func.count()).select_from(s.effects).where(s.effects.c.tenant_id == tenant))
            expected = 1 if case['fault'] == 'no_query_timeout' else 1 + int(case['finish'])
            trace.append(dict(name=tenant + ':external-effects-once', passed=effects == expected))
            trace.append(dict(name=tenant + ':delivery-state', passed=(any(x['status'] == 'manual' for x in jobs) if case['fault'] == 'no_query_timeout' else all(x['status'] == 'succeeded' for x in jobs))))
    return trace


def recover(driver, masters, case):
    for master in masters:
        actor = driver.actor(master['tenant_id'])
        for _ in range(5):
            if dl.tick(driver.engine, actor)['status'] == 'idle': break
    return dict(trace=delivery_checks(driver, masters, case), snapshot=driver.snapshot())
