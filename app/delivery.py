"""Persistent Outbox leases, fenced completion and original-number verification."""
import time
from typing import Protocol
from sqlalchemy import select, insert, update, func
from . import db, m4_schema as s, inventory as inv
from .domain import uid, now, require_role
from .accounts import assert_current
from .m3_common import fail
from .m4_common import connection, scope, anomaly, command


def enqueue(c, actor, connection_id, resource_id, kind, payload, warehouse, sku):
    account = connection(c, actor, connection_id); scope(c, actor, account, warehouse, sku)
    sequence = 1 + c.scalar(select(func.coalesce(func.max(s.outbox.c.sequence), 0)).where(
        s.outbox.c.tenant_id == actor['tenant_id'], s.outbox.c.connection_id == connection_id, s.outbox.c.resource_id == resource_id))
    row = dict(id=uid(), tenant_id=actor['tenant_id'], connection_id=connection_id, resource_id=resource_id,
        warehouse_id=warehouse, sku_id=sku, sequence=sequence, kind=kind, payload=payload, status='pending', attempts=0, retry_base=0,
        next_at=0.0, lease_until=None, lease_token=None, lease_mode=None, last_error=None, created_at=now())
    c.execute(insert(s.outbox).values(**row))
    audit(c, actor, row, 'queued', {})
    return row


def audit(c, actor, row, action, payload):
    c.execute(insert(s.attempts).values(id=uid(), tenant_id=actor['tenant_id'], outbox_id=row['id'],
        token=row['lease_token'], action=action, payload=payload, created_at=now()))
    from .events import record_event
    from .clock import business_now
    record_event(c, actor, event_type='delivery.' + action, object_type='connector_outbox', object_id=row['id'],
        process_id=row['resource_id'], warehouse_id=row['warehouse_id'], sku_id=row['sku_id'],
        lifecycle={'queued': 'queued', 'claimed': 'started', 'succeeded': 'completed', 'manual_retry': 'resumed'}.get(action, 'registered'),
        occurred_at=business_now(c.engine), time_quality='observed', rule_version='delivery-v1',
        business_reference=row['id'], payload=payload)


