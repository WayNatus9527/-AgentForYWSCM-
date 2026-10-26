"""Actual workflows and hand-computed time/dependency/security expectations."""
from datetime import timedelta
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select,insert,update,func,event
from sqlalchemy.exc import DBAPIError
from app import db,review,review_agent,review_metrics as metrics,events,inventory as inv,quality_control as qc,m6_schema as s,m6_models as m,accounts
from app.clock import VirtualBusinessClock,parse_timestamp,business_now
from app.domain import BusinessError
from test_inventory import env,actor,balance,counts
from test_m3_workflows import req,meta
from test_m4_workflows import order,act as sale_act
from test_m5_workflows import vendor,rule,calculate
from test_phase2 import login

def key():return str(uuid4())

def move(e,instant):e.business_clock.advance(seconds=(parse_timestamp(instant)-parse_timestamp(business_now(e))).total_seconds())

def start(e):
    e.business_clock=VirtualBusinessClock('2026-10-01T00:00:00Z');a=actor(e)
    with db.write(e,a['tenant_id']) as c:
        stock=balance(e)
        # Explicit fixture evidence for the demo opening, whose historic seed has
        # no event. Production adapters must keep such uncovered history unknown.
        events.record_event(c,a,event_type='inventory.posted',object_type='stock_document',object_id='opening-fixture',warehouse_id='wh-cn',sku_id='sku-1',
            payload=dict(balance_version=1,delta={'g':stock['g']}))
        inv.movement(c,a,stock,dict(h=stock['g']),'hold','fixture-hold','冻结已有现货，检验显式供给依赖',stock['version'])

def flow(e,c,*,queue_hours=48,late_entry=0,unknown=False,linked=True):
    start(e);a=actor(e);supplier=vendor(e)
    purchase=req(c,'purchase-plans',dict(code=key(),warehouse_id='wh-cn',supplier_id=supplier['id'],ordered_at=business_now(e),
        promised_at='2026-10-06T00:00:00Z',lines=[dict(sku_id='sku-1',quantity=10)]))
    sale=sale_act(c,order(e,c,quantity=10,planned_at='2026-10-10T00:00:00Z'),'confirm')
    if linked:sale=sale_act(c,sale,'link',line_id=sale['lines'][0]['id'],source_type='purchase_line',source_id=purchase['lines'][0]['source_line_id'],quantity=10)
    purchase=req(c,f"supply-orders/purchase/{purchase['id']}/batches",dict(expected_version=purchase['version'],reference=key(),reason='金标准运输批次',
        supply_line_id=purchase['lines'][0]['id'],quantity=10,expected_at='2026-10-06T00:00:00Z'))
    batch=purchase['lines'][0]['batches'][0]
    purchase=req(c,f"supply-batches/{batch['id']}/ship",dict(expected_version=batch['version'],reference=key(),reason='实际交承运',occurred_at=business_now(e)))
    move(e,'2026-10-06T00:00:00Z');arrival=business_now(e)
    e.business_clock.advance(seconds=late_entry*3600)
    batch=purchase['lines'][0]['batches'][0]
    purchase=req(c,f"supply-batches/{batch['id']}/receive",dict(expected_version=batch['version'],reference=key(),reason='实际到货凭证',
        occurred_at=None if unknown else arrival,expected_balance_version=balance(e)['version'],g=10 if late_entry else 0,q=0 if late_entry else 10))
    if not late_entry:
        e.business_clock.advance(seconds=queue_hours*3600);receipt=purchase['lines'][0]['batches'][0]['receipts'][0]
        inspection=req(c,f"supply-receipts/{receipt['id']}/inspect",dict(reference=key(),quantity=10,expected_balance_version=balance(e)['version'],details=meta()))
        e.business_clock.advance(seconds=3600)
        inspection=qc.act(e,a,inspection['id'],'complete',dict(expected_version=inspection['version'],good=10,bad=0,expected_balance_version=balance(e)['version'],details=meta()),key())
        qc.act(e,a,inspection['id'],'restock',dict(expected_version=inspection['version'],quantity=10,expected_balance_version=balance(e)['version']),key())
    return purchase,sale

