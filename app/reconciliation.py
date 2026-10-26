"""Watermarked observations never write local stock; missing coverage stays unknown."""
from sqlalchemy import select, insert, func
from . import db, m4_schema as s, m4_models as models, inventory as inv, fulfillment
from .domain import require_role, uid, now
from .m3_common import fail
from .m4_common import connection, scope, validate, command, anomaly


def snapshot(engine, actor, connection_id, body, key):
    require_role(actor, 'admin'); body = validate(models.Snapshot, body)
    def authorize(c):
        scope(c, actor, connection(c, actor, connection_id), body['warehouse_id'], body['sku_id'])
    def apply(c):
        old = c.execute(select(s.snapshots).where(s.snapshots.c.tenant_id == actor['tenant_id'], s.snapshots.c.connection_id == connection_id, s.snapshots.c.snapshot_id == body['snapshot_id'])).mappings().first()
        digest = inv.fingerprint(body)
        if old:
            if old['fingerprint'] != digest: fail('SNAPSHOT_CONFLICT', '快照编号已用于不同内容')
            return dict(old)
        row = dict(id=uid(), tenant_id=actor['tenant_id'], connection_id=connection_id, **body, fingerprint=digest, created_at=now())
        c.execute(insert(s.snapshots).values(**row)); return row
    return command(engine, actor, 'snapshot:' + connection_id, body, key, apply, authorize)


def get(c, actor, identity):
    row = c.execute(select(s.snapshots).where(s.snapshots.c.id == identity, s.snapshots.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND', '快照不存在', 404)
    scope(c, actor, connection(c, actor, row['connection_id']), row['warehouse_id'], row['sku_id'])
    return dict(row)


def compare(engine, actor, identity, key):
    require_role(actor, 'admin', 'warehouse', 'operations')
    def apply(c):
        snap = get(c, actor, identity); mark = snap['covered_watermark']
        stock = inv.load_balance(c, actor, snap['warehouse_id'], snap['sku_id'])
        known = fulfillment.stock_known(c, actor, snap['warehouse_id'], snap['sku_id'])
        delta_rows = [dict(x) for x in c.execute(select(s.deltas).where(s.deltas.c.connection_id == snap['connection_id'],
            s.deltas.c.tenant_id == actor['tenant_id'], s.deltas.c.warehouse_id == snap['warehouse_id'],
            s.deltas.c.sku_id == snap['sku_id']).order_by(s.deltas.c.sequence)).mappings()]
        uncovered = [x for x in delta_rows if mark is not None and x['sequence'] > mark]
        expected_sequence = list(range(mark + 1, max([mark] + [x['sequence'] for x in uncovered]) + 1)) if mark is not None else []
        gaps = [i for i in expected_sequence if i not in {x['sequence'] for x in uncovered}]
        unresolved = list(c.execute(select(s.inbox.c.id).where(s.inbox.c.connection_id == snap['connection_id'], s.inbox.c.warehouse_id == snap['warehouse_id'],
            s.inbox.c.sku_id == snap['sku_id'], s.inbox.c.kind == 'stock.delta', s.inbox.c.status.in_(('received', 'unknown', 'failed', 'conflict')))).scalars())
        external = {k: snap['quantities'][k] + sum(x['delta'][k] for x in uncovered) for k in ('g', 'q', 'd')} if mark is not None else None
        reasons = ([] if known else ['local_inventory_unknown']) + ([] if mark is not None else ['watermark_unknown']) + ([] if snap['snapshot_at'] else ['snapshot_time_unknown'])
        if gaps: reasons.append('delta_gap')
        if unresolved: reasons.append('unprocessed_delta')
        if external is not None and any(v < 0 for v in external.values()): reasons.append('invalid_external_quantity')
        difference = {k: stock[k] - external[k] for k in ('g', 'q', 'd')} if not reasons else None
        status = 'unknown' if reasons else 'matched' if not any(difference.values()) else 'different'
        from .operations import transfers, transfer_lines
        transit = c.scalar(select(func.coalesce(func.sum(transfer_lines.c.sent - transfer_lines.c.received - transfer_lines.c.lost), 0)).select_from(
            transfers.join(transfer_lines, transfers.c.id == transfer_lines.c.transfer_id)).where(transfers.c.tenant_id == actor['tenant_id'],
            transfers.c.destination_id == snap['warehouse_id'], transfer_lines.c.sku_id == snap['sku_id']))
        row = dict(id=uid(), tenant_id=actor['tenant_id'], connection_id=snap['connection_id'], snapshot_id=identity,
            warehouse_id=snap['warehouse_id'], sku_id=snap['sku_id'], status=status, rule_version='stock-gqd-watermark-v1',
            inputs=dict(local={k: stock[k] for k in ('g', 'q', 'd')}, local_version=stock['version'], local_known=known,
                source_snapshot=snap, uncovered_delta_ids=[x['id'] for x in uncovered], external=external,
                external_raw=snap['quantities'], incoming_transfer_in_transit=transit, missing_sequences=gaps,
                unresolved_inbox_ids=unresolved, unknown_reasons=reasons, compared_at=now(), mapping='g/q/d base units; transit separate'),
            difference=difference, created_at=now())
        c.execute(insert(s.reconciliations).values(**row))
        if status != 'matched':
            anomaly(c, actor, object_type='reconciliation', object_id=row['id'], warehouse_id=snap['warehouse_id'], sku_id=snap['sku_id'],
                rule='reconciliation_' + status, quantity=max([abs(x) for x in (difference or {}).values()] or [0]), evidence=[identity, row['id']])
        from .events import record_event
        record_event(c, actor, event_type='reconciliation.completed', object_type='reconciliation', object_id=row['id'],
            lifecycle='completed', warehouse_id=snap['warehouse_id'], sku_id=snap['sku_id'], rule_version=row['rule_version'],
            payload={'status': status, 'snapshot_id': identity, 'difference': difference}, evidence_refs=[identity])
        return row
    return command(engine, actor, 'reconcile:' + identity, {}, key, apply, lambda c: get(c, actor, identity))
