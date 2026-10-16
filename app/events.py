"""Append-only process evidence. Missing historic occurrence times remain unknown."""
from datetime import timedelta
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import Table, Column, String, Integer, JSON, UniqueConstraint, CheckConstraint, insert, select
from . import db
from .clock import parse_timestamp, utc_now
from .config import settings_for
from contextvars import ContextVar

request_context = ContextVar('business_request_id', default=None)

runtime_bindings = Table('runtime_bindings', db.metadata,
    Column('id', String, primary_key=True), Column('fingerprint', String, nullable=False),
    Column('run_id', String, nullable=False), Column('runtime_mode', String, nullable=False),
    Column('timezone', String, nullable=False))

business_events = Table('business_events', db.metadata,
    Column('id', String, primary_key=True), Column('run_id', String, nullable=False),
    Column('tenant_id', String, nullable=False), Column('event_type', String, nullable=False),
    Column('lifecycle', String, nullable=False), Column('object_type', String, nullable=False),
    Column('object_id', String, nullable=False), Column('object_line_id', String),
    Column('process_id', String), Column('batch_id', String), Column('task_id', String),
    Column('causal_event_id', String), Column('correction_of', String),
    Column('warehouse_id', String), Column('sku_id', String), Column('supplier_id', String),
    Column('resource_group', String), Column('quantity', Integer), Column('unit', String),
    Column('planned_at', String), Column('occurred_at', String), Column('received_at', String, nullable=False),
    Column('registered_at', String, nullable=False), Column('source_timezone', String, nullable=False),
    Column('source_system', String, nullable=False), Column('source_account', String, nullable=False),
    Column('source_event_id', String), Column('source_version', String),
    Column('time_quality', String, nullable=False), Column('missing_fields', JSON, nullable=False),
    Column('quality_flags', JSON, nullable=False), Column('reason_code', String),
    Column('evidence_refs', JSON, nullable=False), Column('dependencies', JSON, nullable=False),
    Column('rule_version', String), Column('calendar_version', String), Column('schema_version', Integer, nullable=False),
    Column('actor_id', String, nullable=False), Column('request_id', String),
    Column('business_reference', String), Column('payload', JSON, nullable=False),
    Column('content_hash', String, nullable=False),
    UniqueConstraint('run_id', 'tenant_id', 'source_system', 'source_account', 'source_event_id'),
    CheckConstraint('quantity IS NULL OR quantity >= 0'),
    CheckConstraint("time_quality IN ('unknown','reported','observed','verified')"),
    CheckConstraint("lifecycle IN ('registered','queued','started','paused','resumed','completed','cancelled','corrected')"))
TABLE_NAMES = ('runtime_bindings', 'business_events')
LIFECYCLES = {'registered', 'queued', 'started', 'paused', 'resumed', 'completed', 'cancelled', 'corrected'}
QUALITIES = {'unknown', 'reported', 'observed', 'verified'}


def _error(code, message, status=422):
    from .domain import BusinessError
    raise BusinessError(code, message, status)