def report(e,c,**kw):
    body=dict(warehouse_id='wh-cn',sku_id='sku-1',start_at='2026-10-01T00:00:00Z',end_at=business_now(e),min_samples=1);body.update(kw)
    job=req(c,'reviews/jobs',body,ok=202)
    job=c.get('/api/v2/reviews/jobs/'+job['id']).json();assert job['status']=='completed',job
    return c.get('/api/v2/reviews/reports/'+job['report_id']).json()

def rows(c,r,category,**kw):
    response=c.get(f"/api/v2/reviews/reports/{r['id']}/rows/{category}",params=dict(limit=100,**kw));assert response.status_code==200,response.text
    return response.json()['items']


def test_gold_quality_wait_beats_normal_long_transport(env):
    e,c=env;flow(e,c);r=report(e,c);cand={x['payload']['metric']:x for x in rows(c,r,'candidate')}
    assert cand['quality_wait']['payload']['classification']=='evidenced_constraint'
    assert cand['quality_wait']['payload']['impact_quantity']==10
    assert cand['quality_wait']['payload']['statistics']['median_hours']==48
    assert cand['transit']['payload']['classification']=='long_duration'
    assert r['summary']['metrics']['quality_work']['median_hours']==1
    impact=rows(c,r,'impact')[0]['payload'];assert impact['quantity']==10 and impact['blocked_hours']==169
    before=counts(e);improvement=req(c,f"reviews/rows/{cand['quality_wait']['id']}/improve",dict(reason='增加检验班次并复盘'))
    repeated=req(c,f"reviews/rows/{cand['quality_wait']['id']}/improve",dict(reason='再次查看已建任务'))
    assert repeated['task_id']==improvement['task_id'] and repeated['business_replayed']
    assert counts(e)['ledger']==before['ledger']


def test_gold_registration_delay_is_not_slow_transport(env):
    e,c=env;flow(e,c,late_entry=48);r=report(e,c);cand={x['payload']['metric']:x['payload'] for x in rows(c,r,'candidate') if x['sku_id']=='sku-1'}
    assert cand['registration_delay']['classification']=='data_delay'
    assert cand['transit']['classification']=='long_duration'
    assert r['summary']['metrics']['transit']['median_hours']==120
    assert r['summary']['metrics']['purchase_original']['ontime_rate']==1


def test_gold_unknown_time_and_unlinked_work_do_not_prove_constraint(env):
    e,c=env;flow(e,c,unknown=True,linked=False);r=report(e,c)
    candidates=[x['payload'] for x in rows(c,r,'candidate')]
    assert any(x['classification']=='unknown' for x in candidates)
    assert not any(x['classification']=='evidenced_constraint' for x in candidates)
    assert r['summary']['metrics']['transit']['median_hours'] is None
    assert r['summary']['impact_quantity']==0


def test_historic_report_reconstructs_unfinished_queue(env):
    e,c=env;flow(e,c)
    r=report(e,c,end_at='2026-10-07T12:00:00Z')
    assert r['summary']['metrics']['quality_wait']['open_quantity']==10
    assert r['summary']['metrics']['quality_wait']['valid_completed']==0
    assert r['summary']['metrics']['quality_wait']['longest_open_hours']==36
    assert 'quality_work' not in r['summary']['metrics']


def test_append_only_correction_old_report_and_stale_correction(env):
    e,c=env;flow(e,c,unknown=True);old=report(e,c)
    original=next(x['payload'] for x in rows(c,old,'event') if x['payload']['event_type']=='supply_batch.receive')
    body=dict(expected_event_id=original['id'],occurred_at='2026-10-06T00:00:00Z',planned_at=original['planned_at'],time_quality='verified',reason='核对签收凭证订正实际时间')
    fix=req(c,f"reviews/events/{original['id']}/correct",body)
    assert req(c,f"reviews/events/{original['id']}/correct",body,ok=409)['error']['code']=='EVENT_VERSION_CONFLICT'
    fresh=report(e,c);assert fresh['summary']['metrics']['transit']['median_hours']==120
    assert fresh['summary']['correction_count']==1 and old['source_hash']!=fresh['source_hash']
    assert c.get('/api/v2/reviews/reports/'+old['id']).json()==old
    for table,identity in ((s.reports,old['id']),(events.business_events,original['id'])):
        with pytest.raises(DBAPIError),e.begin() as conn:conn.execute(update(table).where(table.c.id==identity).values(tenant_id='rewritten'))
    assert fix['old_reports_unchanged']


