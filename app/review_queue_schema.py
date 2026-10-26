"""Migration 10: retry timing and append-only analysis job history."""
from sqlalchemy import Column as C,String as S,Integer as I,Float,Index,UniqueConstraint,inspect
from .m6_schema import table

history=table('analysis_job_history',C('job_id',S,nullable=False),C('actor_id',S),C('event',S,nullable=False),
    C('attempt',I,nullable=False),C('error',S),C('reason',S),C('recorded_at',Float,nullable=False),
    UniqueConstraint('job_id','attempt','event'))
Index('analysis_job_history_page',history.c.job_id,history.c.recorded_at,history.c.id)


def migrate(c):
    from .m3_common import append_only
    existing={x['name'] for x in inspect(c).get_columns('analysis_jobs')}
    for name,definition in [('attempt_limit','INTEGER NOT NULL DEFAULT 3'),('next_attempt_at','DOUBLE PRECISION'),
                            ('started_at','DOUBLE PRECISION'),('finished_at','DOUBLE PRECISION')]:
        if name not in existing:c.exec_driver_sql(f'ALTER TABLE analysis_jobs ADD COLUMN {name} {definition}')
    # Old attempts remain monotonic; permit exhausted legacy leases one bounded
    # recovery cycle, without replacing their report or historical state.
    c.exec_driver_sql('UPDATE analysis_jobs SET attempt_limit = attempts + 3 WHERE attempts >= attempt_limit AND status IN (\'queued\',\'running\')')
    history.create(c,checkfirst=True)
    append_only(c,(history.name,))
    c.exec_driver_sql('CREATE INDEX IF NOT EXISTS analysis_jobs_retry_queue ON analysis_jobs (status,next_attempt_at,lease_until)')
