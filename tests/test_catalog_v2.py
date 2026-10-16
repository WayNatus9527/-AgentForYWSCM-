"""Catalog invariants, scopes and no fabricated zero-stock assertions."""
import pytest
from sqlalchemy import insert, select, func, update
from app import db, catalog as cat
from app.domain import BusinessError, FIELDS, uid
from test_inventory import env, actor


def supplier(**changes):
    return dict(code='SUP-001', name='模拟供应商', default_lead_days=7, sku_ids=['sku-1'], active=True) | changes


def setup_catalog(engine):
    # Also supports legacy fixtures created before the v5 migration is installed.
    with db.write(engine) as c:
        for name in cat.TABLE_NAMES: db.metadata.tables[name].create(c, checkfirst=True)
        cat.backfill_existing(c)


def create_sku(engine, sid='fresh-sku'):
    with db.write(engine) as c:
        c.execute(insert(db.skus).values(id=sid, tenant_id='demo-yiwu', code=sid, name='无历史商品', spec='单件', unit='件', barcode=''))
    return sid


def foreign(engine):
    with db.write(engine) as c:
        c.execute(insert(db.tenants).values(id='other', name='隔离商户'))
        c.execute(insert(db.warehouses).values(id='other-wh', tenant_id='other', code='WH', name='不可见仓', authority='local'))
        c.execute(insert(db.skus).values(id='other-sku', tenant_id='other', code='SKU', name='不可见商品'))
        c.execute(insert(db.users).values(id='other-admin', tenant_id='other', username='other-admin', role='admin', warehouse_ids=[]))
    return actor(engine, 'other-admin')


def test_supplier_idempotency_business_unique_version_and_atomic_skus(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine)
    first = cat.save_supplier(engine, who, supplier(), 'supplier-create-01')
    again = cat.save_supplier(engine, who, supplier(), 'supplier-create-01')
    assert again['replayed'] and again['id'] == first['id']
    with pytest.raises(BusinessError, match='编码已存在'):
        cat.save_supplier(engine, who, supplier(), 'supplier-create-02')
    with pytest.raises(BusinessError) as conflict:
        cat.save_supplier(engine, who, supplier(name='另一名称'), 'supplier-create-01')
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    changed = cat.save_supplier(engine, who, supplier(sku_ids=['sku-2'], expected_version=1), 'supplier-update-01', first['id'])
    assert changed['version'] == 2 and changed['sku_ids'] == ['sku-2']
    with pytest.raises(BusinessError) as stale:
        cat.save_supplier(engine, who, supplier(expected_version=1), 'supplier-stale-01', first['id'])
    assert stale.value.code == 'VERSION_CONFLICT'
    with pytest.raises(BusinessError):
        cat.save_supplier(engine, who, supplier(sku_ids=['sku-1', 'missing'], expected_version=2), 'supplier-missing-01', first['id'])
    assert cat.list_suppliers(engine, who)['items'][0]['sku_ids'] == ['sku-2']


@pytest.mark.parametrize('changes', [
    {'default_lead_days': True}, {'default_lead_days': -1}, {'default_lead_days': 3651},
    {'default_lead_days': '7'}, {'active': 1}, {'code': ' '}, {'name': 'a\nb'},
    {'sku_ids': ['sku-1', 'sku-1']}, {'sku_ids': [{'id': 'sku-1'}]}, {'tenant_id': 'other'},
])
def test_supplier_service_rejects_bad_input_before_writing(env, changes):
    engine, _ = env; setup_catalog(engine)
    with pytest.raises(BusinessError) as error:
        cat.save_supplier(engine, actor(engine), supplier(**changes), 'invalid-supplier-01')
    assert error.value.status == 422
    assert cat.list_suppliers(engine, actor(engine))['total'] == 0


def test_unknown_is_not_zero_and_noncarried_not_cross_product(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine); sid = create_sku(engine)
    before = cat.list_assortments(engine, who)['total']
    result = cat.set_assortment(engine, who, 'wh-cn', sid, dict(active=True, replenishment=True, expected_version=0), 'unknown-create-01')
    assert result['inventory_state'] == 'unknown'
    assert result['g'] is None and result['available'] is None
    assert result['balance_version'] == 0 and not result['eligible_for_replenishment']
    assert cat.list_assortments(engine, who)['total'] == before + 1
    assert cat.list_assortments(engine, who, 'wh-us', sid)['total'] == 0
    with engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(db.balances).where(db.balances.c.sku_id == sid)) == 0


