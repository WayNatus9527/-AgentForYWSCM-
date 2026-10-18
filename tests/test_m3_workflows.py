"""M3 acceptance: locks, independent review, lots, retries and tenant scopes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import select,insert,update,delete,func,event
from fastapi.testclient import TestClient
from app import db,counting as ct,quality_control as qc,exception_tasks as tasks,operations as op,inventory as inv,commitments,workflows
from app.domain import BusinessError,FIELDS
from app.m3_models import Details
from app.clock import VirtualBusinessClock
from app.main import create_app
from app.migrations import migrate
from test_inventory import env,actor,balance,counts,body,post
from test_phase2 import login,send
from test_phase3 import return_customer,inspection,transfer,advance


def meta(**kw):
    return Details(opinion='已核对现场清点及收货证据',evidence_refs=['simulated-evidence-001'],**kw).model_dump()

def req(c,path,body=None,key=None,ok=200):
    r=c.post('/api/v2/'+path,json=body or {},headers={'Idempotency-Key':key or str(uuid4())})
    assert r.status_code==ok,r.text
    return r.json()

def new_count(c,skus=('sku-1',)):
    return req(c,'counts',{'code':str(uuid4()),'reason':'月度模拟盘点','lines':[{'warehouse_id':'wh-cn','sku_id':s} for s in skus]})

def act(c,row,action,extra=None,**kw):
    return req(c,f"counts/{row['id']}/{action}",{'expected_version':row['version'],'reason':'核对盘点记录',**(extra or {})},**kw)

def started(c,skus=('sku-1',)):
    row=new_count(c,skus);row=act(c,row,'confirm');return act(c,row,'start')

def observe(c,row,action='initial',deltas=None):
    deltas=deltas or {}
    return act(c,row,'observations/'+action,{'lines':[dict(line_id=l['id'],observed={f:l['snapshot'][f]+deltas.get(l['sku_id'],{}).get(f,0) for f in ('g','q','d')},details=meta()) for l in row['lines']]})

def awaiting(c,skus=('sku-1',),deltas=None):
    r=started(c,skus);r=observe(c,r,deltas=deltas)
    if deltas:r=observe(c,r,'recount',deltas)
    return act(c,r,'submit')

def test_count_lock_scope_cancel_and_existing_approval(env):
    e,c=env;login(c,'warehouse')
    approval=send(c,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':1,'delta':{'g':1},'reason':'盘点前申请调整'}).json()
    row=started(c);before=balance(e);login(c,'admin')
    assert post(c,body()).json()['error']['code']=='COUNT_LOCKED'
    blocked=send(c,f"/approvals/{approval['id']}/review",{'decision':'approve','note':'禁止绕过盘点'})
    assert blocked.json()['error']['code']=='COUNT_LOCKED'
    assert post(c,{**body(),'sku_id':'sku-2'},key='outside-count').status_code==200
    assert balance(e)==before
    act(c,row,'cancel');assert not c.get('/api/v2/count-locks').json()
    assert balance(e)==before
    assert post(c,body(),key='after-cancel').status_code==200


def test_count_recount_other_reviewer_idempotent_and_history(env):
    e,c=env;login(c,'warehouse');r=started(c);r=observe(c,r,deltas={'sku-1':{'g':-3}})
    assert act(c,r,'submit',ok=409)['error']['code']=='RECOUNT_REQUIRED'
    r=observe(c,r,'recount',{'sku-1':{'g':-2}});r=act(c,r,'submit')
    login(c,'admin');before=counts(e)
    done=act(c,r,'approve',key='approve-count-001');assert done['status']=='posted'
    assert balance(e)['g']==326 and not c.get('/api/v2/count-locks').json()
    assert [x['observed']['g'] for x in done['observations']]==[325,326]
    assert act(c,r,'approve',key='approve-count-001')['replayed']
    after=counts(e);assert after['ledger']==before['ledger']+1
    with e.begin() as conn:
        with pytest.raises(Exception):conn.execute(update(ct.observations).values(kind='forged'))


def test_self_review_and_stale_recount_denied(env):
    e,c=env;r=awaiting(c)
    assert act(c,r,'approve',ok=403)['error']['code']=='SELF_APPROVAL'
    assert act(c,{**r,'version':r['version']-1},'cancel',ok=409)['error']['code']=='VERSION_CONFLICT'
    assert balance(e)['g']==328 and len(c.get('/api/v2/count-locks').json())==1


def test_multi_line_approval_rollback_on_second_post(env):
    e,c=env;login(c,'warehouse');r=awaiting(c,('sku-1','sku-2'),{'sku-1':{'g':1},'sku-2':{'g':2}});login(c,'admin')
    before=counts(e);beforestock=c.get('/api/v1/inventory').json();n=[0]
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO ledger'):
            n[0]+=1
            if n[0]==2:raise RuntimeError('simulated failure at second ledger')
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError):ct.act(e,actor(e),r['id'],'approve',{'expected_version':r['version'],'reason':'原子性注入'},'atomic-approval')
    finally:event.remove(e,'before_cursor_execute',crash)
    assert counts(e)==before and c.get('/api/v1/inventory').json()==beforestock
    assert len(c.get('/api/v2/count-locks').json())==2
    assert act(c,r,'approve')['status']=='posted'


def test_concurrent_start_only_one_wins_and_disjoint_continue(env):
    e,c=env;r1=act(c,new_count(c),'confirm');r2=act(c,new_count(c),'confirm');gate=Barrier(2);a=actor(e)
    def run(r):
        gate.wait()
        try:return ct.act(e,a,r['id'],'start',{'expected_version':r['version'],'reason':'并发模拟启动'},str(uuid4()))['status']
        except BusinessError as ex:return ex.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[r1,r2]))
    assert sorted(results)==['COUNT_LOCKED','counting']
    assert started(c,('sku-2',))['status']=='counting'
    assert len(c.get('/api/v2/count-locks').json())==2


def test_persistent_lock_survives_restart_and_no_commitment_bypass(env):
    e,c=env;r=started(c)
    if e.dialect.name=='sqlite':
        fresh=db.make_engine(str(e.url));migrate(fresh)
    else:fresh=e
    try:
        with pytest.raises(BusinessError,match='正在盘点'):
            inv.receive(fresh,actor(e),{**body(),'source_account':'src','reference':'locked'},'locked-receipt')
        with pytest.raises(BusinessError) as error:
            commitments.authorize(fresh,actor(e),dict(warehouse_id='wh-cn',sku_id='sku-1',quantity=1,expected_version=1,
                source_account='channel',reference='quota-1',action_id='quota-action',restore_on_cancel=True),'locked-quota')
        assert error.value.code=='COUNT_LOCKED'
        with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(commitments.grants))==0
    finally:
        if fresh is not e:fresh.dispose()


def test_conflict_retains_lock_and_task_closing_never_posts(env):
    e,c=env
    commitments.authorize(e,actor(e),dict(warehouse_id='wh-cn',sku_id='sku-1',quantity=320,expected_version=1,source_account='online',reference='quota',action_id='a1',restore_on_cancel=True),'quota-create')
    login(c,'warehouse');r=awaiting(c,deltas={'sku-1':{'g':-10}});login(c,'admin');before=balance(e)
    conflict=act(c,r,'approve');assert conflict['status']=='conflict' and balance(e)==before
    ts=c.get('/api/v2/exceptions').json()['items'];assert len(ts)==1 and ts[0]['requires_review']
    t=tasks.act(e,actor(e),ts[0]['id'],'assign',{'expected_version':ts[0]['version'],'owner_id':'warehouse','reason':'安排核对'},'task-assign')
    t=tasks.act(e,actor(e,'warehouse'),t['id'],'resolve',{'expected_version':t['version'],'reason':'已调查差异','result':'重新盘点并核查凭证','details':meta()},'task-resolve')
    t=tasks.act(e,actor(e),t['id'],'approve',{'expected_version':t['version'],'reason':'结果复核通过'},'task-review')
    assert t['status']=='closed' and balance(e)==before and len(c.get('/api/v2/count-locks').json())==1
    # Corrected physical observation after recount can be submitted afresh.
    login(c,'warehouse');r=observe(c,conflict,'recount',{'sku-1':{'g':-2}});r=act(c,r,'submit');login(c,'admin')
    assert act(c,r,'approve')['status']=='posted' and balance(e)['g']==326


def test_count_cannot_consume_return_quality_hold(env):
    e,c=env;return_customer(c);login(c,'warehouse');r=awaiting(c,deltas={'sku-1':{'q':-1}});login(c,'admin')
    assert act(c,r,'approve')['status']=='conflict' and balance(e)['q']==2


def test_transfer_rollback_with_locked_line(env):
    e,c=env;t=transfer(c);started(c)
    assert advance(c,t,'reserve',1,1).json()['error']['code']=='COUNT_LOCKED'
    assert c.get('/api/v1/transfers').json()[0]['status']=='draft' and balance(e)['t']==0


def start_batch(c,ret=None,quantity=2):
    return req(c,'inspections',dict(warehouse_id='wh-cn',sku_id='sku-1',return_id=ret['id'] if ret else None,
        reference=str(uuid4()),quantity=quantity,expected_balance_version=3 if ret else 2,details=meta(reason_code='quality')))

def qact(c,row,action,**extra):
    return req(c,f"inspections/{row['id']}/{action}",dict(expected_version=row['version'],reason='验证质量分步处置',**extra))

def test_staged_quality_partial_restock_scrap_and_double_review(env):
    e,c=env;ret,_=return_customer(c,4);login(c,'warehouse');batch=start_batch(c,ret,3)
    assert inspection(c,ret,1,0).json()['error']['code']=='QUALITY_RESERVED'
    batch=qact(c,batch,'complete',good=2,bad=1,expected_balance_version=3,details=meta(reason_code='quality',defect_category='damaged'))
    assert balance(e)['g']==323 and balance(e)['q']==3 and balance(e)['d']==1
    assert c.get('/api/v1/returns').json()[0]['status']=='awaiting_quality'
    assert send(c,'/quality',dict(warehouse_id='wh-cn',sku_id='sku-1',reference='cannot-use-inspected',good=1,bad=0,expected_version=4,reason='不能挪用已检待上架')).json()['error']['code']=='QUALITY_RESERVED'
    batch=qact(c,batch,'restock',quantity=1,expected_balance_version=4)
    assert balance(e)['g']==324 and balance(e)['q']==2
    d=req(c,'quality-dispositions',dict(kind='scrap',session_id=batch['id'],reference='scrap-001',quantity=1,expected_balance_version=5,details=meta(reason_code='quality')))
    assert balance(e)['d']==1
    login(c,'admin');done=req(c,f"quality-dispositions/{d['id']}/review",dict(expected_version=1,decision='approve',reason='核对残次报废凭证'))
    assert done['status']=='approved' and balance(e)['d']==0
    assert len(c.get('/api/v2/exceptions').json()['items'])==1


def test_inspection_cancel_preserves_stock_and_releases_batch_hold(env):
    e,c=env;post(c,body());batch=start_batch(c,quantity=2);before=balance(e)
    assert send(c,'/quality',dict(warehouse_id='wh-cn',sku_id='sku-1',reference='generic-during',good=1,bad=0,expected_version=2,reason='已有专属检验')).json()['error']['code']=='QUALITY_RESERVED'
    qact(c,batch,'cancel');assert balance(e)==before
    assert send(c,'/quality',dict(warehouse_id='wh-cn',sku_id='sku-1',reference='generic-after',good=1,bad=0,expected_version=2,reason='释放后普通检验')).status_code==200


def test_quality_lock_blocks_new_or_running_inspection_and_scrap(env):
    e,c=env;post(c,body());batch=start_batch(c,quantity=2);r=started(c)
    result=req(c,f"inspections/{batch['id']}/complete",dict(expected_version=1,reason='锁定范围检验',good=1,bad=1,expected_balance_version=2,details=meta()),ok=409)
    assert result['error']['code']=='COUNT_LOCKED' and balance(e)['q']==2
    act(c,r,'cancel');batch=qact(c,batch,'complete',good=1,bad=1,expected_balance_version=2,details=meta())
    login(c,'warehouse');d=req(c,'quality-dispositions',dict(kind='scrap',session_id=batch['id'],reference='scrap-locked',quantity=1,expected_balance_version=3,details=meta()))
    started(c);login(c,'admin')
    denied=req(c,f"quality-dispositions/{d['id']}/review",dict(expected_version=1,decision='approve',reason='不能绕过盘点'),ok=409)
    assert denied['error']['code']=='COUNT_LOCKED' and balance(e)['d']==2
    assert c.get('/api/v2/inspections').json()['items'][0]['disposed']==0


def test_return_and_legacy_inspection_voucher_dedup(env):
    e,c=env;r,sale=return_customer(c)
    b=dict(kind='customer',original_id=sale['document_id'],reference='RT-001',quantity=2,expected_version=3,reason='客户实物退回')
    assert send(c,'/returns',b,'new-return-key').json()['business_replayed']
    assert send(c,'/returns',{**b,'quantity':1},'changed-return-key').json()['error']['code']=='VOUCHER_CONFLICT'
    q=inspection(c,r).json();assert inspection(c,r,version=4,ret_version=2,key='new-quality-key').json()['document_id']==q['document_id']
    assert balance(e)['g']==324 and balance(e)['q']==0


def task_body(e,**kw):
    with e.connect() as conn:doc=conn.scalar(select(db.documents.c.id).where(db.documents.c.warehouse_id=='wh-cn',db.documents.c.sku_id=='sku-1'))
    return dict(object_type='stock_document',object_id=doc,warehouse_id='wh-cn',sku_id='sku-1',rule='manual',rule_version='v1',instance='same-instance',type='operation',priority='normal',impact_quantity=2,due_at='2026-01-02T00:00:00Z',details=meta(),requires_review=False,**kw)

def test_unique_task_metric_history_pause_review_reopen_and_reminders(env):
    e,c=env;e.business_clock=VirtualBusinessClock('2026-01-01T00:00:00Z');b=task_body(e)
    t=req(c,'exceptions',b);same=req(c,'exceptions',b);assert t['id']==same['id'] and t['version']==same['version']
    t=req(c,'exceptions',{**b,'impact_quantity':5,'rule_version':'v2'});assert len(t['history'])==2
    assert req(c,'exceptions/remind')['reminders_created']==0;e.business_clock.advance(seconds=172800)
    assert req(c,'exceptions/remind')['reminders_created']==1 and req(c,'exceptions/remind')['reminders_created']==0
    t=req(c,f"exceptions/{t['id']}/assign",dict(expected_version=t['version'],owner_id='warehouse',reason='分派现场核查'))
    login(c,'warehouse');t=req(c,f"exceptions/{t['id']}/pause",dict(expected_version=t['version'],reason='等待凭证返回'));assert t['status']=='paused'
    t=req(c,f"exceptions/{t['id']}/resume",dict(expected_version=t['version'],reason='收到补充凭证'))
    d=meta(responsibility_type='person',responsibility_ref='人员候选001')
    t=req(c,f"exceptions/{t['id']}/resolve",dict(expected_version=t['version'],reason='调查结果提交',result='已修正作业流程并提出责任复核',details=d));assert t['status']=='awaiting_review'
    login(c,'admin')
    assert req(c,f"exceptions/{t['id']}/review/approve",dict(expected_version=t['version'],reason='未明确复核'),ok=422)['error']['code']=='RESPONSIBILITY_REVIEW_REQUIRED'
    t=req(c,f"exceptions/{t['id']}/review/reject",dict(expected_version=t['version'],reason='补充责任证据'));assert t['status']=='in_progress'
    login(c,'warehouse');t=req(c,f"exceptions/{t['id']}/resolve",dict(expected_version=t['version'],reason='证据已补齐',result='结果再次复核',details=d));login(c,'admin')
    t=req(c,f"exceptions/{t['id']}/review/approve",dict(expected_version=t['version'],reason='已核实实际责任',confirm_responsibility=True));assert t['responsibility_confirmed']
    t=req(c,f"exceptions/{t['id']}/reopen",dict(expected_version=t['version'],reason='发现新的证据'));assert t['status']=='in_progress' and not t['responsibility_confirmed']


@pytest.mark.parametrize('path', ['counts','count-locks','exceptions','inspections','quality-dispositions'])
def test_cashier_cannot_read_m3(env,path):
    _,c=env;login(c,'cashier');assert c.get('/api/v2/'+path).status_code==403


def test_revoked_permission_cannot_replay_m3_response(env):
    e,c=env;login(c,'warehouse');b={'code':'permission-test','reason':'权限变化测试','lines':[{'warehouse_id':'wh-cn','sku_id':'sku-1'}]}
    req(c,'counts',b,'scope-replay-key')
    with e.begin() as conn:conn.execute(update(db.users).where(db.users.c.id=='warehouse').values(warehouse_ids=['wh-us']))
    login(c,'warehouse');req(c,'counts',b,'scope-replay-key',ok=404)
    assert c.get('/api/v2/counts').json()['items']==[]


def test_cross_tenant_link_and_owner_denied(env):
    e,c=env
    with e.begin() as conn:
        conn.execute(insert(db.tenants).values(id='foreign',name='隔离商户'))
        conn.execute(insert(db.users).values(id='foreign-user',tenant_id='foreign',username='foreign',role='admin',warehouse_ids=[]))
        conn.execute(insert(db.documents).values(id='foreign-doc',tenant_id='foreign',warehouse_id='wh-cn',sku_id='sku-1',kind='receipt',quantity=100))
    b=task_body(e);req(c,'exceptions',{**b,'object_id':'foreign-doc'},ok=404)
    t=req(c,'exceptions',b);req(c,f"exceptions/{t['id']}/assign",dict(expected_version=t['version'],owner_id='foreign-user',reason='不允许越权分派'),ok=422)
    foreign={**actor(e),'id':'foreign-user','tenant_id':'foreign','warehouse_ids':[]}
    assert ct.listing(e,foreign)['items']==[]
    with pytest.raises(BusinessError) as ex:tasks.act(e,foreign,t['id'],'reopen',dict(expected_version=t['version'],reason='跨商户拒绝'),'foreign-reopen')
    assert ex.value.code=='NOT_FOUND'


def test_missing_evidence_unknown_baseline_and_invalid_scope(env):
    e,c=env;r=started(c);line=r['lines'][0]
    result=act(c,r,'observations/initial',{'lines':[dict(line_id=line['id'],observed={'g':327,'q':0,'d':0},details={**meta(),'evidence_refs':[]})]},ok=422)
    assert result['error']['code']=='EVIDENCE_REQUIRED'
    act(c,r,'cancel');new=send(c,'/skus',{'code':'NO-BASELINE','name':'未建立库存','spec':'标准'}).json()
    r=new_count(c,(new['id'],));r=act(c,r,'confirm');assert act(c,r,'start',ok=409)['error']['code']=='UNKNOWN_BASELINE'
    assert not c.get('/api/v2/count-locks').json()


def test_task_priority_escalates_and_reviewed_details_cannot_be_overwritten(env):
    e,c=env;b=task_body(e);t=req(c,'exceptions',b)
    t=req(c,'exceptions',{**b,'priority':'critical'})
    assert t['priority']=='critical' and t['requires_review']
    t=req(c,f"exceptions/{t['id']}/assign",dict(expected_version=t['version'],owner_id='warehouse',reason='分派重大差异'))
    login(c,'warehouse');reviewed=meta(responsibility_type='person',responsibility_ref='reviewed-person')
    t=req(c,f"exceptions/{t['id']}/resolve",dict(expected_version=t['version'],reason='人员责任复核',result='现场证据支持所述结果',details=reviewed))
    login(c,'admin');t=req(c,f"exceptions/{t['id']}/review/approve",dict(expected_version=t['version'],reason='确认所述责任',confirm_responsibility=True))
    observed={**b,'impact_quantity':6,'details':meta(responsibility_type='person',responsibility_ref='unreviewed-other')}
    changed=req(c,'exceptions',observed);again=req(c,'exceptions',observed)
    assert changed['status']=='closed' and changed['details']['responsibility_ref']=='reviewed-person'
    assert again['version']==changed['version'] and changed['history'][-1]['payload']['details']['responsibility_ref']=='unreviewed-other'


def test_pre_m3_completed_supplier_return_replays_without_new_approval(env):
    e,c=env;original=post(c,body()).json();login(c,'warehouse')
    b=dict(kind='supplier',original_id=original['document_id'],reference='legacy-finished',g=1,q=0,d=0,expected_version=2,reason='升级前已完成退货')
    a=actor(e,'warehouse')
    old=workflows.command(e,a,'return.create',b,'pre-m3-finished',lambda conn:op.apply_return(conn,a,b))
    before=counts(e);reply=send(c,'/returns',b,'pre-m3-finished')
    assert reply.status_code==200 and reply.json()['replayed'] and reply.json()['id']==old['id']
    assert counts(e)==before and not c.get('/api/v2/quality-dispositions').json()['items']
    # A fresh request still needs independent approval.
    fresh=send(c,'/returns',{**b,'reference':'new-reviewed-return','expected_version':3},'post-m3-new-return')
    assert fresh.status_code==200 and fresh.json()['status']=='awaiting_review'


def test_confirmed_zero_without_balance_can_be_counted_and_posted(env):
    from app import catalog
    e,c=env;a=actor(e);s=send(c,'/skus',{'code':'KNOWN-ZERO','name':'已确认零库存','spec':'标准'}).json()['id']
    ar=catalog.set_assortment(e,a,'wh-cn',s,dict(active=True,replenishment=False,expected_version=0),'zero-assortment')
    catalog.confirm_inventory(e,a,'wh-cn',s,dict(expected_version=ar['version'],expected_balance_version=0,reference='zero-confirmed',observed={'g':0,'q':0,'d':0}),'zero-confirmation')
    login(c,'warehouse');r=awaiting(c,(s,),{s:{'g':2}});login(c,'admin');r=act(c,r,'approve')
    assert r['status']=='posted'
    with e.connect() as conn:
        row=conn.execute(select(db.balances).where(db.balances.c.sku_id==s)).mappings().one()
        assert row['g']==2 and row['version']==1