def test_negative_duration_preserved_and_calendar_quantiles():
    assert metrics.working_hours('2026-10-23T08:00:00Z','2026-10-26T04:00:00Z',m.Calendar().model_dump(),'Asia/Shanghai')==5
    data=dict(events=[],timezone='UTC')
    body=m.Analyze(warehouse_id='w',start_at='2026-10-01T00:00:00Z',end_at='2026-10-10T00:00:00Z').model_dump()
    sample=dict(metric='quality_wait',object_id='x',sku_id='s',supplier_id=None,connection_id=None,quantity=3,completed=True,
        start_at='2026-10-03T00:00:00Z',end_at='2026-10-02T00:00:00Z',planned_at=None,sources=[],flags=[],event_ids=[])
    measured=metrics.measure([sample],data,body)[0]
    assert measured['raw_hours']==-24 and measured['hours'] is None and 'negative_duration' in measured['flags']
    stat=metrics.summarize([dict(measured,hours=n) for n in range(1,11)],5)
    assert (stat['median_hours'],stat['p90_hours'],stat['p95_hours'])==(5.5,9,10)


def test_expired_lease_and_crash_roll_back_then_publish_once(env):
    e,c=env;flow(e,c);a=actor(e)
    job=review.create(e,a,dict(warehouse_id='wh-cn',start_at='2026-10-01T00:00:00Z',end_at=business_now(e)),key())
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO analysis_rows'):raise RuntimeError('crash before publish')
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError):review.process(e,job['id'])
    finally:event.remove(e,'before_cursor_execute',crash)
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.reports))==0
    with db.write(e,a['tenant_id']) as conn:conn.execute(update(s.jobs).where(s.jobs.c.id==job['id']).values(lease_until=0,next_attempt_at=0))
    report_id=review.recover(e)[0];assert review.process(e,job['id'])==report_id
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.reports))==1


def test_report_authorization_rechecked_after_connection_revocation(env):
    from app import m4_schema as sales
    e,c=env;_,sale=flow(e,c);r=report(e,c);login(c,'warehouse')
    assert c.get('/api/v2/reviews/reports/'+r['id']).status_code==200
    with db.write(e,actor(e)['tenant_id']) as conn:conn.execute(update(sales.connections).where(sales.connections.c.id==sale['connection_id']).values(member_ids=[]))
    assert c.get('/api/v2/reviews/reports/'+r['id']).status_code==404
    assert c.get('/api/v2/reviews/reports').json()['items']==[]


def test_agent_deterministic_plan_does_not_confirm_and_scope_cannot_expand(env,monkeypatch):
    from app import agent_model,workflows,m5_schema as ps
    e,c=env;p=rule(e,c)
    result=req(c,'review-agent/tools',dict(tool='calculate_replenishment',identity=p['id']))
    plan=result['result']['plan'];assert plan['suggested']==384
    explained=req(c,'review-agent/tools',dict(tool='explain_replenishment',identity=plan['id']))
    assert explained['result']['snapshot']['calculation']['net_before_purchase_constraints']==382
    with e.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(workflows.orders))==0
        assert conn.scalar(select(func.count()).select_from(ps.decisions))==0
    monkeypatch.setattr(agent_model,'tool_plan',lambda *a,**k:m.Tool(tool='explain_replenishment',identity='foreign-plan'))
    body=dict(question='解释所选补货建议',selected_tool='explain_replenishment',identity=plan['id'])
    assert req(c,'review-agent/ask',body,ok=502)['error']['code']=='MODEL_SCOPE_REJECTED'
    monkeypatch.setattr(agent_model,'tool_plan',lambda *a,**k:m.Tool(tool='unsupported'))
    assert req(c,'review-agent/ask',dict(body,question='执行SQL直接修改库存'))['tool']=='unsupported'
    assert req(c,'review-agent/tools',dict(tool='approve',identity=plan['id']),ok=422)


