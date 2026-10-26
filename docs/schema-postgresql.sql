-- PostgreSQL reference DDL, application version 0.10.1 / migration 10.

-- Runtime migrations install append-only guards and bind runtime identity.

-- This table reference is not a substitute for app.migrations.migrate.

CREATE TABLE admin_audit (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	actor_id VARCHAR,
	action VARCHAR,
	target_id VARCHAR,
	before JSON,
	after JSON,
	created_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE agent_action_audit (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	proposal_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	event VARCHAR NOT NULL,
	code VARCHAR,
	detail JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE agent_proposals (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	request_key VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	payload JSON NOT NULL,
	preview JSON NOT NULL,
	auth_version INTEGER NOT NULL,
	stock_version INTEGER NOT NULL,
	token VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	result JSON,
	created_at VARCHAR,
	expires_at VARCHAR,
	finished_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, actor_id, request_key)
);

CREATE TABLE analysis_improvements (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	report_id VARCHAR NOT NULL,
	row_id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (report_id, row_id)
);

CREATE TABLE analysis_job_history (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	job_id VARCHAR NOT NULL,
	actor_id VARCHAR,
	event VARCHAR NOT NULL,
	attempt INTEGER NOT NULL,
	error VARCHAR,
	reason VARCHAR,
	recorded_at FLOAT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (job_id, attempt, event)
);

CREATE INDEX analysis_job_history_page ON analysis_job_history (job_id, recorded_at, id);

CREATE TABLE analysis_jobs (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	creator_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	request JSON NOT NULL,
	status VARCHAR NOT NULL,
	lease_token VARCHAR,
	lease_until FLOAT,
	attempts INTEGER NOT NULL,
	attempt_limit INTEGER DEFAULT '3' NOT NULL,
	next_attempt_at FLOAT,
	started_at FLOAT,
	finished_at FLOAT,
	report_id VARCHAR,
	error VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX analysis_jobs_queue ON analysis_jobs (status, lease_until);

CREATE TABLE analysis_reports (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	job_id VARCHAR NOT NULL,
	creator_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	as_of VARCHAR NOT NULL,
	recorded_cutoff VARCHAR NOT NULL,
	rule_version VARCHAR NOT NULL,
	request JSON NOT NULL,
	scope JSON NOT NULL,
	summary JSON NOT NULL,
	source_hash VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (job_id)
);

CREATE TABLE analysis_rows (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	report_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	category VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	supplier_id VARCHAR,
	connection_id VARCHAR,
	metric VARCHAR,
	ordinal INTEGER NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX analysis_rows_metric ON analysis_rows (report_id, category, metric, ordinal);

CREATE INDEX analysis_rows_object ON analysis_rows (report_id, object_id);

CREATE INDEX analysis_rows_page ON analysis_rows (report_id, category, ordinal);

CREATE TABLE analysis_tool_audit (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	tool VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	request_hash VARCHAR NOT NULL,
	result_ref VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE approval_requests (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	kind VARCHAR,
	original_id VARCHAR,
	delta JSON,
	expected_version INTEGER,
	reason VARCHAR,
	requester_id VARCHAR,
	reviewer_id VARCHAR,
	review_note VARCHAR,
	status VARCHAR,
	document_id VARCHAR,
	created_at VARCHAR,
	reviewed_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE balances (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	g INTEGER NOT NULL,
	q INTEGER NOT NULL,
	d INTEGER NOT NULL,
	r INTEGER NOT NULL,
	t INTEGER NOT NULL,
	h INTEGER NOT NULL,
	b INTEGER NOT NULL,
	offline INTEGER NOT NULL,
	online INTEGER NOT NULL,
	pending INTEGER NOT NULL,
	withdrawing INTEGER NOT NULL,
	version INTEGER NOT NULL,
	updated_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, owner_id, warehouse_id, sku_id),
	CHECK (g >= 0),
	CHECK (q >= 0),
	CHECK (d >= 0),
	CHECK (r >= 0),
	CHECK (t >= 0),
	CHECK (h >= 0),
	CHECK (b >= 0),
	CHECK (offline >= 0),
	CHECK (online >= 0),
	CHECK (pending >= 0),
	CHECK (withdrawing >= 0)
);

CREATE TABLE business_events (
	id VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	event_type VARCHAR NOT NULL,
	lifecycle VARCHAR NOT NULL,
	object_type VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	object_line_id VARCHAR,
	process_id VARCHAR,
	batch_id VARCHAR,
	task_id VARCHAR,
	causal_event_id VARCHAR,
	correction_of VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	supplier_id VARCHAR,
	resource_group VARCHAR,
	quantity INTEGER,
	unit VARCHAR,
	planned_at VARCHAR,
	occurred_at VARCHAR,
	received_at VARCHAR NOT NULL,
	registered_at VARCHAR NOT NULL,
	source_timezone VARCHAR NOT NULL,
	source_system VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	source_event_id VARCHAR,
	source_version VARCHAR,
	time_quality VARCHAR NOT NULL,
	missing_fields JSON NOT NULL,
	quality_flags JSON NOT NULL,
	reason_code VARCHAR,
	evidence_refs JSON NOT NULL,
	dependencies JSON NOT NULL,
	rule_version VARCHAR,
	calendar_version VARCHAR,
	schema_version INTEGER NOT NULL,
	actor_id VARCHAR NOT NULL,
	request_id VARCHAR,
	business_reference VARCHAR,
	payload JSON NOT NULL,
	content_hash VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (run_id, tenant_id, source_system, source_account, source_event_id),
	CHECK (quantity IS NULL OR quantity >= 0),
	CHECK (time_quality IN ('unknown','reported','observed','verified')),
	CHECK (lifecycle IN ('registered','queued','started','paused','resumed','completed','cancelled','corrected'))
);

CREATE TABLE catalog_assortments (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	active INTEGER NOT NULL,
	replenishment INTEGER NOT NULL,
	inventory_known INTEGER NOT NULL,
	known_source VARCHAR,
	evidence_id VARCHAR,
	confirmed_at VARCHAR,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, warehouse_id, sku_id)
);

CREATE TABLE catalog_external_mappings (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_account VARCHAR NOT NULL,
	source_code VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_account, source_code)
);

CREATE TABLE catalog_sku_settings (
	sku_id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (sku_id)
);

CREATE TABLE catalog_supplier_skus (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	supplier_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, supplier_id, sku_id)
);

CREATE TABLE catalog_suppliers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	default_lead_days INTEGER NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code),
	CHECK (default_lead_days >= 0 AND default_lead_days <= 3650)
);

