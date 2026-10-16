"""Versioned catalog and explicit inventory knowledge (PRD v2 FR01).

Catalog writes never manufacture stock. A complete opening or an explicitly
confirmed matching physical snapshot establishes the starting balance; ordinary
receipts for a previously unknown combination do not establish that history.
"""
from typing import Annotated
from fastapi import Body, Header, Request
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U, CheckConstraint as CK, select, insert, update, func, and_
from . import db, workflows as w, accounts
from .domain import uid, now, require_role, get_warehouse, get_sku, available, unallocated

suppliers = Table('catalog_suppliers', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False), C('code', S, nullable=False),
    C('name', S, nullable=False), C('default_lead_days', I, nullable=False), C('active', I, nullable=False),
    C('version', I, nullable=False), U('tenant_id', 'code'), CK('default_lead_days >= 0 AND default_lead_days <= 3650'))
supplier_skus = Table('catalog_supplier_skus', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False), C('supplier_id', S, nullable=False),
    C('sku_id', S, nullable=False), U('tenant_id', 'supplier_id', 'sku_id'))
sku_settings = Table('catalog_sku_settings', db.metadata,
    C('sku_id', S, primary_key=True), C('tenant_id', S, nullable=False), C('active', I, nullable=False), C('version', I, nullable=False))
warehouse_settings = Table('catalog_warehouse_settings', db.metadata,
    C('warehouse_id', S, primary_key=True), C('tenant_id', S, nullable=False), C('purpose', S, nullable=False), C('version', I, nullable=False))
assortments = Table('catalog_assortments', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('active', I, nullable=False), C('replenishment', I, nullable=False),
    C('inventory_known', I, nullable=False), C('known_source', S), C('evidence_id', S),
    C('confirmed_at', S), C('version', I, nullable=False), U('tenant_id', 'warehouse_id', 'sku_id'))
external_mappings = Table('catalog_external_mappings', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False), C('connection_account', S, nullable=False),
    C('source_code', S, nullable=False), C('sku_id', S, nullable=False), C('active', I, nullable=False),
    C('version', I, nullable=False), U('tenant_id', 'connection_account', 'source_code'))
TABLE_NAMES = tuple(t.name for t in (suppliers, supplier_skus, sku_settings, warehouse_settings, assortments, external_mappings))
PURPOSES = ('general', 'storage', 'fulfillment', 'transit', 'returns')


def _shape(body, required, optional=()):
    if not isinstance(body, dict) or set(body) - set(required) - set(optional) or set(required) - set(body):
        w.fail('INVALID_CATALOG', '资料字段缺失或包含未知字段', 422)


