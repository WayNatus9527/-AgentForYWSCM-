from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import pytest
from simulator.common import canonical, digest, file_digest, read_json, write_json
from simulator.config import profile, Scenario, Calendar, working_finish, SCENARIOS, FAULTS
from simulator.control import Control, BusyRun
from simulator.generator import generate, validate_dataset
from simulator.oracle import expected
from simulator.runner import run, replay, trace_records
from simulator.isolation import Worker

isolated=pytest.mark.skipif(platform.system()!='Darwin',reason='Managed process isolation currently requires macOS sandbox-exec')


@pytest.fixture
def control(tmp_path):
    return Control.initialize(str(tmp_path.resolve()/'control'),os.environ.get('M2_PG_URL_FILE'))


def backend(control):
    return 'postgresql' if control.identity['postgres'] else 'sqlite'


def small(name='normal',seed=91,**kwargs):
    return profile(name,sku_count=2,order_count=4,seed=seed,**kwargs)


@pytest.mark.parametrize('name',SCENARIOS)
def test_related_generation_reproduces_and_validates(name):
    a,truth=generate(small(name)); b,_=generate(small(name))
    assert digest(a)==digest(b) and validate_dataset(a)['valid']
    assert truth['input_sha256']==digest(a)
    assert digest(generate(small(name,seed=92))[0])!=digest(a)
    assert all(r['g']>=0 and r['q']>=0 for r in expected(a)['balances'])
    assert all('known_factors' not in event for event in a['events'])


@pytest.mark.parametrize('override',[
    {'sku_count':True},{'warehouse_count':1},{'tenant_count':1},{'sku_count':10,'order_count':2},
    {'start_time':'2026-10-01'},{'quality_units_per_hour':0},{'return_rate':1.1},
    {'calendar':{'weekdays':[]}},{'calendar':{'start_hour':18,'end_hour':9}},
    {'faults':['duplicate','duplicate']},{'scenario':'transport_exception'},
    {'generator_version':'unknown'},{'undeclared_option':True}])
def test_impossible_or_unversioned_config_rejected(override):
    with pytest.raises((ValueError,TypeError)): Scenario.model_validate(override)


def test_calendar_consumes_work_only_and_clock_rejects_nonfinite():
    start=datetime.fromisoformat('2026-10-16T17:00:00+08:00')
    assert working_finish(start,3,Calendar(),'Asia/Shanghai').isoformat()=='2026-10-19T11:00:00+08:00'
    from app.clock import VirtualBusinessClock
    clock=VirtualBusinessClock(start)
    for invalid in [float('nan'),float('inf'),-1,True]:
        with pytest.raises(ValueError): clock.advance(seconds=invalid)
    assert clock.now()=='2026-10-16T09:00:00+00:00'


def test_independent_hand_calculated_quantity_gold():
    events=[]
    def event(kind,values):
        key=str(len(events)); events.append(dict(id=key,kind=kind,tenant_id='A',warehouse_id='A1',sku_id='s',values=values)); return key
    purchase=event('purchase',{'quantity':20})
    event('purchase_receipt',dict(parent=purchase,g=20,q=0,d=0))
    sale=event('sale',{'quantity':5})
    ret=event('return',{'parent':sale,'quantity':2})
    event('return_quality',{'parent':ret,'good':1,'bad':1})
    transfer=event('transfer',{'quantity':10})
    event('transfer_reserve',{'parent':transfer}); event('transfer_ship',{'parent':transfer})
    event('transfer_receive',dict(parent=transfer,destination_id='A2',g=4,q=0,d=0))
    result=expected(dict(masters=[dict(tenant_id='A',warehouses=[{'id':'A1'}],skus=[{'id':'s','opening':100}])],events=events))
    assert [(r['warehouse_id'],r['g'],r['q'],r['d'],r['t']) for r in result['balances']]==[('A1',106,0,1,0),('A2',4,0,0,0)]
    assert result['in_transit']==6 and result['postings']==8
    source=Path(__file__).resolve().parents[1]/'simulator/oracle.py'
    assert 'from app' not in source.read_text() and 'import app' not in source.read_text()


def test_control_refuses_unknown_paths_seeds_tampering_and_cleanup(control,tmp_path):
    created=control.create(small(),backend=backend(control)); rid=created['run_id']
    with pytest.raises(ValueError): control.create(small(purpose='validation'),backend=backend(control))
    with pytest.raises(ValueError): control.destroy('../outside',created['fingerprint'])
    with pytest.raises(ValueError): control.destroy('unregistered',created['fingerprint'])
    with pytest.raises(ValueError): control.destroy(rid,'0'*64)
    with control.lock(rid):
        with pytest.raises(BusyRun): control.reset(rid,created['fingerprint'])
    path,manifest=control.verify(rid)
    victim=tmp_path/'keep.txt'; victim.write_text('unrelated')
    (path/'business/attachments/link').symlink_to(victim)
    with pytest.raises(ValueError): control.destroy(rid,created['fingerprint'])
    assert victim.read_text()=='unrelated'
    (path/'business/attachments/link').unlink()
    inputs=path/'inputs.json'; inputs.chmod(0o600); inputs.write_text('{}')
    with pytest.raises(ValueError): control.verify(rid)
    assert manifest['config']['purpose']=='development'