CREATE TABLE catalog_warehouse_settings (
	warehouse_id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	purpose VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (warehouse_id)
);

CREATE TABLE channel_grants (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	authorized INTEGER NOT NULL,
	free INTEGER NOT NULL,
	withdrawing INTEGER NOT NULL,
	withdrawn INTEGER NOT NULL,
	restore_on_cancel INTEGER NOT NULL,
	version INTEGER NOT NULL,
	fingerprint VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account, reference),
	CHECK (authorized > 0 AND free >= 0 AND withdrawing >= 0 AND withdrawn >= 0),
	CHECK (free + withdrawing + withdrawn <= authorized)
);

CREATE TABLE commitment_transitions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	grant_id VARCHAR NOT NULL,
	reservation_id VARCHAR,
	quantity INTEGER NOT NULL,
	document_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	response JSON NOT NULL,
	actor_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account, action_id),
	CHECK (quantity > 0)
);

CREATE TABLE connector_accounts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	warehouse_ids JSON NOT NULL,
	member_ids JSON NOT NULL,
	adapter VARCHAR NOT NULL,
	supports_query INTEGER NOT NULL,
	supports_idempotency INTEGER NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account)
);

CREATE TABLE connector_conflicts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	inbox_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	envelope JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (inbox_id, fingerprint)
);

CREATE TABLE connector_delivery_attempts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	outbox_id VARCHAR NOT NULL,
	token VARCHAR,
	action VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE connector_inbox (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	source_event_id VARCHAR NOT NULL,
	business_action_id VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	source_version INTEGER,
	kind VARCHAR NOT NULL,
	occurred_at VARCHAR,
	received_at VARCHAR NOT NULL,
	payload JSON NOT NULL,
	fingerprint VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	result JSON NOT NULL,
	error VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (run_id, tenant_id, connection_id, source_event_id)
);