def test_confirmed_zero_requires_matching_snapshot_and_no_balance_mutation(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine); sid = create_sku(engine)
    cat.set_assortment(engine, who, 'wh-cn', sid, dict(active=True, replenishment=True, expected_version=0), 'confirm-create-01')
    request = dict(expected_version=1, expected_balance_version=0, reference='SNAP-0001', observed={'g': 0, 'q': 0, 'd': 0})
    with pytest.raises(BusinessError) as mismatched:
        cat.confirm_inventory(engine, who, 'wh-cn', sid, request | {'observed': {'g': 1, 'q': 0, 'd': 0}}, 'bad-snapshot-01')
    assert mismatched.value.code == 'SNAPSHOT_MISMATCH'
    good = cat.confirm_inventory(engine, who, 'wh-cn', sid, request, 'snapshot-confirm-01')
    assert good['inventory_state'] == 'confirmed_zero' and good['g'] == 0 and good['eligible_for_replenishment']
    assert good['known_source'] == 'manual_snapshot' and good['evidence_id'] == 'SNAP-0001'
    assert cat.confirm_inventory(engine, who, 'wh-cn', sid, request, 'snapshot-confirm-01')['replayed']
    with engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(db.balances).where(db.balances.c.sku_id == sid)) == 0
        assert c.scalar(select(func.count()).select_from(db.ledger).where(db.ledger.c.sku_id == sid)) == 0
    with pytest.raises(BusinessError) as stale:
        cat.confirm_inventory(engine, who, 'wh-cn', sid, request, 'snapshot-stale-01')
    assert stale.value.code == 'VERSION_CONFLICT'


def test_receipt_does_not_turn_unknown_into_complete_knowledge(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine); sid = create_sku(engine)
    # The movement hook runs in the same caller-owned transaction.
    with db.write(engine) as c:
        row = dict(warehouse_id='wh-cn', sku_id=sid)
        cat.record_movement(c, who, row, 'receipt', 'RCV-NEW')
    unknown = cat.list_assortments(engine, who, 'wh-cn', sid)['items'][0]
    assert unknown['inventory_state'] == 'unknown' and unknown['g'] is None
    with db.write(engine) as c:
        cat.record_movement(c, who, row, 'opening', 'OPEN-ZERO')
    known = cat.list_assortments(engine, who, 'wh-cn', sid)['items'][0]
    assert known['inventory_state'] == 'confirmed_zero' and known['known_source'] == 'trusted_opening'
    with db.write(engine) as c:
        cat.record_movement(c, who, row, 'receipt', 'RCV-LATER')
    assert cat.list_assortments(engine, who, 'wh-cn', sid)['items'][0]['evidence_id'] == 'OPEN-ZERO'


def test_sku_and_assortment_disable_preserve_stock_but_exclude_replenishment(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine)
    initial = cat.list_assortments(engine, who, 'wh-cn', 'sku-1')['items'][0]
    assert initial['eligible_for_replenishment'] and initial['g'] == 328
    stopped = cat.set_sku(engine, who, 'sku-1', {'active': False, 'expected_version': 0}, 'sku-disable-01')
    assert stopped['version'] == 1
    listing = cat.list_assortments(engine, who, 'wh-cn', 'sku-1')['items'][0]
    assert not listing['eligible_for_replenishment'] and listing['g'] == 328 and not listing['sku_active']
    with pytest.raises(BusinessError) as stale:
        cat.set_sku(engine, who, 'sku-1', {'active': True, 'expected_version': 0}, 'sku-stale-01')
    assert stale.value.code == 'VERSION_CONFLICT'
    stopped = cat.set_assortment(engine, who, 'wh-cn', 'sku-1', {'active': False, 'replenishment': False, 'expected_version': initial['version']}, 'assort-disable-01')
    assert stopped['g'] == 328 and not stopped['active'] and not stopped['eligible_for_replenishment']


