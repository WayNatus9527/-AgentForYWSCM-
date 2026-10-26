"""Recovery, poison jobs, current authorization and lifecycle integration."""
import json,time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,insert,update,func,event
from sqlalchemy.exc import DBAPIError
from app import db,review,review_sources,m6_schema as s,review_queue_schema as q
from app.review_worker import Supervisor
from app.main import create_app
from app.clock import VirtualBusinessClock,business_now
from app.domain import BusinessError
from test_inventory import env,actor,counts
from test_m3_workflows import req
from test_phase2 import login


def key():return str(uuid4())
def body(e,sku='sku-1'):
    e.business_clock=VirtualBusinessClock('2026-10-02T00:00:00Z')
    return dict(warehouse_id='wh-cn',sku_id=sku,start_at='2026-10-01T00:00:00Z',end_at=business_now(e))
def create(e,sku='sku-1',username='admin'):return review.create(e,actor(e,username),body(e,sku),key())
def job(e,identity):
    with e.connect() as c:return dict(c.execute(select(s.jobs).where(s.jobs.c.id==identity)).mappings().one())
def timeline(e,identity):
    with e.connect() as c:return [dict(x) for x in c.execute(select(q.history).where(q.history.c.job_id==identity).order_by(q.history.c.recorded_at,q.history.c.id)).mappings()]
def until(fn):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.01)
    raise AssertionError('Worker did not reach expected state')


def test_backoff_exhaustion_safe_history_manual_retry_and_replay(env,monkeypatch,caplog):
    e,c=env;row=create(e);before=counts(e);original=review_sources.capture
    def fail(*_):raise RuntimeError('password=SHOULD_NOT_LEAK')
    monkeypatch.setattr(review_sources,'capture',fail)
    for attempt in range(1,4):
        assert review.dispatch(e,row['id']) is None
        current=job(e,row['id']);assert current['attempts']==attempt
        assert current['status']==('failed' if attempt==3 else 'queued')
        if attempt<3:
            assert current['next_attempt_at']>time.time()
            assert review.process(e,row['id']) is None and job(e,row['id'])['attempts']==attempt
            with db.write(e) as conn:conn.execute(update(s.jobs).where(s.jobs.c.id==row['id']).values(next_attempt_at=0))
    assert counts(e)['ledger']==before['ledger']
    result=c.get('/api/v2/reviews/jobs/'+row['id']).json()
    assert result['error']=='ANALYSIS_TEMPORARY_ERROR' and 'lease_token' not in result
    history=c.get(f"/api/v2/reviews/jobs/{row['id']}/history").json()
    assert 'SHOULD_NOT_LEAK' not in json.dumps(history)+json.dumps(result)+caplog.text
    assert [x['event'] for x in history['items']]==['queued','started','retry_scheduled','started','retry_scheduled','started','failed']
    monkeypatch.setattr(review_sources,'capture',original)
    payload=dict(expected_attempts=3,reason='核实临时依赖已恢复后重试');request_key=key()
    req(c,f"reviews/jobs/{row['id']}/retry",payload,key=request_key,ok=202)
    req(c,f"reviews/jobs/{row['id']}/retry",payload,key=request_key,ok=202)
    assert job(e,row['id'])['status']=='completed' and job(e,row['id'])['attempts']==4
    assert req(c,f"reviews/jobs/{row['id']}/retry",payload,ok=409)['error']['code']=='JOB_VERSION_CONFLICT'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.reports).where(s.reports.c.job_id==row['id']))==1
    assert sum(x['event']=='retry_requested' for x in timeline(e,row['id']))==1
    with pytest.raises(DBAPIError),e.begin() as conn:conn.execute(update(q.history).where(q.history.c.job_id==row['id']).values(reason='rewritten'))


def test_poison_job_does_not_block_next_job_or_keep_retrying(env,monkeypatch):
    e,_=env;bad=create(e);good=create(e,'sku-2');original=review_sources.capture
    def capture(conn,user,request):
        if request['sku_id']=='sku-1':raise RuntimeError('poison')
        return original(conn,user,request)
    monkeypatch.setattr(review_sources,'capture',capture);monkeypatch.setattr(review,'RETRY_DELAYS',(.02,.02,.02))
    worker=Supervisor(e,interval=.01).start()
    try:
        until(lambda:job(e,bad['id'])['status']=='failed' and job(e,good['id'])['status']=='completed')
        assert job(e,bad['id'])['attempts']==3 and job(e,good['id'])['attempts']==1
        assert worker.status()['last_error'] is None
    finally:worker.stop()
    assert not worker.status()['alive']


def test_authorization_failure_is_terminal_and_retry_rechecks_creator(env):
    e,c=env;row=create(e,username='warehouse');user=actor(e,'warehouse')
    with db.write(e) as conn:conn.execute(update(db.users).where(db.users.c.id==user['id']).values(role='cashier'))
    assert review.process(e,row['id']) is None
    assert job(e,row['id'])['status']=='failed' and job(e,row['id'])['next_attempt_at'] is None
    assert req(c,f"reviews/jobs/{row['id']}/retry",dict(expected_attempts=1,reason='不能越过原创建人的权限'),ok=403)['error']['code']=='FORBIDDEN'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.reports))==0