CREATE TABLE connector_inbox_actions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	business_action_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	inbox_id VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, business_action_id)
);

CREATE TABLE connector_outbox (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	resource_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	sequence INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	retry_base INTEGER NOT NULL,
	next_at FLOAT NOT NULL,
	lease_until FLOAT,
	lease_token VARCHAR,
	lease_mode VARCHAR,
	last_error VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, resource_id, sequence)
);

CREATE TABLE connector_snapshots (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	snapshot_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	covered_watermark INTEGER,
	snapshot_at VARCHAR,
	quantities JSON NOT NULL,
	fingerprint VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, snapshot_id)
);

CREATE TABLE connector_source_states (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	entity_id VARCHAR NOT NULL,
	source_version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, entity_id)
);

CREATE TABLE connector_stock_deltas (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	sequence INTEGER NOT NULL,
	delta JSON NOT NULL,
	inbox_id VARCHAR NOT NULL,
	occurred_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, warehouse_id, sku_id, sequence)
);

CREATE TABLE demand_days (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	day VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	quality VARCHAR NOT NULL,
	source_reference VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, warehouse_id, sku_id, day),
	CHECK (quantity >= 0)
);

CREATE TABLE documents (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	kind VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	quantity INTEGER,
	source VARCHAR,
	note VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	status VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE exception_reminders (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	due_at VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id, due_at)
);

CREATE TABLE exception_task_history (
	id VARCHAR NOT NULL,
	task_id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	action VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (task_id, version)
);

CREATE TABLE exception_tasks (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	object_type VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	rule VARCHAR NOT NULL,
	rule_version VARCHAR NOT NULL,
	instance VARCHAR NOT NULL,
	type VARCHAR NOT NULL,
	priority VARCHAR NOT NULL,
	impact_quantity INTEGER NOT NULL,
	due_at VARCHAR,
	details JSON NOT NULL,
	requires_review INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	previous_status VARCHAR,
	version INTEGER NOT NULL,
	owner_id VARCHAR,
	creator_id VARCHAR NOT NULL,
	resolver_id VARCHAR,
	reviewer_id VARCHAR,
	result VARCHAR,
	responsibility_confirmed INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, object_type, object_id, rule, instance)
);

CREATE TABLE fulfillment_batches (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	line_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	commitment_id VARCHAR,
	quantity INTEGER NOT NULL,
	reserved INTEGER NOT NULL,
	shipped INTEGER NOT NULL,
	cancelled INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	CHECK (quantity > 0 AND reserved >= 0 AND shipped >= 0 AND cancelled >= 0),
	CHECK (reserved + shipped + cancelled = quantity)
);

CREATE TABLE fulfillment_tasks (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	batch_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	actor_id VARCHAR,
	queued_at VARCHAR NOT NULL,
	started_at VARCHAR,
	completed_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (batch_id, kind)
);

CREATE TABLE idempotency (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	actor_id VARCHAR,
	key VARCHAR,
	fingerprint VARCHAR,
	response JSON,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, actor_id, key)
);

CREATE TABLE import_batches (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR,
	kind VARCHAR,
	digest VARCHAR,
	rows JSON,
	errors JSON,
	status VARCHAR,
	created_at VARCHAR,
	applied_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, digest)
);

CREATE TABLE ledger (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	owner_id VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	document_id VARCHAR,
	kind VARCHAR,
	delta JSON,
	before JSON,
	after JSON,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (document_id)
);

CREATE TABLE order_commitments (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	grant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	line_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	reserved INTEGER NOT NULL,
	cancelled INTEGER NOT NULL,
	fulfilled INTEGER NOT NULL,
	version INTEGER NOT NULL,
	fingerprint VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account, order_id, line_id),
	CHECK (quantity > 0 AND reserved >= 0 AND cancelled >= 0 AND fulfilled >= 0),
	CHECK (reserved + cancelled + fulfilled = quantity)
);