def test_warehouse_purpose_versions_and_pagination(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine)
    first = cat.set_warehouse(engine, who, 'wh-cn', {'purpose': 'fulfillment', 'expected_version': 0}, 'warehouse-purpose-01')
    assert first['version'] == 1
    assert cat.set_warehouse(engine, who, 'wh-cn', {'purpose': 'fulfillment', 'expected_version': 0}, 'warehouse-purpose-01')['replayed']
    assert cat.list_warehouses(engine, who, 1, 0)['total'] == 2
    assert cat.list_skus(engine, who, 2, 0)['items'] != cat.list_skus(engine, who, 2, 2)['items']
    with pytest.raises(BusinessError): cat.list_skus(engine, who, 0, 0)
    with pytest.raises(BusinessError): cat.list_assortments(engine, who, limit=True)
    with pytest.raises(BusinessError): cat.set_warehouse(engine, who, 'wh-cn', {'purpose': 'garbage', 'expected_version': 1}, 'invalid-purpose-01')


def test_mapping_unique_by_account_and_source_not_name_and_version(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine)
    payload = dict(connection_account='shop-A', source_code='SRC1', sku_id='sku-1', active=True)
    a = cat.save_mapping(engine, who, payload, 'mapping-create-01')
    assert cat.save_mapping(engine, who, payload, 'mapping-create-01')['id'] == a['id']
    with pytest.raises(BusinessError) as duplicate:
        cat.save_mapping(engine, who, payload | {'sku_id': 'sku-2'}, 'mapping-duplicate-01')
    assert duplicate.value.code == 'MAPPING_EXISTS'
    cat.save_mapping(engine, who, payload | {'connection_account': 'shop-B', 'sku_id': 'sku-2'}, 'mapping-other-account-01')
    assert cat.list_mappings(engine, who)['total'] == 2
    b = cat.save_mapping(engine, who, payload | {'sku_id': 'sku-2', 'expected_version': 1}, 'mapping-change-01', a['id'])
    assert b['version'] == 2 and b['sku_id'] == 'sku-2'
    with pytest.raises(BusinessError) as stale:
        cat.save_mapping(engine, who, payload | {'expected_version': 1}, 'mapping-stale-01', a['id'])
    assert stale.value.code == 'VERSION_CONFLICT'


def test_catalog_tenant_and_warehouse_scopes_and_stale_actor(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine); other = foreign(engine)
    one = cat.save_supplier(engine, who, supplier(), 'tenant-supplier-01')
    second = cat.save_supplier(engine, other, supplier(sku_ids=['other-sku']), 'tenant-supplier-01')
    assert second['id'] != one['id'] and cat.list_suppliers(engine, other)['total'] == 1
    for fn in [
        lambda: cat.set_sku(engine, who, 'other-sku', dict(active=False, expected_version=0), 'bad-tenant-sku-01'),
        lambda: cat.set_assortment(engine, who, 'other-wh', 'sku-1', dict(active=True, replenishment=True, expected_version=0), 'bad-tenant-wh-01'),
        lambda: cat.save_supplier(engine, who, supplier(expected_version=1), 'bad-tenant-supp-01', second['id']),
        lambda: cat.save_mapping(engine, who, dict(connection_account='A', source_code='X', sku_id='other-sku'), 'bad-tenant-map-01'),
    ]:
        with pytest.raises(BusinessError) as blocked: fn()
        assert blocked.value.status == 404
    warehouse = actor(engine, 'warehouse')
    assert all(x['warehouse_id'] == 'wh-cn' for x in cat.list_assortments(engine, warehouse)['items'])
    assert [x['id'] for x in cat.list_warehouses(engine, warehouse)['items']] == ['wh-cn']
    with pytest.raises(BusinessError) as no_scope: cat.list_assortments(engine, warehouse, 'wh-us')
    assert no_scope.value.status == 404
    with pytest.raises(BusinessError) as cannot_edit: cat.save_supplier(engine, warehouse, supplier(), 'not-admin-01')
    assert cannot_edit.value.status == 403
    with db.write(engine) as c:
        c.execute(update(db.users).where(db.users.c.id == warehouse['id']).values(warehouse_ids=['wh-us']))
    with pytest.raises(BusinessError) as revoked: cat.list_assortments(engine, warehouse)
    assert revoked.value.status == 401


