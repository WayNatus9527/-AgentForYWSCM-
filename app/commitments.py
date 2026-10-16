"""Explicit quota/commitment state transitions, ready for M4 fulfillment.

Converting 30 of a 100-unit quota leaves 70 quota + 30 order reservations.
No physical stock is created. A pending withdrawal stays encumbered until ack.
Legacy aggregate quota is deliberately not assigned to an invented account.
"""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON
from sqlalchemy import UniqueConstraint as U, CheckConstraint as CK, select, insert, update, func
from . import db, inventory as inv
from .domain import BusinessError, uid, now, require_role, get_warehouse, get_sku

grants = Table(
    'channel_grants', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False),
    C('source_account', S, nullable=False), C('reference', S, nullable=False),
    C('authorized', I, nullable=False), C('free', I, nullable=False),
    C('withdrawing', I, nullable=False), C('withdrawn', I, nullable=False),
    C('restore_on_cancel', I, nullable=False), C('version', I, nullable=False),
    C('fingerprint', S, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'source_account', 'reference'),
    CK('authorized > 0 AND free >= 0 AND withdrawing >= 0 AND withdrawn >= 0'),
    CK('free + withdrawing + withdrawn <= authorized'))
reservations = Table(
    'order_commitments', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('grant_id', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('source_account', S, nullable=False),
    C('order_id', S, nullable=False), C('line_id', S, nullable=False),
    C('quantity', I, nullable=False), C('reserved', I, nullable=False),
    C('cancelled', I, nullable=False), C('fulfilled', I, nullable=False),
    C('version', I, nullable=False), C('fingerprint', S, nullable=False),
    C('created_at', S, nullable=False),
    U('tenant_id', 'source_account', 'order_id', 'line_id'),
    CK('quantity > 0 AND reserved >= 0 AND cancelled >= 0 AND fulfilled >= 0'),
    CK('reserved + cancelled + fulfilled = quantity'))
transitions = Table(
    'commitment_transitions', db.metadata,
    C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('source_account', S, nullable=False), C('action_id', S, nullable=False),
    C('kind', S, nullable=False), C('grant_id', S, nullable=False), C('reservation_id', S),
    C('quantity', I, nullable=False), C('document_id', S, nullable=False),
    C('fingerprint', S, nullable=False), C('response', JSON, nullable=False),
    C('actor_id', S, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'source_account', 'action_id'), CK('quantity > 0'))
TABLE_NAMES = ('channel_grants', 'order_commitments', 'commitment_transitions')


def positive(value, label='数量'):
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise BusinessError('INVALID_QUANTITY', label + '须为1至1000000的整数', 422)
    return value


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise BusinessError('INVALID_REFERENCE', '业务编号须为1至120个字符', 422)
    return value


def _get(c, table, actor, identity):
    row = c.execute(select(table).where(table.c.id == identity,
                                      table.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row:
        raise BusinessError('NOT_FOUND', '额度或占用不存在', 404)
    get_warehouse(c, actor, row['warehouse_id'], True)
    get_sku(c, actor, row['sku_id'])
    return dict(row)


def _version(row, value):
    if type(value) is not int or value != row['version']:
        raise BusinessError('VERSION_CONFLICT', '额度或占用已变化，请重新核对')


def _digest(kind, body):
    return inv.fingerprint({'operation': kind, **{
        k: v for k, v in body.items() if k not in ('expected_version', 'grant_version', 'reservation_version')}})


def _previous(c, actor, account, action_id, digest):
    text(action_id)
    row = c.execute(select(transitions).where(
        transitions.c.tenant_id == actor['tenant_id'], transitions.c.source_account == account,
        transitions.c.action_id == action_id)).mappings().first()
    if row:
        if row['fingerprint'] != digest:
            raise BusinessError('BUSINESS_ACTION_CONFLICT', '此业务动作编号已用于不同内容')
        return {**row['response'], 'business_replayed': True}
    return None


def _record(c, actor, grant, kind, body, digest, doc, stock, reservation=None):
    result = {'grant': grant, 'reservation': reservation, 'document_id': doc,
              'stock_version': stock['version'], 'balance': {f: stock[f] for f in inv.FIELDS},
              'business_replayed': False}
    c.execute(insert(transitions).values(
        id=uid(), tenant_id=actor['tenant_id'], source_account=grant['source_account'],
        action_id=body['action_id'], kind=kind, grant_id=grant['id'],
        reservation_id=reservation['id'] if reservation else None, quantity=body['quantity'],
        document_id=doc, fingerprint=digest, response=result, actor_id=actor['id'], created_at=now()))
    return result


def authorize(engine, actor, body, key):
    require_role(actor, 'admin')
    positive(body['quantity']);text(body['source_account']);text(body['reference']);text(body['action_id'])
    if type(body['restore_on_cancel']) is not bool:
        raise BusinessError('INVALID_POLICY', '取消回补策略须明确选择', 422)
    def apply(c):
        stock = inv.load_balance(c, actor, body['warehouse_id'], body['sku_id'])
        digest = _digest('authorize', body)
        prior = _previous(c, actor, body['source_account'], body['action_id'], digest)
        if prior:
            return prior
        old = c.execute(select(grants).where(
            grants.c.tenant_id == actor['tenant_id'], grants.c.source_account == body['source_account'],
            grants.c.reference == body['reference'])).mappings().first()
        if old:
            raise BusinessError('GRANT_EXISTS', '该授权凭证已登记，请查看原额度')
        grant = dict(id=uid(), tenant_id=actor['tenant_id'], warehouse_id=body['warehouse_id'],
                     sku_id=body['sku_id'], source_account=body['source_account'], reference=body['reference'],
                     authorized=body['quantity'], free=body['quantity'], withdrawing=0, withdrawn=0,
                     restore_on_cancel=int(body['restore_on_cancel']), version=1, fingerprint=digest, created_at=now())
        c.execute(insert(grants).values(**grant))
        doc = inv.movement(c, actor, stock, {'online': body['quantity']}, 'quota_authorize',
                           grant['id'], body['reference'], body['expected_version'],
                           business_reference=body['action_id'])
        return _record(c, actor, grant, 'authorize', body, digest, doc, stock)
    return inv.command(engine, actor, 'commitment.authorize', body, key, apply)


def reserve(engine, actor, grant_id, body, key):
    require_role(actor, 'admin', 'warehouse')
    positive(body['quantity']);text(body['order_id']);text(body['line_id']);text(body['action_id'])
    def apply(c):
        grant = _get(c, grants, actor, grant_id)
        digest = _digest('reserve', {**body, 'grant_id': grant_id})
        prior = _previous(c, actor, grant['source_account'], body['action_id'], digest)
        if prior:
            return prior
        old = c.execute(select(reservations.c.id).where(
            reservations.c.tenant_id == actor['tenant_id'],
            reservations.c.source_account == grant['source_account'],
            reservations.c.order_id == body['order_id'], reservations.c.line_id == body['line_id'])).first()
        if old:
            raise BusinessError('ORDER_LINE_EXISTS', '此来源订单行已建立占用，请处理原记录')
        _version(grant, body['grant_version'])
        n = body['quantity']
        if n > grant['free']:
            raise BusinessError('INSUFFICIENT_QUOTA', '订单数量超过此来源剩余额度')
        stock = inv.load_balance(c, actor, grant['warehouse_id'], grant['sku_id'])
        row = dict(id=uid(), tenant_id=actor['tenant_id'], grant_id=grant_id,
                   warehouse_id=grant['warehouse_id'], sku_id=grant['sku_id'],
                   source_account=grant['source_account'], order_id=body['order_id'], line_id=body['line_id'],
                   quantity=n, reserved=n, cancelled=0, fulfilled=0, version=1,
                   fingerprint=digest, created_at=now())
        c.execute(insert(reservations).values(**row))
        grant.update(free=grant['free'] - n, version=grant['version'] + 1)
        c.execute(update(grants).where(grants.c.id == grant_id).values(**grant))
        doc = inv.movement(c, actor, stock, {'online': -n, 'r': n}, 'quota_to_order',
                           row['id'], body['order_id'] + '/' + body['line_id'], body['expected_version'],
                           business_reference=body['action_id'])
        return _record(c, actor, grant, 'reserve', body, digest, doc, stock, row)
    return inv.command(engine, actor, 'commitment.reserve:' + grant_id, body, key, apply)


def cancel(engine, actor, reservation_id, body, key):
    require_role(actor, 'admin', 'warehouse')
    positive(body['quantity']);text(body['action_id']);text(body['reason'])
    def apply(c):
        row = _get(c, reservations, actor, reservation_id)
        grant = _get(c, grants, actor, row['grant_id'])
        digest = _digest('cancel', {**body, 'reservation_id': reservation_id})
        prior = _previous(c, actor, grant['source_account'], body['action_id'], digest)
        if prior:
            return prior
        _version(grant, body['grant_version']);_version(row, body['reservation_version'])
        n = body['quantity']
        if n > row['reserved']:
            raise BusinessError('OVER_CANCELLATION', '只能取消尚未履约和未取消的占用量')
        stock = inv.load_balance(c, actor, grant['warehouse_id'], grant['sku_id'])
        delta = {'r': -n}
        if grant['restore_on_cancel']:
            delta['online'] = n
            grant['free'] += n
        else:
            grant['withdrawn'] += n
        grant['version'] += 1
        row.update(reserved=row['reserved'] - n, cancelled=row['cancelled'] + n, version=row['version'] + 1)
        c.execute(update(grants).where(grants.c.id == grant['id']).values(**grant))
        c.execute(update(reservations).where(reservations.c.id == row['id']).values(**row))
        doc = inv.movement(c, actor, stock, delta, 'order_cancel', row['id'], body['reason'],
                           body['expected_version'], business_reference=body['action_id'])
        return _record(c, actor, grant, 'cancel', body, digest, doc, stock, row)
    return inv.command(engine, actor, 'commitment.cancel:' + reservation_id, body, key, apply)


def withdraw(engine, actor, grant_id, body, key, *, confirm=False):
    require_role(actor, 'admin')
    positive(body['quantity']);text(body['action_id']);text(body['reason'])
    operation = 'confirm_withdrawal' if confirm else 'request_withdrawal'
    def apply(c):
        grant = _get(c, grants, actor, grant_id)
        digest = _digest(operation, {**body, 'grant_id': grant_id})
        prior = _previous(c, actor, grant['source_account'], body['action_id'], digest)
        if prior:
            return prior
        _version(grant, body['grant_version'])
        n = body['quantity']
        if n > grant['withdrawing' if confirm else 'free']:
            raise BusinessError('INSUFFICIENT_QUOTA', '撤回数量超过可操作额度')
        stock = inv.load_balance(c, actor, grant['warehouse_id'], grant['sku_id'])
        if confirm:
            grant.update(withdrawing=grant['withdrawing'] - n, withdrawn=grant['withdrawn'] + n)
            delta = {'withdrawing': -n}
        else:
            grant.update(free=grant['free'] - n, withdrawing=grant['withdrawing'] + n)
            delta = {'online': -n, 'withdrawing': n}
        grant['version'] += 1
        c.execute(update(grants).where(grants.c.id == grant_id).values(**grant))
        doc = inv.movement(c, actor, stock, delta, 'quota_' + operation, grant_id, body['reason'],
                           body['expected_version'], business_reference=body['action_id'])
        return _record(c, actor, grant, operation, body, digest, doc, stock)
    return inv.command(engine, actor, 'commitment.' + operation + ':' + grant_id, body, key, apply)


def list_records(engine, actor, kind, offset=0, limit=50):
    if kind not in ('grants', 'reservations') or type(offset) is not int or offset < 0 or not 1 <= limit <= 100:
        raise BusinessError('INVALID_QUERY', '分页参数无效', 422)
    require_role(actor, 'admin', 'warehouse')
    table = grants if kind == 'grants' else reservations
    from .accounts import assert_current
    with engine.connect() as c:
        assert_current(c, actor)
        conditions = [table.c.tenant_id == actor['tenant_id']]
        if actor['role'] != 'admin':
            conditions.append(table.c.warehouse_id.in_(actor['warehouse_ids']))
        total = c.scalar(select(func.count()).select_from(table).where(*conditions))
        items = [dict(r) for r in c.execute(select(table).where(*conditions).order_by(
            table.c.created_at, table.c.id).offset(offset).limit(limit)).mappings()]
        return dict(items=items, total=total, offset=offset, limit=limit)