@isolated
def test_all_faults_actual_crashes_and_captured_replay(control):
    item=control.create(small('mixed'),backend=backend(control))
    first=run(control,item['run_id'])
    assert first['status']=='passed',first
    path,_=control.verify(item['run_id'])
    rows=trace_records(path/'trace.jsonl')
    assert set(r['fault'] for r in rows if r['phase']=='intent' and r['fault'])==set(FAULTS)
    assert sum(r['result']['status']=='lost' for r in rows if r['phase']=='outcome')==3
    assert any(r['result']['status']=='blocked' for r in rows if r['phase']=='outcome')
    second=replay(control,item['run_id'])
    assert second['status']=='passed'
    assert first['business']['actual_sha256']==second['business']['actual_sha256']
    assert first['input_sha256']==second['input_sha256']
    assert first['isolation']['runtime_fingerprint']!=second['isolation']['runtime_fingerprint']


@isolated
def test_parallel_identical_ids_cross_access_reset_cleanup_leave_b_unchanged(control):
    a=control.create(small(seed=101),backend=backend(control))
    b=control.create(small(seed=102),backend=backend(control))
    with ThreadPoolExecutor(2) as pool:
        reports=list(pool.map(lambda x:run(control,x['run_id']),[a,b]))
    assert all(r['status']=='passed' for r in reports)
    ap,am=control.verify(a['run_id']); bp,bm=control.verify(b['run_id'])
    before=read_json(bp/'reports/snapshot.json')
    sentinel=bp/'business/attachments/sentinel'; sentinel.write_text('B-owned')
    with control.lock(a['run_id']) as fd,Worker(ap,am,fd) as worker:
        assert worker.call('probe',path=str(bp/'business/delivery.sqlite'))=={'read':'denied','write':'denied'}
        assert worker.call('probe',path=str(sentinel))=={'read':'denied','write':'denied'}
        assert worker.call('probe',path=str(ap/'private/truth.json'))=={'read':'denied','write':'denied'}
        raw={'operation':'snapshot','run_id':b['run_id'],'fingerprint':bm['database_fingerprint']}
        worker.process.stdin.write((canonical(raw)+'\n').encode());worker.process.stdin.flush()
        assert worker.receive()['error']=='RUNTIME_MISMATCH'
        if backend(control)=='postgresql':
            runtime=read_json(bp/'business/runtime.json')
            assert worker.call('probe',schema=runtime['schema'],expected_role=runtime['role'])==dict(read='denied',write='denied',role_switch='denied')
        with pytest.raises(BusyRun):control.destroy(a['run_id'],a['fingerprint'])
    successor=control.reset(a['run_id'],a['fingerprint'])
    assert successor['run_id']!=a['run_id'] and run(control,successor['run_id'])['status']=='passed'
    assert control.destroy(a['run_id'],a['fingerprint'])['status']=='destroyed'
    assert not ap.exists() and sentinel.read_text()=='B-owned'
    with control.lock(b['run_id']) as fd,Worker(bp,bm,fd) as worker:
        after=worker.call('snapshot',permissions=False)
    assert {k:v for k,v in before.items() if k!='permission_checks'}=={k:v for k,v in after.items() if k!='permission_checks'}


@isolated
def test_fast_forward_pauses_between_events_and_resumes(control):
    item=control.create(small(),backend=backend(control))
    paused=run(control,item['run_id'],until='2026-10-02T09:00:00+08:00')
    assert paused['status']=='paused' and 0<paused['progress']['completed_events']<paused['progress']['total_events']
    result=run(control,item['run_id'])
    assert result['status']=='passed'
    path,_=control.verify(item['run_id'])
    instants=[datetime.fromisoformat(row['at']) for row in trace_records(path/'trace.jsonl') if row['phase']=='intent']
    assert instants==sorted(instants)


