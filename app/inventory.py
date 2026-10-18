"""Inventory posting boundary shared by legacy workflows and v2 commands.

Physical units and commitments retain their v0.4.1 meanings. In particular b is
an explicit physical hold, NOT the replenishment policy's safety stock target.
"""
import hashlib
import json

from sqlalchemy import Table, Column as C, String as S, JSON, UniqueConstraint as U
from sqlalchemy import select, insert, update

from . import db
from .domain import (BusinessError, FIELDS, available, unallocated, get_warehouse,
                     get_sku, require_role, uid, now)

vouchers = Table(
    'receipt_vouchers', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('source_account', S, nullable=False), C('reference', S, nullable=False),
    C('fingerprint', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('document_id', S, nullable=False, unique=True),
    C('response', JSON, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'source_account', 'reference'))
shipment_vouchers = Table(
    'shipment_vouchers', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('source_account', S, nullable=False), C('reference', S, nullable=False),
    C('fingerprint', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('document_id', S, nullable=False, unique=True),
    C('response', JSON, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'source_account', 'reference'))
TABLE_NAMES = ('receipt_vouchers', 'shipment_vouchers')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def command(engine, actor, operation, body, key, action):
    """New requests are scoped by operation, actor and tenant. Legacy keys survive."""
    if not isinstance(key, str) or not 8 <= len(key) <= 160:
        raise BusinessError('INVALID_KEY', '请求编号须为8至160个字符', 422)
    from .workflows import command as legacy_command
    scoped_key = 'v2:' + fingerprint({'operation': operation, 'key': key})
    def authorize(c):
        # A retry is still a read of business data. Recheck the current scope
        # even after re-login, before returning a cached response.
        if body.get('warehouse_id'):
            get_warehouse(c, actor, body['warehouse_id'])
        if body.get('sku_id'):
            get_sku(c, actor, body['sku_id'])
        if operation.startswith('commitment.') and ':' in operation:
            from . import commitments
            verb, identity = operation.split(':', 1)
            table = commitments.reservations if verb == 'commitment.cancel' else commitments.grants
            commitments._get(c, table, actor, identity)
    return legacy_command(engine, actor, operation, body, scoped_key, action, authorize=authorize)


def load_balance(c, actor, warehouse_id, sku_id):
    get_warehouse(c, actor, warehouse_id, True)
    get_sku(c, actor, sku_id)
    row = c.execute(select(db.balances).where(
        db.balances.c.tenant_id == actor['tenant_id'],
        db.balances.c.owner_id == actor['tenant_id'],
        db.balances.c.warehouse_id == warehouse_id,
        db.balances.c.sku_id == sku_id)).mappings().first()
    return dict(row) if row else dict(
        id=uid(), tenant_id=actor['tenant_id'], owner_id=actor['tenant_id'],
        warehouse_id=warehouse_id, sku_id=sku_id, version=0, updated_at=now(),
        **dict.fromkeys(FIELDS, 0))


def validate_balance(row):
    if any(type(row[f]) is not int or row[f] < 0 for f in FIELDS):
        raise BusinessError('INSUFFICIENT_STOCK', '库存和占用必须为非负整数')
    if available(row) < 0 or unallocated(row) < 0:
        raise BusinessError('COMMITMENT_CONFLICT', '良品不足以覆盖占用、冻结、保留和已分配额度')


def movement(c, actor, row, delta, kind, source, note, expected, *,
             occurred_at=None, business_reference=None, source_account=''):
    """Atomic posting within the caller's tenant write transaction.

    The persisted balance is re-read so an arbitrary stale/mutated row cannot
    become a new source of truth. Every write includes ledger, event and outbox.
    """
    from .accounts import assert_current
    assert_current(c, actor)
    if row['tenant_id'] != actor['tenant_id'] or row['owner_id'] != actor['tenant_id']:
        raise BusinessError('NOT_FOUND', '库存不存在或未授权', 404)
    if set(delta) - set(FIELDS) or any(type(v) is not int for v in delta.values()):
        raise BusinessError('INVALID_DELTA', '库存变动须使用已定义字段及整数', 422)
    from .counting import assert_unlocked
    assert_unlocked(c, actor, row['warehouse_id'], row['sku_id'])
    fresh = load_balance(c, actor, row['warehouse_id'], row['sku_id'])
    if fresh['version'] != expected or row['version'] != expected:
        raise BusinessError('VERSION_CONFLICT', '库存已变化，请刷新后重新申请或确认')
    before = {f: fresh[f] for f in FIELDS}
    after = {**fresh, **{f: fresh[f] + delta.get(f, 0) for f in FIELDS}}
    validate_balance(after)
    from .operations import protect_pending_quality
    protect_pending_quality(c, actor, after)
    timestamp = now()
    after.update(version=fresh['version'] + 1, updated_at=timestamp)
    if fresh['version'] or c.execute(select(db.balances.c.id).where(db.balances.c.id == fresh['id'])).first():
        c.execute(update(db.balances).where(db.balances.c.id == fresh['id']).values(**after))
    else:
        c.execute(insert(db.balances).values(**after))
    doc_id = kind.upper()[:4] + '-' + uid()[:18]
    quantity = sum(delta.get(f, 0) for f in ('g', 'q', 'd'))
    if kind == 'sale':
        quantity = -quantity  # Legacy sale/return references use positive quantity.
    c.execute(insert(db.documents).values(
        id=doc_id, tenant_id=actor['tenant_id'], kind=kind,
        warehouse_id=after['warehouse_id'], sku_id=after['sku_id'], quantity=quantity,
        source=source, note=note, actor_id=actor['id'], created_at=timestamp, status='completed'))
    actual_delta = {f: after[f] - before[f] for f in FIELDS}
    c.execute(insert(db.ledger).values(
        id=uid(), tenant_id=actor['tenant_id'], owner_id=actor['tenant_id'],
        warehouse_id=after['warehouse_id'], sku_id=after['sku_id'], document_id=doc_id,
        kind=kind, delta=actual_delta, before=before, after={f: after[f] for f in FIELDS},
        actor_id=actor['id'], created_at=timestamp))
    c.execute(insert(db.outbox).values(
        id=uid(), tenant_id=actor['tenant_id'], document_id=doc_id,
        event_type='inventory.changed',
        payload={'warehouse_id': after['warehouse_id'], 'sku_id': after['sku_id'], 'version': after['version']},
        status='not_connected', created_at=timestamp))
    from .events import register_stock_event
    register_stock_event(c, actor, after, doc_id, kind, source, actual_delta, timestamp,
                         occurred_at=occurred_at, business_reference=business_reference,
                         source_account=source_account)
    from .catalog import record_movement
    record_movement(c, actor, after, kind, doc_id)
    row.clear()
    row.update(after)
    return doc_id


def _apply_stock(c, actor, kind, payload, *, business_reference=None):
    row = load_balance(c, actor, payload['warehouse_id'], payload['sku_id'])
    if row['version'] != payload['expected_version']:
        raise BusinessError('VERSION_CONFLICT', '库存已变化，请刷新库存并重新确认')
    if kind == 'receipt':
        delta = {f: payload[f] for f in ('g', 'q', 'd')}
        if any(type(v) is not int or v < 0 for v in delta.values()) or sum(delta.values()) <= 0:
            raise BusinessError('INVALID_QUANTITY', '实收数量须为非负整数且合计大于零', 422)
        quantity = sum(delta.values())
    else:
        quantity = payload['quantity']
        if type(quantity) is not int or quantity < 1:
            raise BusinessError('INVALID_QUANTITY', '交货数量须为正整数', 422)
        if quantity > available(row) or quantity > row['offline'] + max(0, unallocated(row)):
            raise BusinessError('INSUFFICIENT_STOCK', '本仓可用或线下可用额度不足，不可消耗线上已承诺额度')
        delta = {'g': -quantity, 'offline': -min(row['offline'], quantity)}
    doc_id = movement(c, actor, row, delta, kind, payload['source'], payload.get('note', ''),
                      payload['expected_version'], occurred_at=payload.get('occurred_at'),
                      business_reference=business_reference, source_account=payload.get('source_account', ''))
    return {'document_id': doc_id, 'kind': kind, 'quantity': quantity,
            'version': row['version'], 'available': available(row),
            'balance': {f: row[f] for f in FIELDS}, 'sync_status': 'not_connected'}


def legacy_stock_action(engine, actor, kind, payload, key):
    require_role(actor, *({'receipt': ('admin', 'warehouse'), 'sale': ('admin', 'cashier')}[kind]))
    from .workflows import command as legacy_command
    def apply(c):
        if kind == 'receipt' and not engine.runtime_settings.allow_legacy_receipts:
            raise BusinessError('RECEIPT_REFERENCE_REQUIRED',
                                '新增收货须提供来源主体与到货凭证，请使用新版收货入口', 422)
        if kind == 'sale' and not engine.runtime_settings.allow_legacy_sales:
            raise BusinessError('SHIPMENT_REFERENCE_REQUIRED',
                                '新增交货须提供来源主体与交货凭证，请使用新版交货入口', 422)
        result = _apply_stock(c, actor, kind, payload)
        result['deduplication'] = 'request_only_legacy'
        return result
    def authorize(c):
        get_warehouse(c, actor, payload['warehouse_id'], True)
        get_sku(c, actor, payload['sku_id'])
    return legacy_command(engine, actor, kind, payload, key, apply, authorize=authorize)


def receive(engine, actor, body, key):
    """One source voucher identifies one receipt line; split deliveries use subrefs."""
    require_role(actor, 'admin', 'warehouse')
    for field in ('source_account', 'reference'):
        if not isinstance(body.get(field), str) or not body[field].strip() or len(body[field]) > 120:
            raise BusinessError('MISSING_REFERENCE', '请填写来源主体和本次到货凭证号', 422)
    def apply(c):
        get_warehouse(c, actor, body['warehouse_id'], True)
        get_sku(c, actor, body['sku_id'])
        digest = fingerprint({k: v for k, v in body.items() if k != 'expected_version'})
        old = c.execute(select(vouchers).where(
            vouchers.c.tenant_id == actor['tenant_id'],
            vouchers.c.source_account == body['source_account'],
            vouchers.c.reference == body['reference'])).mappings().first()
        if old:
            if old['fingerprint'] != digest:
                raise BusinessError('VOUCHER_CONFLICT', '此到货凭证已登记为其他内容，请核对原单，不得重复收货')
            return {**old['response'], 'business_replayed': True}
        result = _apply_stock(c, actor, 'receipt', body, business_reference=body['reference'])
        result.update(deduplication='request_and_business', business_replayed=False,
                      source_account=body['source_account'], reference=body['reference'])
        c.execute(insert(vouchers).values(
            id=uid(), tenant_id=actor['tenant_id'], source_account=body['source_account'],
            reference=body['reference'], fingerprint=digest, warehouse_id=body['warehouse_id'],
            sku_id=body['sku_id'], document_id=result['document_id'], response=result, created_at=now()))
        return result
    return command(engine, actor, 'inventory.receipt', body, key, apply)


def sell(engine, actor, body, key):
    """One shipment voucher identifies one SKU delivery; use subrefs for splits."""
    require_role(actor, 'admin', 'cashier')
    for field in ('source_account', 'reference'):
        if not isinstance(body.get(field), str) or not body[field].strip() or len(body[field]) > 120:
            raise BusinessError('MISSING_REFERENCE', '请填写来源主体和本次交货凭证号', 422)
    def apply(c):
        get_warehouse(c, actor, body['warehouse_id'], True)
        get_sku(c, actor, body['sku_id'])
        digest = fingerprint({k: v for k, v in body.items() if k != 'expected_version'})
        old = c.execute(select(shipment_vouchers).where(
            shipment_vouchers.c.tenant_id == actor['tenant_id'],
            shipment_vouchers.c.source_account == body['source_account'],
            shipment_vouchers.c.reference == body['reference'])).mappings().first()
        if old:
            if old['fingerprint'] != digest:
                raise BusinessError('VOUCHER_CONFLICT', '此交货凭证已登记为其他内容，请核对原单，不得重复扣库')
            return {**old['response'], 'business_replayed': True}
        result = _apply_stock(c, actor, 'sale', body, business_reference=body['reference'])
        result.update(deduplication='request_and_business', business_replayed=False,
                      source_account=body['source_account'], reference=body['reference'])
        c.execute(insert(shipment_vouchers).values(
            id=uid(), tenant_id=actor['tenant_id'], source_account=body['source_account'],
            reference=body['reference'], fingerprint=digest, warehouse_id=body['warehouse_id'],
            sku_id=body['sku_id'], document_id=result['document_id'], response=result, created_at=now()))
        return result
    return command(engine, actor, 'inventory.sale', body, key, apply)
