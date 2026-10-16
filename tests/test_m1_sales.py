"""Shipment vouchers prevent double posting across request keys and operators."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import select, func, update
from sqlalchemy.engine import Connection

from app import db, events, inventory as inv
from app.config import load_settings
from app.domain import BusinessError
from test_inventory import env, actor, balance


def sale(**changes):
    return dict(warehouse_id='wh-cn', sku_id='sku-1', expected_version=1,
                source='模拟门店订单', source_account='store-yiwu-01', reference='SHIP-001-A',
                quantity=3, note='', occurred_at=None) | changes


def post(client, body=None, key='shipment-request-001'):
    return client.post('/api/v2/inventory/sales', json=sale() if body is None else body,
                       headers={'Idempotency-Key': key})


def counts(engine):
    with engine.connect() as c:
        return {t.name: c.scalar(select(func.count()).select_from(t))
                for t in (db.documents, db.ledger, db.outbox, db.requests,
                          inv.shipment_vouchers, events.business_events)}


def test_shipment_replays_across_requests_actors_and_inventory_versions(env):
    engine, client = env
    first = post(client)
    assert first.status_code == 200, first.text
    doc = first.json()['document_id']
    assert first.json()['quantity'] == 3
    assert post(client).json()['replayed']
    replay = inv.sell(engine, actor(engine, 'cashier'), sale(expected_version=2), 'shipment-another-actor')
    assert replay['business_replayed'] and replay['document_id'] == doc
    assert balance(engine)['g'] == 325
    with engine.connect() as c:
        assert c.scalar(select(db.documents.c.quantity).where(db.documents.c.id == doc)) == 3
        event = c.execute(select(events.business_events)).mappings().one()
        assert event['business_reference'] == 'SHIP-001-A'
        assert event['source_account'] == 'store-yiwu-01'
        assert event['occurred_at'] is None and event['time_quality'] == 'unknown'


def test_shipment_conflicts_rollback_and_real_subvoucher_posts(env):
    engine, client = env
    assert post(client).status_code == 200
    before = counts(engine)
    conflict = post(client, sale(quantity=4, expected_version=2), 'shipment-new-request')
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'VOUCHER_CONFLICT'
    changed_request = post(client, sale(quantity=4))
    assert changed_request.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    assert counts(engine) == before and balance(engine)['g'] == 325
    second = post(client, sale(reference='SHIP-001-B', expected_version=2), 'shipment-second-batch')
    assert second.status_code == 200 and balance(engine)['g'] == 322


def test_shipment_duplicate_concurrent_requests_post_once(env):
    engine, _ = env
    who = actor(engine)
    gate = Barrier(2)
    def execute(n):
        gate.wait()
        return inv.sell(engine, who, sale(), 'shipment-concurrent-' + str(n))
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(execute, range(2)))
    assert results[0]['document_id'] == results[1]['document_id']
    assert sorted(r['business_replayed'] for r in results) == [False, True]
    assert balance(engine)['g'] == 325 and counts(engine)['shipment_vouchers'] == 1


@pytest.mark.parametrize('change', [dict(reference=''), dict(source_account=''), dict(quantity=True),
                                    dict(quantity=0), dict(quantity=-1), dict(quantity=1_000_001),
                                    dict(occurred_at='2026-10-16T09:00:00'), dict(run_id='other')])
def test_shipment_rejects_invalid_contract(env, change):
    engine, client = env
    before = counts(engine)
    assert post(client, sale(**change)).status_code == 422
    assert counts(engine) == before


@pytest.mark.parametrize('failure_table', [inv.shipment_vouchers, events.business_events])
def test_shipment_voucher_and_evidence_are_atomic(env, monkeypatch, failure_table):
    engine, _ = env
    before, stock = counts(engine), balance(engine)
    execute = Connection.execute
    def crash(self, statement, *args, **kwargs):
        if getattr(statement, 'table', None) is failure_table and getattr(statement, 'is_insert', False):
            raise RuntimeError('injected shipment failure')
        return execute(self, statement, *args, **kwargs)
    monkeypatch.setattr(Connection, 'execute', crash)
    with pytest.raises(RuntimeError):
        inv.sell(engine, actor(engine), sale(), 'shipment-atomic-failure')
    assert counts(engine) == before and balance(engine) == stock


def test_v2_shipment_honors_allocations_and_preserves_return_flow(env):
    engine, client = env
    with db.write(engine) as c:
        c.execute(update(db.balances).where(db.balances.c.id == balance(engine)['id']).values(offline=5))
    first = post(client, sale(occurred_at='2026-10-15T09:00:00+08:00'))
    assert first.status_code == 200
    with engine.connect() as c:
        entry = c.execute(select(db.ledger).where(db.ledger.c.document_id == first.json()['document_id'])).mappings().one()
        assert entry['delta']['offline'] == -3 and entry['delta']['g'] == -3
        event = c.execute(select(events.business_events)).mappings().one()
        assert event['occurred_at'] == '2026-10-15T01:00:00+00:00'
    result = client.post('/api/v1/returns', json=dict(kind='customer', original_id=first.json()['document_id'],
        reference='RT-NEW-SHIP', quantity=3, expected_version=2, reason='模拟交货实物退回'),
        headers={'Idempotency-Key': 'return-from-v2-shipment'})
    assert result.status_code == 200, result.text
    assert balance(engine)['q'] == 3


def test_shipment_cannot_consume_online_commitments(env):
    engine, client = env
    with db.write(engine) as c:
        c.execute(update(db.balances).where(db.balances.c.id == balance(engine)['id']).values(online=328))
    before = counts(engine)
    result = post(client)
    assert result.status_code == 409 and result.json()['error']['code'] == 'INSUFFICIENT_STOCK'
    assert counts(engine) == before and balance(engine)['g'] == 328


def test_shipment_roles_csrf_and_manual_warehouse(env):
    engine, client = env
    result = client.post('/api/v2/inventory/sales', json=sale(),
        headers={'Idempotency-Key': 'shipment-no-csrf', 'X-CSRF-Token': ''})
    assert result.status_code == 403
    assert post(client, sale(warehouse_id='wh-us')).status_code == 409
    login = client.post('/api/v1/session', json={'username': 'warehouse', 'password': 'test-password'})
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    assert post(client).status_code == 403
    login = client.post('/api/v1/session', json={'username': 'cashier', 'password': 'test-password'})
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    assert post(client).status_code == 200


def test_new_legacy_sales_default_blocked_but_completed_retries_survive(env):
    engine, client = env
    body = {k: v for k, v in sale().items() if k not in ('source_account', 'reference', 'occurred_at')}
    first = client.post('/api/v1/sales/confirm', json=body, headers={'Idempotency-Key': 'legacy-sale-completed'})
    assert first.status_code == 200
    engine.runtime_settings = replace(engine.runtime_settings, allow_legacy_sales=False)
    repeated = client.post('/api/v1/sales/confirm', json=body, headers={'Idempotency-Key': 'legacy-sale-completed'})
    assert repeated.status_code == 200 and repeated.json()['replayed']
    before = counts(engine)
    rejected = client.post('/api/v1/sales/confirm', json=body | {'expected_version': 2},
        headers={'Idempotency-Key': 'new-voucherless-sale'})
    assert rejected.status_code == 422 and rejected.json()['error']['code'] == 'SHIPMENT_REFERENCE_REQUIRED'
    assert rejected.headers['Deprecation'] == 'true'
    assert '/api/v2/inventory/sales' in rejected.headers['Link']
    assert counts(engine) == before
    assert post(client, sale(expected_version=2)).status_code == 200


def test_legacy_sales_compatibility_is_independent_and_opt_in():
    assert not load_settings({}).allow_legacy_sales
    assert not load_settings({'APP_ALLOW_LEGACY_RECEIPTS': 'true'}).allow_legacy_sales
    enabled = load_settings({'APP_ALLOW_LEGACY_SALES': 'true'})
    assert enabled.allow_legacy_sales and not enabled.allow_legacy_receipts
    with pytest.raises(ValueError):
        load_settings({'APP_RUNTIME_MODE': 'production', 'APP_RUN_ID': 'prod', 'APP_ALLOW_LEGACY_SALES': 'true'})
    with pytest.raises(ValueError):
        load_settings({'APP_ALLOW_LEGACY_SALES': 'invalid'})