CREATE TABLE outbox (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	document_id VARCHAR,
	event_type VARCHAR,
	payload JSON,
	status VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (document_id)
);

CREATE TABLE purchase_lines (
	id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	ordered INTEGER NOT NULL,
	received INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (order_id, sku_id),
	CHECK (ordered > 0),
	CHECK (received >= 0 AND received <= ordered)
);

CREATE TABLE purchase_orders (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	code VARCHAR,
	supplier VARCHAR,
	note VARCHAR,
	status VARCHAR,
	version INTEGER,
	actor_id VARCHAR,
	created_at VARCHAR,
	close_reason VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE purchase_receipts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR,
	reference VARCHAR,
	document_ids JSON,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, order_id, reference)
);

CREATE TABLE purchase_terms (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	supplier_id VARCHAR NOT NULL,
	replacement_return_id VARCHAR,
	ordered_at VARCHAR,
	original_due VARCHAR,
	latest_due VARCHAR,
	fingerprint VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (order_id)
);

CREATE TABLE quality_action_history (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE quality_details (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	object_type VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	details JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, object_type, object_id)
);

CREATE TABLE quality_dispositions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	session_id VARCHAR,
	original_id VARCHAR,
	quantity INTEGER NOT NULL,
	body JSON NOT NULL,
	details JSON NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	requester_id VARCHAR NOT NULL,
	reviewer_id VARCHAR,
	document_id VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, reference)
);

CREATE TABLE quality_events (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	return_id VARCHAR,
	reference VARCHAR,
	good INTEGER,
	bad INTEGER,
	reason VARCHAR,
	document_id VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, reference)
);

CREATE TABLE quality_sessions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	return_id VARCHAR,
	reference VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	good INTEGER NOT NULL,
	bad INTEGER NOT NULL,
	restocked INTEGER NOT NULL,
	disposed INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	details JSON NOT NULL,
	actor_id VARCHAR NOT NULL,
	document_id VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, reference)
);

CREATE TABLE quality_vouchers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	response JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, reference)
);

CREATE TABLE receipt_vouchers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	document_id VARCHAR NOT NULL,
	response JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account, reference),
	UNIQUE (document_id)
);

CREATE TABLE replenishment_decisions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	plan_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	reason VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	order_id VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (plan_id),
	CHECK (quantity >= 0)
);

CREATE TABLE replenishment_plans (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	policy_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	creator_id VARCHAR NOT NULL,
	auth_version INTEGER NOT NULL,
	as_of VARCHAR NOT NULL,
	expires_at VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	suggested INTEGER,
	snapshot JSON NOT NULL,
	fingerprint VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE replenishment_policies (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	supplier_id VARCHAR NOT NULL,
	window_days INTEGER NOT NULL,
	coverage_days INTEGER NOT NULL,
	lead_days INTEGER NOT NULL,
	safety INTEGER NOT NULL,
	minimum INTEGER NOT NULL,
	pack INTEGER NOT NULL,
	max_daily INTEGER NOT NULL,
	includes_open_orders INTEGER NOT NULL,
	version INTEGER NOT NULL,
	active INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, warehouse_id, sku_id),
	CHECK (window_days > 0 AND coverage_days > 0 AND lead_days >= 0 AND safety >= 0 AND minimum >= 1 AND pack >= 1 AND max_daily >= 1)
);

CREATE TABLE return_orders (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	kind VARCHAR,
	reference VARCHAR,
	original_id VARCHAR,
	quantity INTEGER,
	good INTEGER,
	bad INTEGER,
	status VARCHAR,
	version INTEGER,
	reason VARCHAR,
	actor_id VARCHAR,
	document_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, reference),
	CHECK (quantity > 0 AND good >= 0 AND bad >= 0 AND good + bad <= quantity)
);

CREATE TABLE reversal_links (
	original_id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	reversal_id VARCHAR,
	approval_id VARCHAR,
	PRIMARY KEY (original_id),
	UNIQUE (reversal_id)
);

