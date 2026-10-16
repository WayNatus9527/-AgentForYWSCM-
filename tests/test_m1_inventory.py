from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, func, insert, update
from sqlalchemy.engine import Connection
from app import db, inventory as inv, commitments as cm, events, catalog
from app.domain import BusinessError, FIELDS
from app.agent_model import QueryPlan
from test_inventory import env, actor, balance


def receipt(**changes):
    return dict(warehouse_id='wh-cn', sku_id='sku-1', expected_version=1,
                source='模拟供货', source_account='supplier-01', reference='DEL-001-A',
                g=10, q=2, d=1, note='', occurred_at=None) | changes


def post(client, path, body, key='m1-request-0001'):
    return client.post('/api/v2/' + path, json=body, headers={'Idempotency-Key': key})


def all_counts(engine):
    with engine.connect() as c:
        return {t.name: c.scalar(select(func.count()).select_from(t))
                for t in (db.documents, db.ledger, db.outbox, db.requests, inv.vouchers,
                          events.business_events, cm.grants, cm.reservations, cm.transitions)}


def authorize(engine, **changes):
    who = actor(engine)
    body = dict(warehouse_id='wh-cn', sku_id='sku-1', source_account='shop-A', reference='AUTH-001',
                action_id='authorize-001', quantity=100, expected_version=1, restore_on_cancel=True) | changes
    return cm.authorize(engine, who, body, 'grant-request-001')


def reserve(engine, grant_id, **changes):
    return cm.reserve(engine, actor(engine), grant_id,
                      dict(order_id='ORD-001', line_id='01', quantity=30, action_id='order-001',
                           grant_version=1, expected_version=2) | changes, 'reserve-request-001')


def test_receipt_request_and_voucher_idempotency_across_actors(env):
    engine, client = env
    first = post(client, 'inventory/receipts', receipt())
    assert first.status_code == 200, first.text
    doc = first.json()['document_id']
    assert post(client, 'inventory/receipts', receipt()).json()['replayed']
    again = inv.receive(engine, actor(engine, 'warehouse'), receipt(expected_version=2), 'new-key-same-voucher')
    assert again['business_replayed'] and again['document_id'] == doc
    assert balance(engine)['g'] == 338
    with engine.connect() as c:
        row = c.execute(select(events.business_events)).mappings().one()
        assert row['object_id'] == doc and row['occurred_at'] is None
        assert row['time_quality'] == 'unknown' and row['business_reference'] == 'DEL-001-A'


def test_changed_voucher_content_and_request_body_are_rejected(env):
    engine, client = env
    assert post(client, 'inventory/receipts', receipt()).status_code == 200
    before = all_counts(engine)
    r = post(client, 'inventory/receipts', receipt(g=11), 'different-request-key')
    assert r.status_code == 409 and r.json()['error']['code'] == 'VOUCHER_CONFLICT'
    r = post(client, 'inventory/receipts', receipt(g=11))
    assert r.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert all_counts(engine) == before
    assert post(client, 'inventory/receipts', receipt(reference='DEL-001-B', expected_version=2), 'real-second-batch').status_code == 200
    assert balance(engine)['g'] == 348


@pytest.mark.parametrize('change', [dict(reference=''), dict(source_account=''), dict(g=True), dict(g=-1),
                                    dict(g=0,q=0,d=0), dict(occurred_at='2026-10-15T10:00:00'), dict(run_id='B')])
def test_receipt_contract_rejects_invalid_fields(env, change):
    engine, client = env; before = all_counts(engine)
    assert post(client, 'inventory/receipts', receipt(**change)).status_code == 422
    assert all_counts(engine) == before


def test_receipt_concurrency_with_different_request_keys_posts_once(env):
    engine, _ = env; who = actor(engine); gate = Barrier(2)
    def run(n):
        gate.wait()
        return inv.receive(engine, who, receipt(), 'concurrent-voucher-' + str(n))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert results[0]['document_id'] == results[1]['document_id']
    assert sorted(x['business_replayed'] for x in results) == [False, True]
    assert balance(engine)['g'] == 338 and all_counts(engine)['receipt_vouchers'] == 1