def _text(value, name, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        _error('INVALID_EVENT', f'{name} must be non-empty text, maximum 256 characters')
    return value.strip()


def _timestamp(value, name, *, required=False):
    if value is None and not required:
        return None
    try:
        return parse_timestamp(value).isoformat()
    except (ValueError, TypeError, OverflowError):
        _error('INVALID_EVENT_TIME', f'{name} must include a valid explicit UTC offset')


def record_event(c, actor, *, event_type, object_type, object_id, lifecycle='registered',
                 occurred_at=None, received_at=None, registered_at=None, planned_at=None,
                 source_timezone=None, source_system='local', source_account='', source_event_id=None,
                 source_version=None, time_quality=None, object_line_id=None, process_id=None,
                 batch_id=None, task_id=None, causal_event_id=None, correction_of=None,
                 warehouse_id=None, sku_id=None, supplier_id=None, resource_group=None,
                 quantity=None, unit=None, dependencies=None, evidence_refs=None,
                 reason_code=None, rule_version=None, calendar_version=None, request_id=None,
                 business_reference=None, payload=None):
    """Append an event within the caller's business transaction; return event ID.

    Corrections append a new row referencing the original. They never rewrite
    evidence. Source-event retries are idempotent, but changed source content is
    rejected. No historical ledger timestamps are converted into occurrences.
    """
    settings = settings_for(c.engine)
    tenant = _text(actor.get('tenant_id'), 'tenant_id', required=True)
    actor_id = _text(actor.get('id'), 'actor_id', required=True)
    from .accounts import assert_current
    from .domain import get_warehouse, get_sku
    assert_current(c, actor)
    if warehouse_id:
        get_warehouse(c, actor, warehouse_id)
    if sku_id:
        get_sku(c, actor, sku_id)
    if lifecycle not in LIFECYCLES:
        _error('INVALID_EVENT', 'Unsupported lifecycle stage')
    if quantity is not None and (not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0):
        _error('INVALID_EVENT', 'Event quantity must be a non-negative integer')
    if not isinstance(source_account, str) or len(source_account) > 256:
        _error('INVALID_EVENT', 'Invalid source account')
    source_timezone = source_timezone or settings.timezone
    try:
        ZoneInfo(source_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        _error('INVALID_EVENT_TIME', 'Invalid source timezone')
    occurred = _timestamp(occurred_at, 'occurred_at')
    planned = _timestamp(planned_at, 'planned_at')
    registered = _timestamp(registered_at or utc_now(), 'registered_at', required=True)
    received = _timestamp(received_at or registered, 'received_at', required=True)
    quality = time_quality or ('reported' if occurred else 'unknown')
    if quality not in QUALITIES or (occurred is None and quality != 'unknown'):
        _error('INVALID_EVENT', 'Missing occurrence time must remain unknown')
    # Unknown actual time may be explicitly retained even with an untrusted source
    # value. Negative durations are preserved and flagged, never clamped to zero.
    flags = []
    if occurred and parse_timestamp(occurred) > parse_timestamp(received):
        flags.append('occurred_after_received')
    if parse_timestamp(received) > parse_timestamp(registered):
        flags.append('received_after_registered')
    missing = [name for name, value in [('occurred_at', occurred), ('planned_at', planned),
                                       ('rule_version', rule_version), ('calendar_version', calendar_version)] if value is None]
    if correction_of and lifecycle != 'corrected':
        _error('INVALID_EVENT', 'A correction must use the corrected lifecycle')
    if lifecycle == 'corrected' and not correction_of:
        _error('INVALID_EVENT', 'A correction must reference an original event')
    refs = [('causal_event_id', causal_event_id), ('correction_of', correction_of)]
    for name, reference in refs:
        if reference:
            original = c.execute(select(business_events).where(business_events.c.id == reference,
                        business_events.c.tenant_id == tenant, business_events.c.run_id == settings.run_id)).mappings().first()
            if not original:
                _error('EVENT_REFERENCE_NOT_FOUND', 'Event reference does not exist in this tenant and runtime', 404)
            if name == 'correction_of' and (original['object_type'] != object_type or original['object_id'] != object_id):
                _error('INVALID_EVENT', 'A correction must refer to the same business object')
    values = dict(run_id=settings.run_id, tenant_id=tenant, event_type=_text(event_type, 'event_type', required=True),
        object_type=_text(object_type, 'object_type', required=True), object_id=_text(object_id, 'object_id', required=True),
        lifecycle=lifecycle, actor_id=actor_id, quantity=quantity, source_timezone=source_timezone,
        source_system=_text(source_system, 'source_system', required=True), source_account=source_account,
        occurred_at=occurred, planned_at=planned, received_at=received, registered_at=registered,
        time_quality=quality, missing_fields=missing, quality_flags=flags, schema_version=1,
        dependencies=[] if dependencies is None else dependencies,
        evidence_refs=[] if evidence_refs is None else evidence_refs,
        payload={} if payload is None else payload)
    for name, value in [('source_event_id', source_event_id), ('source_version', source_version),
        ('object_line_id', object_line_id), ('process_id', process_id), ('batch_id', batch_id), ('task_id', task_id),
        ('causal_event_id', causal_event_id), ('correction_of', correction_of), ('warehouse_id', warehouse_id),
        ('sku_id', sku_id), ('supplier_id', supplier_id), ('resource_group', resource_group), ('unit', unit),
        ('reason_code', reason_code), ('rule_version', rule_version), ('calendar_version', calendar_version),
        ('request_id', request_id), ('business_reference', business_reference)]:
        values[name] = _text(value, name)
    if not isinstance(values['dependencies'], list) or not isinstance(values['evidence_refs'], list) or not isinstance(values['payload'], dict):
        _error('INVALID_EVENT', 'Dependencies/evidence must be arrays and payload must be an object')
    try:
        # Receiver metadata may differ on redelivery. Source occurrence, business
        # content and version must match; local request/actor are not source facts.
        canonical = {key: value for key, value in values.items() if key not in
                     {'received_at', 'registered_at', 'quality_flags', 'actor_id', 'request_id'}}
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True,
                                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        _error('INVALID_EVENT', 'Event content must be finite JSON data')
    if source_event_id:
        old = c.execute(select(business_events).where(business_events.c.run_id == settings.run_id,
                    business_events.c.tenant_id == tenant, business_events.c.source_system == values['source_system'],
                    business_events.c.source_account == source_account,
                    business_events.c.source_event_id == values['source_event_id'])).mappings().first()
        if old:
            if old['content_hash'] != digest:
                _error('EVENT_CONTENT_CONFLICT', 'Source event ID was already used with different content', 409)
            return old['id']
    event_id = str(uuid.uuid4())
    c.execute(insert(business_events).values(id=event_id, content_hash=digest, **values))
    return event_id


def register_stock_event(c, actor, row, doc_id, kind, source, delta, timestamp, *,
                         occurred_at=None, request_id=None, business_reference=None, source_account=''):
    """Capture a newly posted stock movement without inventing physical timing.

    `timestamp` is the registration timestamp only. User-supplied occurrence
    times are reported evidence, not verified ground truth. Historical data is
    not backfilled through this function.
    """
    if row['tenant_id'] != actor['tenant_id']:
        _error('INVALID_EVENT', 'Stock event tenant does not match actor', 403)
    unit = c.scalar(select(db.skus.c.unit).where(db.skus.c.id == row['sku_id'], db.skus.c.tenant_id == actor['tenant_id']))
    # Count physical units once during quality reclassification (Q -> G/D).
    physical = [delta.get(k, 0) for k in ('g', 'q', 'd')]
    quantity = max(sum(v for v in physical if v > 0), -sum(v for v in physical if v < 0))
    if not quantity:
        quantity = max((abs(v) for v in delta.values()), default=0)
    return record_event(c, actor, event_type='inventory.posted', object_type='stock_document', object_id=doc_id,
        lifecycle='registered', occurred_at=occurred_at, received_at=timestamp, registered_at=timestamp,
        source_system='local_inventory', source_event_id=doc_id, source_account=source_account,
        quantity=quantity, unit=unit,
        warehouse_id=row['warehouse_id'], sku_id=row['sku_id'],
        time_quality='reported' if occurred_at is not None else 'unknown',
        request_id=request_id or request_context.get(), business_reference=business_reference,
        rule_version='inventory-m1-v1', payload={'kind': kind, 'source': source, 'delta': delta,
        'owner_id': row['owner_id'], 'balance_id': row['id'], 'balance_version': row['version']})


def install_append_only_guards(c):
    """Prevent UPDATE/DELETE through normal database access on either backend."""
    if c.dialect.name == 'sqlite':
        for operation in ('UPDATE', 'DELETE'):
            c.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS business_events_no_{operation.lower()} "
                              f"BEFORE {operation} ON business_events BEGIN "
                              "SELECT RAISE(ABORT, 'business_events is append only'); END")
    elif c.dialect.name == 'postgresql':
        c.exec_driver_sql("""CREATE OR REPLACE FUNCTION reject_business_event_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'business_events is append only'; END; $$""")
        c.exec_driver_sql('DROP TRIGGER IF EXISTS business_events_append_only ON business_events')
        c.exec_driver_sql('CREATE TRIGGER business_events_append_only BEFORE UPDATE OR DELETE ON business_events '
                          'FOR EACH ROW EXECUTE FUNCTION reject_business_event_mutation()')
