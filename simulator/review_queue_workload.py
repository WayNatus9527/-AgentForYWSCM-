"""Queue fault probes inside the same restricted business worker as other flows."""
import os,time
from datetime import timedelta
from sqlalchemy import select,func,event
from app import review,review_sources,m6_schema as s,review_queue_schema as q
from app.clock import business_now,parse_timestamp
from app.review_worker import Supervisor


def fixture(driver,master):
    engine=driver.engine;actor=driver.actor(master['tenant_id']);end=business_now(engine)
    body=dict(warehouse_id=master['warehouses'][0]['id'],sku_id=master['skus'][0]['id'],
        start_at=(parse_timestamp(end)-timedelta(days=1)).isoformat(),end_at=end)
    failed=review.create(engine,actor,body,'queue-probe-failed')
    capture=review_sources.capture;delays=review.RETRY_DELAYS
    def fail(*args):raise RuntimeError('injected temporary analysis failure')
    try:
        review_sources.capture=fail;review.RETRY_DELAYS=(0,0,0)
        for _ in range(3):review.dispatch(engine,failed['id'])
    finally:review_sources.capture=capture;review.RETRY_DELAYS=delays
    queued=review.create(engine,actor,body,'queue-probe-recover')
    return dict(failed_job=failed['id'],queued_job=queued['id'])


def crash(driver,identity):
    review.LEASE_SECONDS=.1
    def terminate(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO analysis_rows'):os._exit(97)
    event.listen(driver.engine,'before_cursor_execute',terminate)
    review.process(driver.engine,identity)
    raise AssertionError('Expected process termination before report commit')


def recover(driver,master,identities):
    engine=driver.engine;actor=driver.actor(master['tenant_id']);supervisor=Supervisor(engine,interval=.02).start()
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with engine.connect() as c:job=review.get_job(c,actor,identities['queued_job'])
            if job['status']=='completed':break
            time.sleep(.02)
        assert job['status']=='completed'
        retry_body=dict(expected_attempts=3,reason='故障注入结束，恢复同一任务')
        review.retry(engine,actor,identities['failed_job'],retry_body,'queue-probe-manual-retry')
        review.retry(engine,actor,identities['failed_job'],retry_body,'queue-probe-manual-retry')
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with engine.connect() as c:retried=review.get_job(c,actor,identities['failed_job'])
            if retried['status']=='completed':break
            time.sleep(.02)
        assert retried['status']=='completed'
    finally:supervisor.stop()
    with engine.connect() as c:
        ids=list(identities.values())
        reports=c.scalar(select(func.count()).select_from(s.reports).where(s.reports.c.job_id.in_(ids)))
        history=[dict(x) for x in c.execute(select(q.history).where(q.history.c.job_id.in_(ids)).order_by(q.history.c.recorded_at)).mappings()]
    return dict(reports=reports,queued_attempts=job['attempts'],retried_attempts=retried['attempts'],history=history,stopped=not supervisor.status()['alive'])