@pytest.mark.parametrize('failure_table', [events.business_events, inv.vouchers, db.outbox])
def test_receipt_atomic_rollback_includes_new_evidence(env, monkeypatch, failure_table):
    engine, _ = env; before = all_counts(engine); stock = balance(engine)
    execute = Connection.execute
    def crash(self, statement, *args, **kwargs):
        if getattr(statement, 'table', None) is failure_table and getattr(statement, 'is_insert', False):
            raise RuntimeError('injected failure')
        return execute(self, statement, *args, **kwargs)
    monkeypatch.setattr(Connection, 'execute', crash)
    with pytest.raises(RuntimeError):
        inv.receive(engine, actor(engine), receipt(), 'atomic-voucher-001')
    assert all_counts(engine) == before and balance(engine) == stock


def test_quota_converts_without_double_count_and_cancel_respects_policy(env):
    engine, _ = env
    grant = authorize(engine)['grant']
    result = reserve(engine, grant['id']); row = result['reservation']; stock = balance(engine)
    assert (stock['g'], stock['online'], stock['r']) == (328, 70, 30)
    assert stock['g'] - stock['r'] - stock['online'] == 228
    body = dict(quantity=10, action_id='cancel-001', grant_version=2,
                reservation_version=1, expected_version=3, reason='客户取消未发部分')
    result = cm.cancel(engine, actor(engine), row['id'], body, 'cancel-request-001')
    assert result['grant']['free'] == 80 and result['reservation']['reserved'] == 20
    assert (balance(engine)['online'], balance(engine)['r'], balance(engine)['g']) == (80, 20, 328)
    again = cm.cancel(engine, actor(engine), row['id'], body | {'expected_version': 4}, 'cancel-request-002')
    assert again['business_replayed'] and again['document_id'] == result['document_id']
    with pytest.raises(BusinessError) as over:
        cm.cancel(engine, actor(engine), row['id'], body | dict(quantity=21, action_id='cancel-002', grant_version=3,
                  reservation_version=2, expected_version=4), 'cancel-over-001')
    assert over.value.code == 'OVER_CANCELLATION'


def test_no_restore_policy_releases_to_free_stock(env):
    engine, _ = env; grant = authorize(engine, restore_on_cancel=False)['grant']
    row = reserve(engine, grant['id'])['reservation']
    cm.cancel(engine, actor(engine), row['id'], dict(quantity=30, action_id='cancel-001', grant_version=2,
              reservation_version=1, expected_version=3, reason='释放未发占用'), 'release-no-restore')
    assert (balance(engine)['online'], balance(engine)['r']) == (70, 0)


def test_withdrawing_cannot_be_sold_until_confirmed(env):
    engine, _ = env; who = actor(engine); grant = authorize(engine)['grant']
    body = dict(quantity=50, grant_version=1, expected_version=2, action_id='withdraw-01', reason='撤回授权')
    started = cm.withdraw(engine, who, grant['id'], body, 'withdraw-request-001')
    assert (balance(engine)['online'], balance(engine)['withdrawing']) == (50, 50)
    assert balance(engine)['g'] - balance(engine)['online'] - balance(engine)['withdrawing'] == 228
    assert cm.withdraw(engine, who, grant['id'], body, 'withdraw-duplicate-001')['business_replayed']
    finished = cm.withdraw(engine, who, grant['id'], body | dict(grant_version=2, expected_version=3,
               action_id='confirm-01', reason='已取得模拟来源撤回确认'), 'withdraw-confirm-001', confirm=True)
    assert (balance(engine)['online'], balance(engine)['withdrawing']) == (50, 0)
    assert finished['grant']['withdrawn'] == 50


def test_order_line_business_dedup_survives_new_action_id(env):
    engine, _ = env; who = actor(engine); grant = authorize(engine)['grant']
    reserve(engine, grant['id'])
    with pytest.raises(BusinessError) as duplicate:
        cm.reserve(engine, who, grant['id'], dict(order_id='ORD-001', line_id='01', quantity=30,
            action_id='new-action', grant_version=2, expected_version=3), 'new-request-order-001')
    assert duplicate.value.code == 'ORDER_LINE_EXISTS'
    assert balance(engine)['r'] == 30


def test_competing_reservations_do_not_overbook(env):
    engine, _ = env; who = actor(engine); grant = authorize(engine, quantity=1)['grant']; gate = Barrier(2)
    def run(n):
        gate.wait()
        try:
            return cm.reserve(engine, who, grant['id'], dict(order_id='ORD-'+str(n), line_id='01', quantity=1,
                action_id='reserve-'+str(n), grant_version=1, expected_version=2), 'request-order-'+str(n))
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(isinstance(x, dict) for x in results) == 1 and 'VERSION_CONFLICT' in results
    assert (balance(engine)['online'], balance(engine)['r']) == (0, 1)