CREATE TABLE runtime_bindings (
	id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	run_id VARCHAR NOT NULL,
	runtime_mode VARCHAR NOT NULL,
	timezone VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE sales_actions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	response JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, action_id)
);

CREATE TABLE sales_history (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	event_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE sales_order_lines (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	source_line_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	reserved INTEGER NOT NULL,
	shipped INTEGER NOT NULL,
	cancelled INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (order_id, source_line_id),
	CHECK (quantity > 0 AND reserved >= 0 AND shipped >= 0 AND cancelled >= 0),
	CHECK (reserved + shipped + cancelled <= quantity)
);

CREATE TABLE sales_orders (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	source_order_id VARCHAR NOT NULL,
	warehouse_ids JSON NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	fingerprint VARCHAR NOT NULL,
	planned_at VARCHAR,
	occurred_at VARCHAR,
	channel_completed_at VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, source_order_id)
);

CREATE TABLE sales_shipments (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	batch_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	document_id VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	action_id VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	occurred_at VARCHAR,
	delivered_at VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, order_id, reference),
	CHECK (quantity > 0),
	UNIQUE (document_id)
);

CREATE TABLE sales_supply_links (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	line_id VARCHAR NOT NULL,
	source_type VARCHAR NOT NULL,
	source_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	released INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	CHECK (quantity > 0 AND released >= 0 AND released <= quantity)
);

CREATE TABLE sessions (
	token_hash VARCHAR NOT NULL,
	user_id VARCHAR,
	csrf VARCHAR,
	expires INTEGER,
	PRIMARY KEY (token_hash)
);

CREATE TABLE shipment_vouchers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	source_account VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	document_id VARCHAR NOT NULL,
	response JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, source_account, reference),
	UNIQUE (document_id)
);

CREATE TABLE simulated_connector_effects (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	business_number VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, business_number)
);

CREATE TABLE simulated_source_events (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	source_event_id VARCHAR NOT NULL,
	envelope JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, connection_id, source_event_id)
);

CREATE TABLE skus (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	name VARCHAR,
	spec VARCHAR,
	unit VARCHAR,
	barcode VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE stock_count_history (
	id VARCHAR NOT NULL,
	count_id VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	action VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (count_id, version)
);

CREATE TABLE stock_count_lines (
	id VARCHAR NOT NULL,
	count_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	snapshot JSON,
	snapshot_version INTEGER,
	observed JSON,
	details JSON,
	document_id VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (count_id, warehouse_id, sku_id)
);

CREATE TABLE stock_count_locks (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	count_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, warehouse_id, sku_id)
);

CREATE TABLE stock_count_observations (
	id VARCHAR NOT NULL,
	count_id VARCHAR NOT NULL,
	line_id VARCHAR NOT NULL,
	round INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	observed JSON NOT NULL,
	details JSON NOT NULL,
	actor_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (line_id, round)
);

CREATE TABLE stock_counts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	creator_id VARCHAR NOT NULL,
	submitter_id VARCHAR,
	reviewer_id VARCHAR,
	reason VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE stock_reconciliations (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	connection_id VARCHAR NOT NULL,
	snapshot_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	rule_version VARCHAR NOT NULL,
	inputs JSON NOT NULL,
	difference JSON,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE supply_actions (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	operation VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	response JSON NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, operation, reference)
);

CREATE TABLE supply_batches (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	supply_line_id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	source_warehouse_id VARCHAR,
	sku_id VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	reserved INTEGER NOT NULL,
	sent INTEGER NOT NULL,
	received INTEGER NOT NULL,
	lost INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	carrier VARCHAR,
	transport_mode VARCHAR,
	tracking_number VARCHAR,
	planned_ship_at VARCHAR,
	expected_at VARCHAR,
	shipped_at VARCHAR,
	arrived_at VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, reference),
	CHECK (quantity > 0 AND reserved >= 0 AND sent >= 0 AND received >= 0 AND lost >= 0 AND reserved + sent <= quantity AND received + lost <= sent)
);