def test_legacy_backfill_is_sparse_audited_idempotent_without_balance_rewrite(env):
    engine, _ = env; setup_catalog(engine); who = actor(engine)
    with engine.connect() as c:
        before = [dict(x) for x in c.execute(select(db.balances)).mappings()]
    rows = cat.list_assortments(engine, who)['items']
    assert len(rows) == 12 and all(x['inventory_known'] for x in rows)
    assert {x['known_source'] for x in rows} == {'local_ledger', 'manual_snapshot'}
    zero = next(x for x in rows if x['warehouse_id'] == 'wh-us' and x['sku_id'] == 'sku-4')
    assert zero['inventory_state'] == 'confirmed_zero'
    sid = create_sku(engine)
    with db.write(engine) as c:
        c.execute(insert(db.balances).values(id=uid(), tenant_id=who['tenant_id'], owner_id=who['tenant_id'], warehouse_id='wh-cn', sku_id=sid, version=0, **dict.fromkeys(FIELDS, 0)))
        cat.backfill_existing(c); cat.backfill_existing(c)
    unknown = cat.list_assortments(engine, who, 'wh-cn', sid)['items'][0]
    assert unknown['inventory_state'] == 'unknown'
    with engine.connect() as c:
        after = [dict(x) for x in c.execute(select(db.balances).where(db.balances.c.sku_id != sid)).mappings()]
        assert after == before
        audit_count = c.scalar(select(func.count()).select_from(cat.accounts.audit).where(cat.accounts.audit.c.action == 'catalog.migration_origin'))
        assert audit_count == 13


def test_catalog_http_requires_csrf_idempotency_and_rejects_scope_fields(env):
    _, client = env
    url = '/api/v2/catalog/suppliers'
    # Registered by create_app in M1, exercises authentication and route contracts.
    response = client.post(url, json=supplier(), headers={'Idempotency-Key': 'http-supplier-01'})
    assert response.status_code == 200, response.text
    assert client.post(url, json=supplier(), headers={'Idempotency-Key': 'http-supplier-01'}).json()['replayed']
    assert client.post(url, json=supplier()).status_code == 422
    assert client.post(url, json=supplier(code='OTHER'), headers={'Idempotency-Key': 'http-no-csrf-01', 'X-CSRF-Token': ''}).status_code == 403
    assert client.post(url, json=supplier(tenant_id='other'), headers={'Idempotency-Key': 'http-scope-01'}).status_code == 422
    assert client.get('/api/v2/catalog/skus?limit=0').status_code == 422


def test_legacy_receipt_without_opening_stays_unknown(env):
    engine,_=env;who=actor(engine);sid=create_sku(engine,'legacy-receipt-only')
    with db.write(engine) as c:
        c.execute(insert(db.balances).values(id=uid(),tenant_id=who['tenant_id'],owner_id=who['tenant_id'],
            warehouse_id='wh-cn',sku_id=sid,version=1,**(dict.fromkeys(FIELDS,0)|{'g':5})))
        c.execute(insert(db.ledger).values(id=uid(),tenant_id=who['tenant_id'],owner_id=who['tenant_id'],
            warehouse_id='wh-cn',sku_id=sid,document_id='legacy-receipt',kind='receipt',delta={'g':5},
            before=dict.fromkeys(FIELDS,0),after=dict.fromkeys(FIELDS,0)|{'g':5},created_at='2026-01-01'))
        cat.backfill_existing(c)
    result=cat.list_assortments(engine,who,'wh-cn',sid)['items'][0]
    assert not result['inventory_known'] and result['g'] is None


def test_cashier_cannot_read_procurement_and_external_mapping_details(env):
    engine,client=env
    reply=client.post('/api/v1/session',json={'username':'cashier','password':'test-password'})
    client.headers['X-CSRF-Token']=reply.json()['csrf']
    assert client.get('/api/v2/catalog/suppliers').status_code==403
    assert client.get('/api/v2/catalog/external-mappings').status_code==403
    assert client.get('/api/v2/catalog/assortments').status_code==200