def test_new_routes_keep_auth_csrf_scope_and_no_raw_stock_write(env):
    engine, client = env
    r = client.post('/api/v2/inventory/receipts', json=receipt(), headers={'Idempotency-Key':'no-csrf-001','X-CSRF-Token':''})
    assert r.status_code == 403
    assert post(client, 'inventory/receipts', receipt(warehouse_id='wh-us')).status_code == 409
    reply = client.post('/api/v1/session', json={'username':'cashier','password':'test-password'})
    client.headers['X-CSRF-Token'] = reply.json()['csrf']
    assert post(client, 'inventory/receipts', receipt()).status_code == 403
    assert client.get('/api/v2/events').status_code == 403
    assert client.get('/api/v2/commitments/grants').status_code == 403


def test_ledger_records_all_quota_changes_and_keeps_sale_return_quantity(env):
    engine, client = env
    with db.write(engine) as c:
        c.execute(update(db.balances).where(db.balances.c.warehouse_id=='wh-cn',db.balances.c.sku_id=='sku-1').values(offline=5))
    reply = client.post('/api/v1/sales/confirm', json=dict(warehouse_id='wh-cn',sku_id='sku-1',
                        expected_version=1,source='模拟顾客',quantity=3),headers={'Idempotency-Key':'legacy-sale-001'})
    assert reply.status_code == 200
    with engine.connect() as c:
        entry = c.execute(select(db.ledger).where(db.ledger.c.document_id==reply.json()['document_id'])).mappings().one()
        assert entry['delta']['offline'] == -3 and entry['delta']['g'] == -3
        assert c.scalar(select(db.documents.c.quantity).where(db.documents.c.id==reply.json()['document_id'])) == 3


def test_reported_occurrence_preserves_utc_and_unknown_is_not_invented(env):
    engine, client = env
    response = post(client, 'inventory/receipts', receipt(occurred_at='2026-10-13T12:30:00+08:00'))
    assert response.status_code == 200
    events_reply = client.get('/api/v2/events?warehouse_id=wh-cn&limit=1').json()
    assert events_reply['total'] == 1
    assert events_reply['items'][0]['occurred_at'] == '2026-10-13T04:30:00+00:00'
    assert events_reply['items'][0]['time_quality'] == 'reported'
    assert client.get('/api/v2/events?limit=0').status_code == 422


def test_agent_does_not_label_unknown_or_inactive_as_low_stock(env):
    engine, _ = env; who = actor(engine)
    from app import agent_queries
    catalog.set_sku(engine, who, 'sku-6', {'active':False,'expected_version':0}, 'disable-low-sku-001')
    result = agent_queries.execute(engine, who, QueryPlan(intent='issues',sku_text='',warehouse_text='',issue='low_stock'), 'wh-cn', 'sku-6')
    assert not any(x['type']=='low_stock' for x in result['issues'])


def test_default_profile_blocks_new_voucherless_receipts_but_replays_old_requests(env):
    from dataclasses import replace
    from test_inventory import body
    engine,client=env
    first=client.post('/api/v1/receipts/confirm',json=body(),headers={'Idempotency-Key':'old-client-receipt'})
    assert first.status_code==200
    engine.runtime_settings=replace(engine.runtime_settings,allow_legacy_receipts=False)
    replay=client.post('/api/v1/receipts/confirm',json=body(),headers={'Idempotency-Key':'old-client-receipt'})
    assert replay.status_code==200 and replay.json()['replayed']
    before=all_counts(engine)
    new=client.post('/api/v1/receipts/confirm',json=body(2),headers={'Idempotency-Key':'new-voucherless-receipt'})
    assert new.status_code==422 and new.json()['error']['code']=='RECEIPT_REFERENCE_REQUIRED'
    assert all_counts(engine)==before
    assert post(client,'inventory/receipts',receipt(expected_version=2)).status_code==200


def test_legacy_receipt_compatibility_is_explicitly_opt_in():
    from app.config import load_settings
    assert not load_settings({}).allow_legacy_receipts
    assert load_settings({'APP_ALLOW_LEGACY_RECEIPTS':'true'}).allow_legacy_receipts
    with pytest.raises(ValueError):
        load_settings({'APP_RUNTIME_MODE':'production','APP_RUN_ID':'prod','APP_ALLOW_LEGACY_RECEIPTS':'true'})