CREATE TABLE supply_discrepancies (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	batch_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	details JSON NOT NULL,
	status VARCHAR NOT NULL,
	version INTEGER NOT NULL,
	batch_version INTEGER NOT NULL,
	requester_id VARCHAR NOT NULL,
	reviewer_id VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, reference),
	CHECK (quantity > 0)
);

CREATE TABLE supply_history (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	object_type VARCHAR NOT NULL,
	object_id VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	payload JSON NOT NULL,
	event_id VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE supply_inspection_links (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	receipt_id VARCHAR NOT NULL,
	session_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (session_id),
	CHECK (quantity > 0)
);

CREATE TABLE supply_lines (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	source_line_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	source_warehouse_id VARCHAR,
	sku_id VARCHAR NOT NULL,
	quantity INTEGER NOT NULL,
	closed_quantity INTEGER NOT NULL,
	expected_at VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (kind, source_line_id),
	CHECK (quantity > 0 AND closed_quantity >= 0 AND closed_quantity <= quantity)
);

CREATE TABLE supply_receipts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	batch_id VARCHAR NOT NULL,
	supply_line_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	reference VARCHAR NOT NULL,
	document_id VARCHAR NOT NULL,
	g INTEGER NOT NULL,
	q INTEGER NOT NULL,
	d INTEGER NOT NULL,
	occurred_at VARCHAR,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (batch_id, reference),
	UNIQUE (document_id),
	CHECK (g >= 0 AND q >= 0 AND d >= 0 AND g + q + d > 0)
);

CREATE TABLE tenants (
	id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE transfer_claims (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	transfer_id VARCHAR,
	expected_version INTEGER,
	reason VARCHAR,
	requester_id VARCHAR,
	reviewer_id VARCHAR,
	note VARCHAR,
	status VARCHAR,
	created_at VARCHAR,
	reviewed_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE transfer_events (
	id VARCHAR NOT NULL,
	transfer_id VARCHAR NOT NULL,
	kind VARCHAR,
	reference VARCHAR,
	items JSON,
	document_ids JSON,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (transfer_id, kind, reference)
);

CREATE TABLE transfer_lines (
	id VARCHAR NOT NULL,
	transfer_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	quantity INTEGER,
	sent INTEGER,
	received INTEGER,
	lost INTEGER,
	PRIMARY KEY (id),
	UNIQUE (transfer_id, sku_id),
	CHECK (quantity > 0 AND sent >= 0 AND received >= 0 AND lost >= 0 AND sent <= quantity AND received + lost <= sent)
);

CREATE TABLE transfers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	warehouse_id VARCHAR,
	destination_id VARCHAR,
	status VARCHAR,
	version INTEGER,
	reason VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE user_settings (
	user_id VARCHAR NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (user_id)
);

CREATE TABLE users (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	username VARCHAR NOT NULL,
	name VARCHAR,
	password_hash VARCHAR,
	role VARCHAR,
	warehouse_ids JSON,
	PRIMARY KEY (id),
	UNIQUE (username)
);

CREATE TABLE warehouses (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	name VARCHAR,
	country VARCHAR,
	timezone VARCHAR,
	authority VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

-- Additional migration indexes.

CREATE INDEX IF NOT EXISTS m6_sales_order_lines_order ON sales_order_lines (order_id);

CREATE INDEX IF NOT EXISTS m6_fulfillment_batches_order ON fulfillment_batches (order_id);

CREATE INDEX IF NOT EXISTS m6_fulfillment_tasks_order ON fulfillment_tasks (order_id);

CREATE INDEX IF NOT EXISTS m6_sales_shipments_order ON sales_shipments (order_id);

CREATE INDEX IF NOT EXISTS m6_sales_supply_links_order ON sales_supply_links (order_id);

CREATE INDEX IF NOT EXISTS m6_sales_history_order ON sales_history (order_id);

CREATE INDEX IF NOT EXISTS m6_sales_order_page ON sales_orders (tenant_id,created_at,id);

CREATE INDEX IF NOT EXISTS m6_event_timeline ON business_events (tenant_id,registered_at,id);

CREATE INDEX IF NOT EXISTS analysis_jobs_retry_queue ON analysis_jobs (status,next_attempt_at,lease_until);
