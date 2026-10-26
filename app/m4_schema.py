"""Additive M4 tables. Migration 7 owns these; existing migrations stay fixed."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, Float, JSON
from sqlalchemy import UniqueConstraint as U, CheckConstraint as CK
from .db import metadata


def table(name, *columns):
    return Table(name, metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False), *columns)


connections = table('connector_accounts', C('source_account', S, nullable=False), C('name', S, nullable=False),
    C('warehouse_ids', JSON, nullable=False), C('member_ids', JSON, nullable=False),
    C('adapter', S, nullable=False), C('supports_query', I, nullable=False), C('supports_idempotency', I, nullable=False),
    C('active', I, nullable=False), C('version', I, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'source_account'))
orders = table('sales_orders', C('connection_id', S, nullable=False), C('source_order_id', S, nullable=False),
    C('warehouse_ids', JSON, nullable=False), C('status', S, nullable=False), C('version', I, nullable=False),
    C('fingerprint', S, nullable=False), C('planned_at', S), C('occurred_at', S), C('channel_completed_at', S),
    C('created_at', S, nullable=False), U('tenant_id', 'connection_id', 'source_order_id'))
lines = table('sales_order_lines', C('order_id', S, nullable=False), C('source_line_id', S, nullable=False),
    C('sku_id', S, nullable=False), *[C(k, I, nullable=False) for k in ('quantity', 'reserved', 'shipped', 'cancelled')],
    U('order_id', 'source_line_id'), CK('quantity > 0 AND reserved >= 0 AND shipped >= 0 AND cancelled >= 0'),
    CK('reserved + shipped + cancelled <= quantity'))
batches = table('fulfillment_batches', C('order_id', S, nullable=False), C('line_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('commitment_id', S),
    C('quantity', I, nullable=False), C('reserved', I, nullable=False), C('shipped', I, nullable=False),
    C('cancelled', I, nullable=False), C('created_at', S, nullable=False),
    CK('quantity > 0 AND reserved >= 0 AND shipped >= 0 AND cancelled >= 0'), CK('reserved + shipped + cancelled = quantity'))
tasks = table('fulfillment_tasks', C('order_id', S, nullable=False), C('batch_id', S, nullable=False),
    C('kind', S, nullable=False), C('status', S, nullable=False), C('quantity', I, nullable=False),
    C('actor_id', S), C('queued_at', S, nullable=False), C('started_at', S), C('completed_at', S), U('batch_id', 'kind'))
shipments = table('sales_shipments', C('order_id', S, nullable=False), C('batch_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('quantity', I, nullable=False),
    C('document_id', S, nullable=False, unique=True), C('reference', S, nullable=False), C('action_id', S, nullable=False), C('fingerprint', S, nullable=False),
    C('occurred_at', S), C('delivered_at', S), C('created_at', S, nullable=False),
    U('tenant_id', 'order_id', 'reference'), CK('quantity > 0'))
actions = table('sales_actions', C('connection_id', S, nullable=False), C('action_id', S, nullable=False),
    C('fingerprint', S, nullable=False), C('response', JSON, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'connection_id', 'action_id'))
links = table('sales_supply_links', C('order_id', S, nullable=False), C('line_id', S, nullable=False),
    C('source_type', S, nullable=False), C('source_id', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('quantity', I, nullable=False), C('released', I, nullable=False),
    C('created_at', S, nullable=False), CK('quantity > 0 AND released >= 0 AND released <= quantity'))
history = table('sales_history', C('order_id', S, nullable=False), C('action', S, nullable=False),
    C('actor_id', S, nullable=False), C('payload', JSON, nullable=False), C('event_id', S, nullable=False), C('created_at', S, nullable=False))
inbox = table('connector_inbox', C('run_id', S, nullable=False), C('connection_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False),
    C('source_event_id', S, nullable=False), C('business_action_id', S, nullable=False), C('entity_id', S, nullable=False),
    C('source_version', I), C('kind', S, nullable=False), C('occurred_at', S), C('received_at', S, nullable=False),
    C('payload', JSON, nullable=False), C('fingerprint', S, nullable=False), C('status', S, nullable=False),
    C('result', JSON, nullable=False), C('error', S), U('run_id', 'tenant_id', 'connection_id', 'source_event_id'))
inbox_actions = table('connector_inbox_actions', C('connection_id', S, nullable=False), C('business_action_id', S, nullable=False),
    C('fingerprint', S, nullable=False), C('inbox_id', S, nullable=False), U('tenant_id', 'connection_id', 'business_action_id'))
source_states = table('connector_source_states', C('connection_id', S, nullable=False), C('entity_id', S, nullable=False),
    C('source_version', I, nullable=False), U('tenant_id', 'connection_id', 'entity_id'))
conflicts = table('connector_conflicts', C('connection_id', S, nullable=False), C('inbox_id', S, nullable=False),
    C('fingerprint', S, nullable=False), C('envelope', JSON, nullable=False), C('created_at', S, nullable=False),
    U('inbox_id', 'fingerprint'))
outbox = table('connector_outbox', C('connection_id', S, nullable=False), C('resource_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('sequence', I, nullable=False),
    C('kind', S, nullable=False), C('payload', JSON, nullable=False), C('status', S, nullable=False),
    C('attempts', I, nullable=False), C('retry_base', I, nullable=False), C('next_at', Float, nullable=False), C('lease_until', Float), C('lease_token', S),
    C('lease_mode', S), C('last_error', S), C('created_at', S, nullable=False),
    U('tenant_id', 'connection_id', 'resource_id', 'sequence'))
attempts = table('connector_delivery_attempts', C('outbox_id', S, nullable=False), C('token', S), C('action', S, nullable=False),
    C('payload', JSON, nullable=False), C('created_at', S, nullable=False))
effects = table('simulated_connector_effects', C('connection_id', S, nullable=False), C('business_number', S, nullable=False),
    C('fingerprint', S, nullable=False), C('status', S, nullable=False), C('payload', JSON, nullable=False),
    C('created_at', S, nullable=False), U('tenant_id', 'connection_id', 'business_number'))
source_events = table('simulated_source_events', C('connection_id', S, nullable=False), C('source_event_id', S, nullable=False),
    C('envelope', JSON, nullable=False), C('created_at', S, nullable=False), U('tenant_id', 'connection_id', 'source_event_id'))
snapshots = table('connector_snapshots', C('connection_id', S, nullable=False), C('snapshot_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('covered_watermark', I),
    C('snapshot_at', S), C('quantities', JSON, nullable=False), C('fingerprint', S, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'connection_id', 'snapshot_id'))
deltas = table('connector_stock_deltas', C('connection_id', S, nullable=False), C('warehouse_id', S, nullable=False),
    C('sku_id', S, nullable=False), C('sequence', I, nullable=False), C('delta', JSON, nullable=False),
    C('inbox_id', S, nullable=False), C('occurred_at', S),
    U('tenant_id', 'connection_id', 'warehouse_id', 'sku_id', 'sequence'))
reconciliations = table('stock_reconciliations', C('connection_id', S, nullable=False), C('snapshot_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('status', S, nullable=False),
    C('rule_version', S, nullable=False), C('inputs', JSON, nullable=False), C('difference', JSON), C('created_at', S, nullable=False))

TABLES = (connections, orders, lines, batches, tasks, shipments, actions, links, history, inbox, inbox_actions,
          source_states, conflicts, outbox, attempts, effects, source_events, snapshots, deltas, reconciliations)
TABLE_NAMES = tuple(t.name for t in TABLES)
APPEND_ONLY = tuple(t.name for t in (actions, history, inbox_actions, conflicts, attempts, source_events, snapshots, deltas, reconciliations))