def test_agent_warehouse_role_cannot_read_foreign_plan_or_cashier_run_tools(env):
    e,c=env;p=rule(e,c);plan=calculate(c,p);login(c,'cashier')
    assert req(c,'review-agent/tools',dict(tool='explain_replenishment',identity=plan['id']),ok=403)['error']['code']=='FORBIDDEN'


def test_analysis_input_bounds_and_csrf(env):
    e,c=env;start(e)
    body=dict(warehouse_id='wh-cn',start_at='2025-01-01T00:00:00Z',end_at=business_now(e))
    req(c,'reviews/jobs',body,ok=422)
    body['start_at']='2026-09-30T00:00:00Z'
    headers=dict(c.headers);c.headers.pop('X-CSRF-Token')
    assert c.post('/api/v2/reviews/jobs',json=body,headers={'Idempotency-Key':key()}).status_code==403
    c.headers.update(headers)


def test_bad_source_clock_does_not_invent_open_transport(env):
    e,c=env;flow(e,c,queue_hours=6);old=report(e,c)
    receipt=next(x['payload'] for x in rows(c,old,'event') if x['payload']['event_type']=='supply_batch.receive')
    req(c,f"reviews/events/{receipt['id']}/correct",dict(expected_event_id=receipt['id'],occurred_at='2026-10-07T00:00:00Z',
        planned_at=receipt['planned_at'],time_quality='reported',reason='保留来源时钟异常待核实'))
    r=report(e,c);stat=r['summary']['metrics']['transit']
    assert stat['open_quantity']==0 and stat['unknown_samples']==1 and stat['median_hours'] is None
    assert not any(x['payload']['classification']=='evidenced_constraint' for x in rows(c,r,'candidate'))
    assert len(r['summary']['data_quality_tasks'])==1
    before=counts(e)
    again=report(e,c)
    assert again['summary']['data_quality_tasks']==r['summary']['data_quality_tasks']
    assert counts(e)['ledger']==before['ledger']


def test_unreceived_purchase_original_latest_and_historic_close(env):
    e,c=env;start(e)
    p=req(c,'purchase-plans',dict(code=key(),warehouse_id='wh-cn',supplier_id=vendor(e)['id'],ordered_at=business_now(e),
        promised_at='2026-10-02T00:00:00Z',lines=[dict(sku_id='sku-1',quantity=10)]))
    move(e,'2026-10-03T00:00:00Z')
    p=req(c,f"purchase-plans/{p['id']}/promise",dict(expected_version=p['version'],reference=key(),promised_at='2026-10-05T00:00:00Z',reason='供应商提供新的交期'))
    before=report(e,c);stat=before['summary']['metrics']
    assert stat['purchase_original']['open_quantity']==10 and stat['purchase_original']['overdue_quantity']==10
    assert stat['purchase_latest']['overdue_quantity']==0 and stat['purchase_latest']['ontime_denominator']==0
    assert stat['purchase_latest']['median_hours'] is None
    move(e,'2026-10-04T00:00:00Z')
    req(c,f"supply-orders/purchase/{p['id']}/close",dict(expected_version=p['version'],reference=key(),reason='明确关闭未发余量'))
    assert 'purchase_original' not in report(e,c)['summary']['metrics']
    historic=report(e,c,end_at='2026-10-02T12:00:00Z')['summary']['metrics']
    assert historic['purchase_latest']['open_quantity']==10 and historic['purchase_latest']['overdue_quantity']==10
    assert c.get('/api/v2/reviews/reports/'+before['id']).json()==before


