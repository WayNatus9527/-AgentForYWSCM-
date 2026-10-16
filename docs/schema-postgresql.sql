-- PostgreSQL reference DDL, application version 0.5.0 / migration 5.
-- Runtime migrations also install append-only guards for business_events.
-- This is a table reference, not a substitute for app.migrations.migrate.

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

CREATE TABLE schema_migrations (
	version SERIAL NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (version)
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
