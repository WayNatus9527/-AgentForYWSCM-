"""Additive migration 9: durable analysis and immutable evidence."""
from sqlalchemy import Table,Column as C,String as S,Integer as I,Float,JSON,UniqueConstraint as U,Index
from . import db

def table(name,*columns):
    return Table(name,db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),*columns)

jobs=table('analysis_jobs',C('creator_id',S,nullable=False),C('warehouse_id',S,nullable=False),C('sku_id',S),
    C('request',JSON,nullable=False),C('status',S,nullable=False),C('lease_token',S),C('lease_until',Float),
    C('attempts',I,nullable=False),C('attempt_limit',I,nullable=False,server_default='3'),
    C('next_attempt_at',Float),C('started_at',Float),C('finished_at',Float),
    C('report_id',S),C('error',S),C('created_at',S,nullable=False))
reports=table('analysis_reports',C('job_id',S,nullable=False),C('creator_id',S,nullable=False),C('warehouse_id',S,nullable=False),C('sku_id',S),
    C('as_of',S,nullable=False),C('recorded_cutoff',S,nullable=False),C('rule_version',S,nullable=False),C('request',JSON,nullable=False),
    C('scope',JSON,nullable=False),C('summary',JSON,nullable=False),C('source_hash',S,nullable=False),C('created_at',S,nullable=False),U('job_id'))
rows=table('analysis_rows',C('report_id',S,nullable=False),C('warehouse_id',S,nullable=False),C('category',S,nullable=False),C('object_id',S,nullable=False),
    C('sku_id',S),C('supplier_id',S),C('connection_id',S),C('metric',S),C('ordinal',I,nullable=False),C('payload',JSON,nullable=False))
Index('analysis_rows_page',rows.c.report_id,rows.c.category,rows.c.ordinal)
Index('analysis_rows_object',rows.c.report_id,rows.c.object_id)
Index('analysis_rows_metric',rows.c.report_id,rows.c.category,rows.c.metric,rows.c.ordinal)
Index('analysis_jobs_queue',jobs.c.status,jobs.c.lease_until)
improvements=table('analysis_improvements',C('report_id',S,nullable=False),C('row_id',S,nullable=False),C('task_id',S,nullable=False),
    C('actor_id',S,nullable=False),C('created_at',S,nullable=False),U('report_id','row_id'))
audit=table('analysis_tool_audit',C('actor_id',S,nullable=False),C('tool',S,nullable=False),C('object_id',S,nullable=False),
    C('request_hash',S,nullable=False),C('result_ref',S),C('created_at',S,nullable=False))
TABLES=(jobs,reports,rows,improvements,audit)
APPEND_ONLY=tuple(t.name for t in (reports,rows,improvements,audit))

def performance_indexes(c):
    # Additive indexes on previous modules; no previous migration is rewritten.
    from . import m4_schema as sales
    for table in (sales.lines,sales.batches,sales.tasks,sales.shipments,sales.links,sales.history):
        name='m6_'+table.name+'_order'
        c.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS {name} ON {table.name} (order_id)')
    c.exec_driver_sql('CREATE INDEX IF NOT EXISTS m6_sales_order_page ON sales_orders (tenant_id,created_at,id)')
    c.exec_driver_sql('CREATE INDEX IF NOT EXISTS m6_event_timeline ON business_events (tenant_id,registered_at,id)')