def _text(value, name, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or value != value.strip() or any(ord(c) < 32 for c in value):
        w.fail('INVALID_CATALOG', name + '必须为非空文本，不能含控制字符或首尾空格', 422)
    return value


def _integer(value, name, maximum=1_000_000):
    if type(value) is not int or not 0 <= value <= maximum:
        w.fail('INVALID_CATALOG', name + '必须为规定范围内的整数', 422)
    return value


def _boolean(value):
    if type(value) is not bool:
        w.fail('INVALID_CATALOG', '启停选项必须为布尔值', 422)
    return int(value)


def _version(body, current):
    if _integer(body['expected_version'], '版本') != current:
        w.fail('VERSION_CONFLICT', '资料已变化，请刷新后确认')


def _command(engine, actor, kind, body, key, action):
    require_role(actor, 'admin')
    _text(key, '请求编号', 120)
    from .inventory import command
    return command(engine, actor, kind, body, key, action)


def _audit(c, actor, action, target, before, after):
    c.execute(insert(accounts.audit).values(id=uid(), tenant_id=actor['tenant_id'], actor_id=actor['id'],
        action=action, target_id=target, before=before, after=after, created_at=now()))


def _read(c, actor):
    accounts.assert_current(c, actor)
    require_role(actor, 'admin', 'warehouse', 'cashier')


def _paginate(c, query, limit, offset):
    _integer(limit, '每页数量', 200)
    if limit == 0: w.fail('INVALID_CATALOG', '每页数量必须大于零', 422)
    _integer(offset, '分页偏移', 10_000_000)
    total = c.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {'items': [dict(x) for x in c.execute(query.limit(limit).offset(offset)).mappings()], 'total': total, 'limit': limit, 'offset': offset}


def _supplier_view(c, row):
    result = dict(row)
    result['active'] = bool(result['active'])
    result['sku_ids'] = list(c.execute(select(supplier_skus.c.sku_id).where(supplier_skus.c.tenant_id == row['tenant_id'], supplier_skus.c.supplier_id == row['id']).order_by(supplier_skus.c.sku_id)).scalars())
    return result


def save_supplier(engine, actor, body, key, supplier_id=None):
    _shape(body, ('code', 'name', 'default_lead_days', 'sku_ids') + (('expected_version',) if supplier_id else ()), ('active',))
    code = _text(body['code'], '供应商编码'); name = _text(body['name'], '供应商名称')
    days = _integer(body['default_lead_days'], '默认交期', 3650)
    active = _boolean(body.get('active', True))
    ids = body['sku_ids']
    if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        w.fail('INVALID_CATALOG', '供货商品必须为不重复的商品编号列表，最多1000项', 422)
    for sid in ids: _text(sid, '商品编号')
    def apply(c):
        old = c.execute(select(suppliers).where(suppliers.c.id == supplier_id, suppliers.c.tenant_id == actor['tenant_id'])).mappings().first() if supplier_id else None
        if supplier_id and not old: w.fail('NOT_FOUND', '供应商不存在', 404)
        if old: _version(body, old['version'])
        duplicate = c.scalar(select(suppliers.c.id).where(suppliers.c.tenant_id == actor['tenant_id'], suppliers.c.code == code))
        if duplicate and duplicate != supplier_id: w.fail('CATALOG_EXISTS', '供应商编码已存在')
        for sid in ids: get_sku(c, actor, sid)
        before = _supplier_view(c, old) if old else {}
        row = dict(id=supplier_id or uid(), tenant_id=actor['tenant_id'], code=code, name=name, default_lead_days=days, active=active, version=old['version'] + 1 if old else 1)
        if old:
            from sqlalchemy import delete
            c.execute(update(suppliers).where(suppliers.c.id == supplier_id).values(**row))
            c.execute(delete(supplier_skus).where(supplier_skus.c.tenant_id == actor['tenant_id'], supplier_skus.c.supplier_id == supplier_id))
        else: c.execute(insert(suppliers).values(**row))
        for sid in ids: c.execute(insert(supplier_skus).values(id=uid(), tenant_id=actor['tenant_id'], supplier_id=row['id'], sku_id=sid))
        result = _supplier_view(c, row)
        _audit(c, actor, 'catalog.supplier', row['id'], before, result)
        return result
    return _command(engine, actor, 'catalog.supplier:' + (supplier_id or 'new'), body, key, apply)


def list_suppliers(engine, actor, limit=50, offset=0):
    require_role(actor, 'admin', 'warehouse')
    with engine.connect() as c:
        _read(c, actor)
        page = _paginate(c, select(suppliers).where(suppliers.c.tenant_id == actor['tenant_id']).order_by(suppliers.c.code, suppliers.c.id), limit, offset)
        page['items'] = [_supplier_view(c, x) for x in page['items']]
        return page


def set_sku(engine, actor, sku_id, body, key):
    _shape(body, ('active', 'expected_version')); active = _boolean(body['active'])
    def apply(c):
        get_sku(c, actor, sku_id)
        old = c.execute(select(sku_settings).where(sku_settings.c.sku_id == sku_id, sku_settings.c.tenant_id == actor['tenant_id'])).mappings().first()
        _version(body, old['version'] if old else 0)
        row = dict(sku_id=sku_id, tenant_id=actor['tenant_id'], active=active, version=(old['version'] if old else 0)+1)
        if old: c.execute(update(sku_settings).where(sku_settings.c.sku_id == sku_id).values(**row))
        else: c.execute(insert(sku_settings).values(**row))
        _audit(c, actor, 'catalog.sku', sku_id, dict(old) if old else {'active': True, 'version': 0}, row)
        return {**row, 'active': bool(active)}
    return _command(engine, actor, 'catalog.sku:' + sku_id, body, key, apply)


def set_warehouse(engine, actor, warehouse_id, body, key):
    _shape(body, ('purpose', 'expected_version'))
    if body['purpose'] not in PURPOSES: w.fail('INVALID_CATALOG', '仓库用途不受支持', 422)
    def apply(c):
        get_warehouse(c, actor, warehouse_id)
        old = c.execute(select(warehouse_settings).where(warehouse_settings.c.warehouse_id == warehouse_id, warehouse_settings.c.tenant_id == actor['tenant_id'])).mappings().first()
        _version(body, old['version'] if old else 0)
        row = dict(warehouse_id=warehouse_id, tenant_id=actor['tenant_id'], purpose=body['purpose'], version=(old['version'] if old else 0)+1)
        if old: c.execute(update(warehouse_settings).where(warehouse_settings.c.warehouse_id == warehouse_id).values(**row))
        else: c.execute(insert(warehouse_settings).values(**row))
        _audit(c, actor, 'catalog.warehouse', warehouse_id, dict(old) if old else {'purpose': 'general', 'version': 0}, row)
        return row
    return _command(engine, actor, 'catalog.warehouse:' + warehouse_id, body, key, apply)


def list_skus(engine, actor, limit=50, offset=0):
    with engine.connect() as c:
        _read(c, actor)
        query = select(db.skus, func.coalesce(sku_settings.c.active, 1).label('active'), func.coalesce(sku_settings.c.version, 0).label('version')).select_from(db.skus.outerjoin(sku_settings, and_(sku_settings.c.sku_id == db.skus.c.id, sku_settings.c.tenant_id == db.skus.c.tenant_id))).where(db.skus.c.tenant_id == actor['tenant_id']).order_by(db.skus.c.code, db.skus.c.id)
        page = _paginate(c, query, limit, offset)
        for row in page['items']: row['active'] = bool(row['active'])
        return page


def list_warehouses(engine, actor, limit=50, offset=0):
    with engine.connect() as c:
        _read(c, actor)
        query = select(db.warehouses, func.coalesce(warehouse_settings.c.purpose, 'general').label('purpose'), func.coalesce(warehouse_settings.c.version, 0).label('version')).select_from(db.warehouses.outerjoin(warehouse_settings, and_(warehouse_settings.c.warehouse_id == db.warehouses.c.id, warehouse_settings.c.tenant_id == db.warehouses.c.tenant_id))).where(db.warehouses.c.tenant_id == actor['tenant_id']).order_by(db.warehouses.c.code, db.warehouses.c.id)
        if actor['role'] != 'admin': query = query.where(db.warehouses.c.id.in_(actor['warehouse_ids']))
        return _paginate(c, query, limit, offset)


def _assortment(c, tenant_id, warehouse_id, sku_id):
    return c.execute(select(assortments).where(assortments.c.tenant_id == tenant_id, assortments.c.warehouse_id == warehouse_id, assortments.c.sku_id == sku_id)).mappings().first()


def _stock(c, tenant_id, warehouse_id, sku_id):
    return c.execute(select(db.balances).where(db.balances.c.tenant_id == tenant_id, db.balances.c.owner_id == tenant_id, db.balances.c.warehouse_id == warehouse_id, db.balances.c.sku_id == sku_id)).mappings().first()


def _new_assortment(tenant_id, warehouse_id, sku_id):
    return dict(id=uid(), tenant_id=tenant_id, warehouse_id=warehouse_id, sku_id=sku_id, active=1, replenishment=1, inventory_known=0, known_source=None, evidence_id=None, confirmed_at=None, version=1)


def assortment_view(c, row):
    result = dict(row)
    stock = _stock(c, row['tenant_id'], row['warehouse_id'], row['sku_id'])
    active = c.scalar(select(sku_settings.c.active).where(sku_settings.c.tenant_id == row['tenant_id'], sku_settings.c.sku_id == row['sku_id']))
    known = bool(row['inventory_known'])
    result.update(active=bool(row['active']), replenishment=bool(row['replenishment']), inventory_known=known,
        sku_active=active is None or bool(active), balance_version=stock['version'] if stock else 0)
    result['inventory_state'] = 'unknown' if not known else ('confirmed_zero' if not stock or sum(stock[f] for f in ('g', 'q', 'd')) == 0 else 'known')
    for field in ('g', 'q', 'd', 'r', 't', 'h', 'b', 'offline', 'online', 'pending', 'withdrawing'): result[field] = (stock[field] if stock else 0) if known else None
    result['available'] = (available(stock) if stock else 0) if known else None
    result['unallocated'] = (unallocated(stock) if stock else 0) if known else None
    result['offline_available'] = (stock['offline'] + max(0, unallocated(stock)) if stock else 0) if known else None
    result['eligible_for_replenishment'] = bool(row['active'] and row['replenishment'] and result['sku_active'] and known)
    return result


def set_assortment(engine, actor, warehouse_id, sku_id, body, key):
    _shape(body, ('active', 'replenishment', 'expected_version'))
    active = _boolean(body['active']); replenish = _boolean(body['replenishment'])
    def apply(c):
        get_warehouse(c, actor, warehouse_id); get_sku(c, actor, sku_id)
        old = _assortment(c, actor['tenant_id'], warehouse_id, sku_id)
        _version(body, old['version'] if old else 0)
        row = dict(old) if old else _new_assortment(actor['tenant_id'], warehouse_id, sku_id)
        row.update(active=active, replenishment=replenish, version=old['version']+1 if old else 1)
        if old: c.execute(update(assortments).where(assortments.c.id == row['id']).values(**row))
        else: c.execute(insert(assortments).values(**row))
        _audit(c, actor, 'catalog.assortment', row['id'], dict(old) if old else {}, row)
        return assortment_view(c, row)
    return _command(engine, actor, 'catalog.assortment:' + warehouse_id + ':' + sku_id, body, key, apply)


def list_assortments(engine, actor, warehouse_id=None, sku_id=None, limit=50, offset=0):
    with engine.connect() as c:
        _read(c, actor)
        query = select(assortments).where(assortments.c.tenant_id == actor['tenant_id']).order_by(assortments.c.warehouse_id, assortments.c.sku_id)
        if warehouse_id:
            get_warehouse(c, actor, warehouse_id); query = query.where(assortments.c.warehouse_id == warehouse_id)
        if sku_id:
            get_sku(c, actor, sku_id); query = query.where(assortments.c.sku_id == sku_id)
        if actor['role'] != 'admin': query = query.where(assortments.c.warehouse_id.in_(actor['warehouse_ids']))
        page = _paginate(c, query, limit, offset)
        page['items'] = [assortment_view(c, x) for x in page['items']]
        return page


def confirm_inventory(engine, actor, warehouse_id, sku_id, body, key):
    _shape(body, ('expected_version', 'expected_balance_version', 'reference', 'observed'))
    _text(body['reference'], '快照证据', 240); _integer(body['expected_balance_version'], '库存版本')
    _shape(body['observed'], ('g', 'q', 'd'))
    for field in ('g', 'q', 'd'): _integer(body['observed'][field], field)
    def apply(c):
        get_warehouse(c, actor, warehouse_id); get_sku(c, actor, sku_id)
        old = _assortment(c, actor['tenant_id'], warehouse_id, sku_id)
        if not old: w.fail('NOT_FOUND', '请先建立经营关系', 404)
        _version(body, old['version'])
        stock = _stock(c, actor['tenant_id'], warehouse_id, sku_id)
        if body['expected_balance_version'] != (stock['version'] if stock else 0): w.fail('VERSION_CONFLICT', '库存已变化，请重新核对快照')
        if any(body['observed'][f] != (stock[f] if stock else 0) for f in ('g', 'q', 'd')):
            w.fail('SNAPSHOT_MISMATCH', '快照与账面不符，须先走审批调整流程，不能直接确认或覆盖余额')
        row = dict(old) | {'inventory_known': 1, 'known_source': 'manual_snapshot', 'evidence_id': body['reference'], 'confirmed_at': now(), 'version': old['version']+1}
        c.execute(update(assortments).where(assortments.c.id == old['id']).values(**row))
        _audit(c, actor, 'catalog.inventory_confirmation', old['id'], dict(old), row)
        return assortment_view(c, row)
    return _command(engine, actor, 'catalog.confirm:' + warehouse_id + ':' + sku_id, body, key, apply)


def save_mapping(engine, actor, body, key, mapping_id=None):
    _shape(body, ('connection_account', 'source_code', 'sku_id') + (('expected_version',) if mapping_id else ()), ('active',))
    for field in ('connection_account', 'source_code', 'sku_id'): _text(body[field], field)
    active = _boolean(body.get('active', True))
    def apply(c):
        get_sku(c, actor, body['sku_id'])
        old = c.execute(select(external_mappings).where(external_mappings.c.tenant_id == actor['tenant_id'], external_mappings.c.id == mapping_id)).mappings().first() if mapping_id else None
        if mapping_id and not old: w.fail('NOT_FOUND', '外部映射不存在', 404)
        if old: _version(body, old['version'])
        duplicate = c.scalar(select(external_mappings.c.id).where(external_mappings.c.tenant_id == actor['tenant_id'], external_mappings.c.connection_account == body['connection_account'], external_mappings.c.source_code == body['source_code']))
        if duplicate and duplicate != mapping_id: w.fail('MAPPING_EXISTS', '此连接账号与源编码已有映射')
        row = dict(id=mapping_id or uid(), tenant_id=actor['tenant_id'], **{k: body[k] for k in ('connection_account', 'source_code', 'sku_id')}, active=active, version=old['version']+1 if old else 1)
        if old: c.execute(update(external_mappings).where(external_mappings.c.id == mapping_id).values(**row))
        else: c.execute(insert(external_mappings).values(**row))
        _audit(c, actor, 'catalog.mapping', row['id'], dict(old) if old else {}, row)
        return {**row, 'active': bool(active)}
    return _command(engine, actor, 'catalog.mapping:' + (mapping_id or 'new'), body, key, apply)


def list_mappings(engine, actor, limit=50, offset=0):
    require_role(actor, 'admin', 'warehouse')
    with engine.connect() as c:
        _read(c, actor)
        page = _paginate(c, select(external_mappings).where(external_mappings.c.tenant_id == actor['tenant_id']).order_by(external_mappings.c.connection_account, external_mappings.c.source_code), limit, offset)
        for row in page['items']: row['active'] = bool(row['active'])
        return page


def record_movement(c, actor, row, kind, document_id):
    """Called inside the inventory transaction, after its ledger entry exists."""
    old = _assortment(c, actor['tenant_id'], row['warehouse_id'], row['sku_id'])
    if old and (old['inventory_known'] or kind not in ('opening', 'initial')): return
    new = dict(old) if old else _new_assortment(actor['tenant_id'], row['warehouse_id'], row['sku_id'])
    if kind in ('opening', 'initial'):
        new.update(inventory_known=1, known_source='trusted_opening', evidence_id=document_id, confirmed_at=now())
    if old:
        new['version'] = old['version']+1
        c.execute(update(assortments).where(assortments.c.id == old['id']).values(**new))
    else: c.execute(insert(assortments).values(**new))
    _audit(c, actor, 'catalog.inventory_origin', new['id'], dict(old) if old else {}, new)


def backfill_existing(c):
    """Adopt existing persisted balances once; never infer all SKU/warehouse pairs.

    Legacy ledgers represent the old system's local inventory baseline. This
    compatibility migration keeps their provenance, without claiming the
    registration timestamp is the real arrival or processing time.
    """
    for stock in c.execute(select(db.balances).where(db.balances.c.owner_id == db.balances.c.tenant_id)).mappings():
        tenant = stock['tenant_id']; wid = stock['warehouse_id']; sid = stock['sku_id']
        if _assortment(c, tenant, wid, sid): continue
        wh = c.execute(select(db.warehouses).where(db.warehouses.c.id == wid, db.warehouses.c.tenant_id == tenant)).mappings().first()
        sku = c.scalar(select(db.skus.c.id).where(db.skus.c.id == sid, db.skus.c.tenant_id == tenant))
        if not wh or not sku: continue
        evidence = c.execute(select(db.ledger).where(db.ledger.c.tenant_id == tenant, db.ledger.c.owner_id == tenant, db.ledger.c.warehouse_id == wid, db.ledger.c.sku_id == sid, db.ledger.c.kind.in_(('initial', 'opening'))).order_by(db.ledger.c.created_at, db.ledger.c.id)).mappings().first()
        row = _new_assortment(tenant, wid, sid)
        if stock['version'] > 0 and evidence:
            row.update(inventory_known=1, known_source='local_ledger' if wh['authority'] == 'local' else 'manual_snapshot', evidence_id=evidence['id'], confirmed_at=now())
        c.execute(insert(assortments).values(**row))
        _audit(c, {'tenant_id': tenant, 'id': 'schema-migration-v5'}, 'catalog.migration_origin', row['id'], {}, row)


def install(app, engine, actor_for):
    Key = Annotated[str, Header(alias='Idempotency-Key', min_length=8, max_length=120)]
    @app.get('/api/v2/catalog/skus')
    def skus_get(request: Request, limit: int=50, offset: int=0):
        actor, _ = actor_for(request); return list_skus(engine, actor, limit, offset)
    @app.put('/api/v2/catalog/skus/{sku_id}')
    def sku_put(sku_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return set_sku(engine, actor, sku_id, body, idempotency_key)
    @app.get('/api/v2/catalog/warehouses')
    def warehouses_get(request: Request, limit: int=50, offset: int=0):
        actor, _ = actor_for(request); return list_warehouses(engine, actor, limit, offset)
    @app.put('/api/v2/catalog/warehouses/{warehouse_id}')
    def warehouse_put(warehouse_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return set_warehouse(engine, actor, warehouse_id, body, idempotency_key)
    @app.get('/api/v2/catalog/suppliers')
    def suppliers_get(request: Request, limit: int=50, offset: int=0):
        actor, _ = actor_for(request); return list_suppliers(engine, actor, limit, offset)
    @app.post('/api/v2/catalog/suppliers')
    def supplier_post(request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return save_supplier(engine, actor, body, idempotency_key)
    @app.put('/api/v2/catalog/suppliers/{supplier_id}')
    def supplier_put(supplier_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return save_supplier(engine, actor, body, idempotency_key, supplier_id)
    @app.get('/api/v2/catalog/assortments')
    def assortments_get(request: Request, warehouse_id: str | None=None, sku_id: str | None=None, limit: int=50, offset: int=0):
        actor, _ = actor_for(request); return list_assortments(engine, actor, warehouse_id, sku_id, limit, offset)
    @app.put('/api/v2/catalog/assortments/{warehouse_id}/{sku_id}')
    def assortment_put(warehouse_id: str, sku_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return set_assortment(engine, actor, warehouse_id, sku_id, body, idempotency_key)
    @app.post('/api/v2/catalog/assortments/{warehouse_id}/{sku_id}/confirm')
    def confirm_post(warehouse_id: str, sku_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return confirm_inventory(engine, actor, warehouse_id, sku_id, body, idempotency_key)
    @app.get('/api/v2/catalog/external-mappings')
    def mappings_get(request: Request, limit: int=50, offset: int=0):
        actor, _ = actor_for(request); return list_mappings(engine, actor, limit, offset)
    @app.post('/api/v2/catalog/external-mappings')
    def mapping_post(request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return save_mapping(engine, actor, body, idempotency_key)
    @app.put('/api/v2/catalog/external-mappings/{mapping_id}')
    def mapping_put(mapping_id: str, request: Request, idempotency_key: Key, body: dict=Body(...)):
        actor, _ = actor_for(request, True); return save_mapping(engine, actor, body, idempotency_key, mapping_id)