def get(c, actor, identity):
    row = c.execute(select(s.outbox).where(s.outbox.c.id == identity, s.outbox.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND', '外发动作不存在', 404)
    account = connection(c, actor, row['connection_id']); scope(c, actor, account, row['warehouse_id'], row['sku_id'])
    return dict(row), account


def claim(engine, actor, *, clock=time.time, lease_seconds=30):
    require_role(actor, 'admin')
    if not 1 <= lease_seconds <= 300: raise ValueError('Lease must be 1..300 seconds')
    instant = clock()
    with db.write(engine, actor['tenant_id']) as c:
        assert_current(c, actor)
        rows = c.execute(select(s.outbox).where(s.outbox.c.tenant_id == actor['tenant_id'], s.outbox.c.status != 'succeeded')
            .order_by(s.outbox.c.connection_id, s.outbox.c.resource_id, s.outbox.c.sequence)).mappings().all()
        seen = set()
        for candidate in rows:
            resource = (candidate['connection_id'], candidate['resource_id'])
            if resource in seen: continue
            seen.add(resource)
            row = dict(candidate)
            account = connection(c, actor, row['connection_id'], active=False)
            if not account['active']: continue
            if row['status'] in ('manual', 'dead') or row['next_at'] > instant: continue
            if row['status'] == 'leased' and row['lease_until'] > instant: continue
            mode = 'query' if row['status'] in ('unknown', 'leased') else 'execute'
            if mode == 'query' and not account['supports_query']:
                row.update(status='manual', lease_token=None, lease_until=None, last_error='QUERY_UNSUPPORTED')
                c.execute(update(s.outbox).where(s.outbox.c.id == row['id']).values(**row))
                audit(c, actor, row, 'manual', {'reason': row['last_error']})
                anomaly(c, actor, object_type='connector_outbox', object_id=row['id'], warehouse_id=row['warehouse_id'], sku_id=row['sku_id'], rule='unknown_delivery')
                continue
            row.update(status='leased', lease_mode=mode, lease_token=uid(), lease_until=instant + lease_seconds, attempts=row['attempts'] + 1)
            c.execute(update(s.outbox).where(s.outbox.c.id == row['id']).values(**row))
            audit(c, actor, row, 'claimed', {'mode': mode, 'attempt': row['attempts'], 'lease_until': row['lease_until']})
            return row
    return None


class ConnectorAdapter(Protocol):
    def execute(self, job): ...
    def query(self, job): ...
    def query_event(self, connection_id, source_event_id): ...
    def query_snapshot(self, connection_id, snapshot_id): ...


class SimulatedAdapter:
    """Durable simulated remote effects, separate transactions from local ack.

    Fault injection is a local constructor dependency used by the simulator;
    production network adapters are deliberately absent.
    """
    def __init__(self, engine, actor, faults=()):
        self.engine, self.actor, self.faults = engine, actor, list(faults)

    def execute(self, job):
        fault = self.faults.pop(0) if self.faults else 'success'
        if fault == 'rate_limit': return {'status': 'retry', 'error': 'RATE_LIMITED', 'retry_after': 5}
        if fault in ('timeout_before', 'disconnect'): return {'status': 'unknown', 'error': fault.upper()}
        if fault == 'failure': return {'status': 'failed', 'error': 'SOURCE_REJECTED'}
        if fault not in ('success', 'timeout_after', 'processing'): raise ValueError('Unknown simulated fault')
        with db.write(self.engine, self.actor['tenant_id']) as c:
            current, account = get(c, self.actor, job['id'])
            if current['payload'] != job['payload']: fail('PAYLOAD_CHANGED', '已领取动作内容不一致')
            digest = inv.fingerprint({'kind': job['kind'], 'payload': job['payload']})
            old = c.execute(select(s.effects).where(s.effects.c.tenant_id == job['tenant_id'], s.effects.c.connection_id == job['connection_id'], s.effects.c.business_number == job['id'])).mappings().first()
            if old:
                if old['fingerprint'] != digest: return {'status': 'failed', 'error': 'EXTERNAL_ID_CONFLICT'}
                status = old['status']
            else:
                status = 'processing' if fault == 'processing' else 'succeeded'
                c.execute(insert(s.effects).values(id=uid(), tenant_id=job['tenant_id'], connection_id=job['connection_id'],
                    business_number=job['id'], fingerprint=digest, status=status, payload=job['payload'], created_at=now()))
        return {'status': 'unknown', 'error': 'RESPONSE_TIMEOUT'} if fault == 'timeout_after' else {'status': status}

    def query(self, job):
        with self.engine.connect() as c:
            _, account = get(c, self.actor, job['id'])
            if not account['supports_query']: return {'status': 'unknown', 'error': 'QUERY_UNSUPPORTED'}
            row = c.execute(select(s.effects).where(s.effects.c.tenant_id == job['tenant_id'], s.effects.c.connection_id == job['connection_id'], s.effects.c.business_number == job['id'])).mappings().first()
            return {'status': row['status'] if row else 'not_found'}

    def query_event(self, connection_id, source_event_id):
        with self.engine.connect() as c:
            account = connection(c, self.actor, connection_id)
            if not account['supports_query']: return None
            row = c.execute(select(s.source_events).where(s.source_events.c.tenant_id == self.actor['tenant_id'],
                s.source_events.c.connection_id == connection_id, s.source_events.c.source_event_id == source_event_id)).mappings().first()
            return dict(row) if row else None

    def query_snapshot(self, connection_id, snapshot_id):
        with self.engine.connect() as c:
            account = connection(c, self.actor, connection_id)
            if not account['supports_query']: return None
            row = c.execute(select(s.snapshots).where(s.snapshots.c.tenant_id == self.actor['tenant_id'],
                s.snapshots.c.connection_id == connection_id, s.snapshots.c.snapshot_id == snapshot_id)).mappings().first()
            if row: scope(c, self.actor, account, row['warehouse_id'], row['sku_id'])
            return dict(row) if row else None


def finish(engine, actor, claimed, result, *, clock=time.time, max_attempts=5):
    instant = clock()
    with db.write(engine, actor['tenant_id']) as c:
        row, account = get(c, actor, claimed['id'])
        if row['status'] != 'leased' or row['lease_token'] != claimed['lease_token'] or row['lease_until'] <= instant:
            return {'id': row['id'], 'status': 'lease_lost'}
        outcome = result['status']; status = 'unknown'; error = result.get('error')
        if outcome == 'succeeded': status = 'succeeded'
        elif outcome == 'failed': status = 'dead'
        elif outcome == 'retry': status = 'retry' if row['attempts'] - row['retry_base'] < max_attempts else 'dead'
        elif outcome == 'not_found' and row['lease_mode'] == 'query':
            status = 'retry' if account['supports_query'] and account['supports_idempotency'] else 'manual'
            if row['attempts'] - row['retry_base'] >= max_attempts: status = 'dead'
        elif outcome in ('unknown', 'processing'):
            if not account['supports_query'] or row['attempts'] - row['retry_base'] >= max_attempts: status = 'manual'
        else: status = 'manual'; error = 'INVALID_ADAPTER_RESULT'
        row.update(status=status, next_at=instant + max(min(float(result.get('retry_after', 0)), 3600), min(2 ** min(row['attempts'], 10), 300)),
            lease_token=None, lease_until=None, last_error=error or (outcome if status != 'succeeded' else None))
        c.execute(update(s.outbox).where(s.outbox.c.id == row['id']).values(**row))
        audit(c, actor, {**row, 'lease_token': claimed['lease_token']}, status, {'mode': claimed['lease_mode'], 'result': result})
        if status in ('manual', 'dead'):
            anomaly(c, actor, object_type='connector_outbox', object_id=row['id'], warehouse_id=row['warehouse_id'], sku_id=row['sku_id'], rule='delivery_' + status)
        return row


def tick(engine, actor, *, adapter=None, clock=time.time, lease_seconds=30, after_external=None):
    job = claim(engine, actor, clock=clock, lease_seconds=lease_seconds)
    if not job: return {'status': 'idle'}
    adapter = adapter or SimulatedAdapter(engine, actor)
    try:
        result = adapter.query(job) if job['lease_mode'] == 'query' else adapter.execute(job)
    except (TimeoutError, ConnectionError):
        result = {'status': 'unknown', 'error': 'TRANSPORT_UNKNOWN'}
    if after_external: after_external(job)  # Crash boundary exercised by tests.
    return finish(engine, actor, job, result, clock=clock)


def retry(engine, actor, identity, body, key):
    require_role(actor, 'admin')
    if not isinstance(body.get('reason'), str) or not 2 <= len(body['reason']) <= 1000: fail('REASON_REQUIRED', '人工重试须填写核实说明', 422)
    def apply(c):
        row, _ = get(c, actor, identity)
        if row['status'] not in ('dead', 'manual'): fail('INVALID_STATE', '仅可人工重试死信或待接管动作')
        # Even a dead letter is queried before retransmission, with the same ID.
        row.update(status='unknown', retry_base=row['attempts'], next_at=0.0)
        c.execute(update(s.outbox).where(s.outbox.c.id == identity).values(**row))
        audit(c, actor, row, 'manual_retry', body); return row
    return command(engine, actor, 'outbox.retry:' + identity, body, key, apply, lambda c: get(c, actor, identity))