@isolated
def test_controller_abrupt_exit_recovers_committed_action(control):
    item=control.create(small(),backend=backend(control)); rid=item['run_id']
    code='''
import os,sys
from simulator.control import Control
from simulator.isolation import Worker
from simulator.runner import run
original=Worker.call
def interrupted(self,operation,**kwargs):
    result=original(self,operation,**kwargs)
    if operation=='deliver' and result.get('status')=='ok': os._exit(91)
    return result
Worker.call=interrupted
run(Control(sys.argv[1]),sys.argv[2])
'''
    process=subprocess.run([sys.executable,'-c',code,str(control.root),rid],cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=40)
    assert process.returncode==91,process.stderr.decode()
    # The child holds the lifecycle lock until EOF shuts it down.
    for attempt in range(100):
        try:
            with control.lock(rid): pass
            break
        except BusyRun:time.sleep(.03)
    result=run(control,rid)
    assert result['status']=='passed'
    path,_=control.verify(rid)
    rows=trace_records(path/'trace.jsonl')
    assert len([x for x in rows if x['phase']=='intent'])==len([x for x in rows if x['phase']=='outcome'])+1


@isolated
def test_changed_source_id_and_mutated_trace_rejected(control):
    item=control.create(small(),backend=backend(control)); rid=item['run_id']
    assert run(control,rid)['status']=='passed'
    path,manifest=control.verify(rid)
    event=read_json(path/'inputs.json')['events'][0]
    altered=event|{'values':event['values']|{'quantity':999}}
    with Worker(path,manifest) as worker:
        assert worker.call('deliver',event=altered)['error']=='SOURCE_EVENT_CONFLICT'
    trace=path/'trace.jsonl'; trace.write_text(trace.read_text().replace('EV-0000001','EV-9999999',1))
    with pytest.raises(ValueError): run(control,rid)


def test_separate_management_auth_csrf_and_business_login(control):
    from fastapi.testclient import TestClient
    from simulator.web import create_app
    with TestClient(create_app(control)) as client:
        assert client.get('/api/runs').status_code==401
        assert client.post('/api/session',json={'token':'business-password'}).status_code==401
        token=read_json(control.root/'admin-token.json')['token']
        login=client.post('/api/session',json={'token':token})
        assert login.status_code==200
        body={'scenario':'normal','purpose':'development','seed':471}
        assert client.post('/api/runs',json=body).status_code==403
        client.headers['X-CSRF-Token']=login.json()['csrf']
        assert client.post('/api/runs',json=body,headers={'Origin':'https://foreign.invalid'}).status_code==403
        created=client.post('/api/runs',json=body)
        assert created.status_code==200
        rid=created.json()['run_id']
        assert client.post('/api/runs/'+rid+'/destroy',json={}).status_code==409
        assert client.get('/api/runs/'+rid+'/export').status_code==200
        assert token not in client.get('/api/runs').text
        assert client.get('/private/truth.json').status_code==404
        assert client.delete('/api/session').status_code==200
        assert client.get('/api/runs').status_code==401


@isolated
def test_ten_fresh_independent_parameter_sets(control):
    from simulator.batch import verify_batch
    result=verify_batch(control,count=10,backend=backend(control),seed_start=820010)
    assert result['passed']==10 and result['failed']==0,result
    assert len({x['input_sha256'] for x in result['cases']})==10
    assert set(result['scenarios'])==set(SCENARIOS)
    assert set(result['fault_coverage'])==set(FAULTS)


@isolated
def test_development_workbench_runs_in_same_restricted_process(control):
    import socket
    import httpx
    item=control.create(small(),backend=backend(control)); rid=item['run_id']
    assert run(control,rid)['status']=='passed'
    path,manifest=control.verify(rid)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
    with control.lock(rid) as fd,Worker(path,manifest,fd,serve_port=port):
        with httpx.Client(base_url=f'http://127.0.0.1:{port}') as client:
            for _ in range(100):
                try:
                    health=client.get('/api/v1/health')
                    break
                except httpx.ConnectError: time.sleep(.03)
            response=client.post('/api/v1/session',json=dict(username='tenant-0-admin',password=read_json(path/'business/login.json')['password']))
            assert response.status_code==200
            assert client.get('/api/v2/runtime').json()['run_id']==rid
            assert all(r['tenant_id']=='tenant-0' for r in client.get('/api/v1/inventory').json())
            assert client.post('/api/simulation/reset',json={}).status_code==404
            assert client.get('/private/truth.json').status_code==404
        with pytest.raises(BusyRun): control.reset(rid,item['fingerprint'])


@isolated
def test_postgresql_privilege_drift_fails_closed(control):
    if backend(control)!='postgresql': pytest.skip('Restricted PostgreSQL role acceptance')
    from psycopg import sql
    item=control.create(small(),backend='postgresql')
    path,_=control.verify(item['run_id']); role=read_json(path/'business/runtime.json')['role']
    with control.pg().admin() as c: c.execute(sql.SQL('ALTER ROLE {} CREATEDB').format(sql.Identifier(role)))
    try:
        with pytest.raises(ValueError,match='privileges'): control.verify(item['run_id'])
    finally:
        with control.pg().admin() as c: c.execute(sql.SQL('ALTER ROLE {} NOCREATEDB').format(sql.Identifier(role)))