def test_queue_list_history_retry_are_owner_scoped_and_csrf_guarded(env):
    e,c=env;admin=create(e);other=create(e,username='warehouse');login(c,'warehouse')
    result=c.get('/api/v2/reviews/jobs').json()
    assert [x['id'] for x in result['items']]==[other['id']]
    for path in (f"jobs/{admin['id']}",f"jobs/{admin['id']}/history",'worker-status'):
        assert c.get('/api/v2/reviews/'+path).status_code in (403,404)
    assert req(c,f"reviews/jobs/{admin['id']}/retry",dict(expected_attempts=0,reason='不能重试其他创建者任务'),ok=404)
    headers=dict(c.headers);c.headers.pop('X-CSRF-Token')
    assert c.post(f"/api/v2/reviews/jobs/{other['id']}/retry",json=dict(expected_attempts=0,reason='必须带当前会话凭据'),headers={'Idempotency-Key':key()}).status_code==403
    c.headers.update(headers);login(c,'cashier')
    assert c.get('/api/v2/reviews/jobs').status_code==403


def test_expired_crash_before_commit_is_recovered_on_api_startup(env):
    e,c=env;row=create(e)
    class PowerLoss(BaseException):pass
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO analysis_rows'):raise PowerLoss()
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(PowerLoss):review.process(e,row['id'])
    finally:event.remove(e,'before_cursor_execute',crash)
    assert job(e,row['id'])['status']=='running'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.reports))==0
    with db.write(e) as conn:conn.execute(update(s.jobs).where(s.jobs.c.id==row['id']).values(lease_until=0))
    app=create_app(e,review_worker_enabled=True,review_poll_seconds=.02)
    with TestClient(app) as restarted:
        until(lambda:job(e,row['id'])['status']=='completed');login(restarted,'admin')
        status=restarted.get('/api/v2/reviews/worker-status').json()
        assert status['enabled'] and status['alive'] and 'reports_observed' not in status
        assert status['last_finished_at'] is not None
    assert not app.state.review_supervisor.status()['alive']
    assert [x['event'] for x in timeline(e,row['id'])]==['queued','started','lease_expired','started','completed']
    assert job(e,row['id'])['attempts']==2


def test_active_lease_and_future_retry_are_not_claimed_and_tenants_are_fair(env):
    e,_=env;one=create(e);two=create(e,'sku-2');three=create(e)
    with db.write(e) as c:
        c.execute(update(s.jobs).where(s.jobs.c.id.in_([one['id'],two['id'],three['id']])).values(created_at='2020-01-01T00:00:00Z'))
        foreign=dict(one,id='other-tenant-job',tenant_id='other',created_at='2021-01-01T00:00:00Z')
        foreign={k:v for k,v in foreign.items() if k in s.jobs.c};c.execute(insert(s.jobs).values(**foreign))
    selected=review.pending(e,2)
    assert 'other-tenant-job' in selected and len(selected)==2
    with db.write(e) as c:
        c.execute(update(s.jobs).where(s.jobs.c.id==one['id']).values(status='running',lease_token='owned',lease_until=time.time()+60))
        c.execute(update(s.jobs).where(s.jobs.c.id==two['id']).values(next_attempt_at=time.time()+60))
    selected=review.pending(e,20)
    assert one['id'] not in selected and two['id'] not in selected
    assert review.process(e,one['id']) is None and job(e,one['id'])['lease_token']=='owned'


def test_concurrent_manual_retry_enqueues_once(env,monkeypatch):
    e,_=env;row=create(e);a=actor(e)
    with db.write(e) as c:c.execute(update(s.jobs).where(s.jobs.c.id==row['id']).values(status='failed',error='TEST_ERROR'))
    gate=Barrier(2);payload=dict(expected_attempts=0,reason='核实后并发请求重试')
    def retry(_):
        gate.wait()
        try:return review.retry(e,a,row['id'],payload,key())['status']
        except BusinessError as exc:return exc.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(retry,range(2)))
    assert sorted(results)==['JOB_VERSION_CONFLICT','queued']
    assert sum(x['event']=='retry_requested' for x in timeline(e,row['id']))==1


def test_queue_database_error_is_sanitized_and_later_cycle_recovers(env,monkeypatch,caplog):
    e,_=env;row=create(e);original=review.pending
    def fail(*_):raise RuntimeError('postgresql://secret-password')
    worker=Supervisor(e);monkeypatch.setattr(review,'pending',fail)
    assert worker.cycle()==[] and worker.status()['last_error']=='QUEUE_UNAVAILABLE'
    assert 'secret-password' not in caplog.text
    monkeypatch.setattr(review,'pending',original)
    assert worker.cycle()[0] and worker.status()['last_error'] is None
    assert job(e,row['id'])['status']=='completed'


def test_repeated_process_crashes_exhaust_limit_and_manual_retry_respects_backlog(env):
    e,c=env;row=create(e)
    with db.write(e) as conn:
        conn.execute(update(s.jobs).where(s.jobs.c.id==row['id']).values(status='running',attempts=3,lease_until=0,lease_token='lost'))
    assert review.process(e,row['id']) is None
    assert job(e,row['id'])['status']=='failed' and job(e,row['id'])['error']=='RETRY_EXHAUSTED'
    for _ in range(3):create(e)
    assert req(c,f"reviews/jobs/{row['id']}/retry",dict(expected_attempts=3,reason='队列满时不能绕过限制'),ok=429)['error']['code']=='ANALYSIS_BUSY'
    assert job(e,row['id'])['attempt_limit']==3


def test_worker_cli_requires_explicit_target_before_opening_default_database(monkeypatch):
    from app import review_worker
    monkeypatch.delenv('APP_DATA_DIR',raising=False)
    monkeypatch.setattr('sys.argv',['review_worker','--expected-run-id','chosen-runtime'])
    monkeypatch.setattr(db,'make_engine',lambda:pytest.fail('Must not open default database'))
    with pytest.raises(SystemExit) as result:review_worker.main()
    assert result.value.code==2
