"""Migration 8: supply evidence and deterministic planning, all additive."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U, CheckConstraint as CK
from . import db

def table(name, *columns):
    return Table(name, db.metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False), *columns)

terms = table('purchase_terms', C('order_id', S, nullable=False), C('supplier_id', S, nullable=False),
    C('replacement_return_id', S),
    C('ordered_at', S), C('original_due', S), C('latest_due', S), C('fingerprint', S, nullable=False), U('order_id'))
lines = table('supply_lines', C('kind', S, nullable=False), C('order_id', S, nullable=False), C('source_line_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('source_warehouse_id', S), C('sku_id', S, nullable=False),
    C('quantity', I, nullable=False), C('closed_quantity', I, nullable=False), C('expected_at', S), C('created_at', S, nullable=False),
    U('kind', 'source_line_id'), CK('quantity > 0 AND closed_quantity >= 0 AND closed_quantity <= quantity'))
batches = table('supply_batches', C('supply_line_id', S, nullable=False), C('order_id', S, nullable=False), C('kind', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('source_warehouse_id', S), C('sku_id', S, nullable=False), C('reference', S, nullable=False),
    C('quantity', I, nullable=False), C('reserved', I, nullable=False), C('sent', I, nullable=False), C('received', I, nullable=False),
    C('lost', I, nullable=False), C('status', S, nullable=False), C('version', I, nullable=False),
    C('carrier', S), C('transport_mode', S), C('tracking_number', S), C('planned_ship_at', S), C('expected_at', S),
    C('shipped_at', S), C('arrived_at', S), C('created_at', S, nullable=False),
    U('tenant_id', 'kind', 'reference'), CK('quantity > 0 AND reserved >= 0 AND sent >= 0 AND received >= 0 AND lost >= 0 AND reserved + sent <= quantity AND received + lost <= sent'))
receipts = table('supply_receipts', C('batch_id', S, nullable=False), C('supply_line_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('reference', S, nullable=False), C('document_id', S, nullable=False),
    C('g', I, nullable=False), C('q', I, nullable=False), C('d', I, nullable=False), C('occurred_at', S), C('created_at', S, nullable=False),
    U('batch_id', 'reference'), U('document_id'), CK('g >= 0 AND q >= 0 AND d >= 0 AND g + q + d > 0'))
inspections = table('supply_inspection_links', C('receipt_id', S, nullable=False), C('session_id', S, nullable=False),
    C('quantity', I, nullable=False), C('created_at', S, nullable=False), U('session_id'), CK('quantity > 0'))
claims = table('supply_discrepancies', C('batch_id', S, nullable=False), C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False),
    C('reference', S, nullable=False), C('quantity', I, nullable=False), C('kind', S, nullable=False), C('details', JSON, nullable=False),
    C('status', S, nullable=False), C('version', I, nullable=False), C('batch_version', I, nullable=False),
    C('requester_id', S, nullable=False), C('reviewer_id', S), C('created_at', S, nullable=False),
    U('tenant_id', 'reference'), CK('quantity > 0'))
actions = table('supply_actions', C('operation', S, nullable=False), C('reference', S, nullable=False),
    C('fingerprint', S, nullable=False), C('response', JSON, nullable=False), U('tenant_id', 'operation', 'reference'))
history = table('supply_history', C('object_type', S, nullable=False), C('object_id', S, nullable=False),
    C('action', S, nullable=False), C('actor_id', S, nullable=False), C('payload', JSON, nullable=False),
    C('event_id', S, nullable=False), C('created_at', S, nullable=False))
demand = table('demand_days', C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('day', S, nullable=False),
    C('quantity', I, nullable=False), C('quality', S, nullable=False), C('source_reference', S, nullable=False),
    C('created_at', S, nullable=False), U('tenant_id', 'warehouse_id', 'sku_id', 'day'), CK('quantity >= 0'))
policies = table('replenishment_policies', C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('supplier_id', S, nullable=False),
    C('window_days', I, nullable=False), C('coverage_days', I, nullable=False), C('lead_days', I, nullable=False), C('safety', I, nullable=False),
    C('minimum', I, nullable=False), C('pack', I, nullable=False), C('max_daily', I, nullable=False), C('includes_open_orders', I, nullable=False),
    C('version', I, nullable=False), C('active', I, nullable=False), C('created_at', S, nullable=False),
    U('tenant_id', 'warehouse_id', 'sku_id'), CK('window_days > 0 AND coverage_days > 0 AND lead_days >= 0 AND safety >= 0 AND minimum >= 1 AND pack >= 1 AND max_daily >= 1'))
plans = table('replenishment_plans', C('policy_id', S, nullable=False), C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False),
    C('creator_id', S, nullable=False), C('auth_version', I, nullable=False), C('as_of', S, nullable=False), C('expires_at', S, nullable=False),
    C('status', S, nullable=False), C('suggested', I), C('snapshot', JSON, nullable=False), C('fingerprint', S, nullable=False), C('created_at', S, nullable=False))
decisions = table('replenishment_decisions', C('plan_id', S, nullable=False), C('quantity', I, nullable=False), C('reason', S, nullable=False),
    C('actor_id', S, nullable=False), C('order_id', S), C('created_at', S, nullable=False), U('plan_id'), CK('quantity >= 0'))

TABLES = (terms, lines, batches, receipts, inspections, claims, actions, history, demand, policies, plans, decisions)
APPEND_ONLY = tuple(t.name for t in (receipts, inspections, actions, history, demand, plans, decisions))