def test_report_input_snapshot_excludes_other_warehouse_purchase_terms(env):
    from test_phase3 import make_target
    e,c=env;start(e);warehouse=make_target(c)
    foreign=req(c,'purchase-plans',dict(code=key(),warehouse_id=warehouse,supplier_id=vendor(e)['id'],ordered_at=business_now(e),
        promised_at='2026-10-02T00:00:00Z',lines=[dict(sku_id='sku-1',quantity=7)]))
    move(e,'2026-10-02T00:00:00Z');r=report(e,c)
    assert all(x['payload'].get('record',{}).get('order_id')!=foreign['id'] for x in rows(c,r,'input'))


def test_parallel_quantity_and_critical_path_do_not_sum_overlapping_branches():
    body=m.Analyze(warehouse_id='w',start_at='2026-10-01T00:00:00Z',end_at='2026-10-08T02:00:00Z',min_samples=1).model_dump()
    def event(kind,t,**kw):return dict(event_type=kind,occurred_at=t,registered_at=t,time_quality='observed',quality_flags=[],object_id='order',object_line_id='line',payload={},quantity=0,lifecycle='registered',task_id=None,evidence_ids=[],**kw)
    confirmed=event('fulfillment.confirmed',body['start_at']);linked=event('fulfillment.supply_linked',body['start_at']);linked['payload']={'link_id':'link'}
    data=dict(events=[confirmed,linked],lines=[dict(id='line',order_id='order',sku_id='s',quantity=10)],orders=[dict(id='order',connection_id='account')],
        links=[dict(id='link',line_id='line',quantity=10,source_type='purchase_line',source_id='purchase')],balances=[dict(sku_id='s',version=0)],timezone='UTC',scope_incomplete=False)
    raw=[]
    for receipt,qty,offset in [('one',4,0),('two',6,1)]:
        arrival='2026-10-06T0'+str(offset)+':00:00Z';start='2026-10-08T0'+str(offset)+':00:00Z';end='2026-10-08T0'+str(offset+1)+':00:00Z'
        for metric,left,right in [('transit',body['start_at'],arrival),('quality_wait',arrival,start),('quality_work',start,end)]:
            raw.append(dict(metric=metric,object_id=receipt,sku_id='s',supplier_id='supplier',connection_id=None,quantity=qty,completed=True,
                start_at=left,end_at=right,planned_at=None,sources=[dict(type='purchase_line',id='purchase')],flags=[],event_ids=[],dependency_branch=receipt))
    result=metrics.analyze(raw,data,body);line=result['impacts'][0]
    assert line['quantity']==10 and line['blocked_hours']==170
    assert line['dependency_graph']['critical_hours']==170
    assert len(line['dependency_graph']['edges'])==4
    assert result['summary']['constraint_quantity']==10


def test_current_permission_is_rechecked_after_model_response(env,monkeypatch):
    from app import agent_model
    e,c=env;p=rule(e,c);plan=calculate(c,p);a=actor(e)
    def changed(*args,**kwargs):
        with db.write(e,a['tenant_id']) as conn:conn.execute(update(db.users).where(db.users.c.id==a['id']).values(role='cashier'))
        return m.Tool(tool='explain_replenishment',identity=plan['id'])
    monkeypatch.setattr(agent_model,'tool_plan',changed)
    assert req(c,'review-agent/ask',dict(question='解释所选方案',selected_tool='explain_replenishment',identity=plan['id']),ok=401)['error']['code']=='UNAUTHENTICATED'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.audit))==0


def test_concurrent_job_workers_publish_single_report(env):
    e,c=env;flow(e,c);a=actor(e);job=review.create(e,a,dict(warehouse_id='wh-cn',start_at='2026-10-01T00:00:00Z',end_at=business_now(e)),key())
    gate=Barrier(2)
    def run(_):gate.wait();return review.process(e,job['id'])
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
    with e.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(s.reports))==1
        report_id=review.get_job(conn,a,job['id'])['report_id']
    assert all(x in (None,report_id) for x in results)
