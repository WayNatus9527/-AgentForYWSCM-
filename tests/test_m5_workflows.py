"""M5 quantity conservation, evidence, race and planning boundary regression."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import select, update, insert, func, event
from sqlalchemy.exc import DBAPIError
from app import db, supply, planning, m5_schema as s, catalog, quality_control as qc, workflows as w, operations as op, accounts
from app.domain import BusinessError
from app.clock import VirtualBusinessClock
from test_inventory import env, actor, balance, counts
from test_m3_workflows import req, meta, started, act as count_act
from test_phase2 import send,login
from test_phase3 import make_target
from test_m4_workflows import reserved,act as sale_act


def vendor(e):
    with e.connect() as c:
        old=c.execute(select(catalog.suppliers).where(catalog.suppliers.c.code=='M5-VENDOR')).mappings().first()
        if old:return dict(old)
    return catalog.save_supplier(e,actor(e),dict(code='M5-VENDOR',name='模拟供货商',default_lead_days=2,sku_ids=['sku-1','sku-2']),str(uuid4()))

def purchase(e,c,n=12,**kw):
    return req(c,'purchase-plans',dict(code=str(uuid4()),warehouse_id='wh-cn',supplier_id=vendor(e)['id'],ordered_at='2026-10-24T01:00:00Z',
        promised_at='2026-10-26T01:00:00Z',lines=[dict(sku_id='sku-1',quantity=n)],**kw))

def transfer(e,c,n=12):
    return req(c,'transfer-plans',dict(code=str(uuid4()),warehouse_id='wh-cn',destination_id=make_target(c),reason='分批调拨模拟',
        expected_at='2026-10-26T01:00:00Z',lines=[dict(sku_id='sku-1',quantity=n)]))

def batches(e,c,r,n,**kw):
    return req(c,f"supply-orders/{r['kind']}/{r['id']}/batches",dict(expected_version=r['version'],reference=str(uuid4()),reason='独立运输批次',
        supply_line_id=r['lines'][0]['id'],quantity=n,expected_at='2026-10-26T01:00:00Z',expected_balance_version=balance(e)['version'] if r['kind']=='transfer' else None,**kw))

def batch(r,index=-1):return r['lines'][0]['batches'][index]

def b_action(e,c,r,action,*,index=-1,ok=200,key=None,**kw):
    b=batch(r,index);body=dict(expected_version=b['version'],reference=str(uuid4()),reason='模拟批次凭证')
    if action in ('ship','receive'):body['occurred_at']='2026-10-25T01:00:00Z'
    if action=='receive':
        with e.connect() as conn:stock=w.balance(conn,actor(e),b['warehouse_id'],b['sku_id'])
        body['expected_balance_version']=stock['version']
    elif r['kind']=='transfer':body['expected_balance_version']=balance(e)['version']
    body.update(kw)
    return req(c,f"supply-batches/{b['id']}/{action}",body,key=key,ok=ok)

def frozen(e):e.business_clock=VirtualBusinessClock('2026-10-25T00:00:00Z')

def rule(e,c,*,history=True,quantity=100,**kw):
    frozen(e)
    p=dict(warehouse_id='wh-cn',sku_id='sku-1',supplier_id=vendor(e)['id'],window_days=3,coverage_days=5,lead_days=2,safety=10,minimum=10,pack=6)
    p.update(kw)
    r=req(c,'planning/policies',p)
    if history:
        for date in ['2026-10-22','2026-10-23','2026-10-24']:
            req(c,'planning/demand',dict(warehouse_id='wh-cn',sku_id='sku-1',day=date,quantity=quantity,source_reference='sim-'+date))
    return r

def calculate(c,p):return req(c,f"planning/policies/{p['id']}/calculate")

def test_purchase_partial_quality_history_and_close(env):
    e,c=env;frozen(e);r=batches(e,c,purchase(e,c),8);r=b_action(e,c,r,'ship');r=b_action(e,c,r,'receive',g=3,q=4,d=1)
    line=r['lines'][0];receipt=batch(r)['receipts'][0]
    assert (line['received'],line['pending_quality'],line['became_available'],line['unbatched'])==(8,4,3,4)
    assert (balance(e)['g'],balance(e)['q'],balance(e)['d'])==(331,4,1)
    # Ordinary inspection cannot consume source-specific pending quality.
    rejected=send(c,'/quality',dict(warehouse_id='wh-cn',sku_id='sku-1',reference='no-source',good=1,bad=0,expected_version=2,reason='防止混用')).json()
    assert rejected['error']['code']=='QUALITY_RESERVED'
    inspection=req(c,f"supply-receipts/{receipt['id']}/inspect",dict(reference='source-inspection',quantity=4,expected_balance_version=2,details=meta()))
    inspection=qc.act(e,actor(e),inspection['id'],'complete',dict(expected_version=inspection['version'],good=3,bad=1,expected_balance_version=2,details=meta()),'m5-quality-complete')
    qc.act(e,actor(e),inspection['id'],'restock',dict(expected_version=inspection['version'],quantity=2,expected_balance_version=3),'m5-quality-restock')
    with e.connect() as conn:r=supply.view(conn,actor(e),'purchase',r['id'])
    assert (r['lines'][0]['pending_quality'],r['lines'][0]['became_available'])==(1,5)
    r=req(c,f"supply-orders/purchase/{r['id']}/close",dict(expected_version=r['version'],reference='close-left',reason='关闭未发计划'))
    assert r['status']=='closed' and r['lines'][0]['closed_quantity']==4 and r['lines'][0]['received']==8

def test_original_promise_and_unique_late_task_updates(env):
    e,c=env;frozen(e);r=purchase(e,c)
    original=r['terms']['original_due']
    r=req(c,f"purchase-plans/{r['id']}/promise",dict(expected_version=1,reference='promise-change',promised_at='2026-10-24T00:00:00Z',reason='模拟承诺变更'))
    assert r['terms']['original_due']==original and r['terms']['latest_due']!=original
    req(c,'purchase-plans/scan-delays');req(c,'purchase-plans/scan-delays')
    from app.exception_tasks import tasks
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(tasks).where(tasks.c.rule=='purchase_late'))==1
    r=batches(e,c,r,5);r=b_action(e,c,r,'ship');r=b_action(e,c,r,'receive',g=5)
    with e.connect() as conn:assert conn.scalar(select(tasks.c.impact_quantity).where(tasks.c.rule=='purchase_late'))==7

def test_purchase_closed_remaining_excluded_while_sent_batch_still_receivable(env):
    e,c=env;r=batches(e,c,purchase(e,c),5);r=b_action(e,c,r,'ship')
    r=req(c,f"supply-orders/purchase/{r['id']}/close",dict(expected_version=r['version'],reference='close-rest',reason='关闭未发部分'))
    assert r['lines'][0]['closed_quantity']==7 and r['lines'][0]['in_transit']==5
    r=b_action(e,c,r,'receive',g=5);assert r['status']=='closed'
    assert req(c,f"supply-orders/purchase/{r['id']}/batches",dict(expected_version=r['version'],reference='new-batch',reason='不能重开',supply_line_id=r['lines'][0]['id'],quantity=1),ok=409)['error']['code']=='OVER_BATCH'

def test_transport_two_batches_partial_loss_leaves_other_batch_open(env):
    e,c=env;r=transfer(e,c);r=batches(e,c,r,5);r=batches(e,c,r,7)
    assert balance(e)['t']==12 and balance(e)['g']==328
    r=b_action(e,c,r,'ship',index=0);r=b_action(e,c,r,'ship',index=1)
    assert balance(e)['t']==0 and balance(e)['g']==316
    r=b_action(e,c,r,'receive',index=0,g=3)
    # Separate requester and reviewer; grant destination access to warehouse user.
    dest=r['destination_id']
    with db.write(e) as conn:conn.execute(update(db.users).where(db.users.c.id=='warehouse').values(warehouse_ids=['wh-cn',dest]))
    login(c,'warehouse');b=batch(r,0)
    claim=req(c,f"supply-batches/{b['id']}/discrepancy",dict(expected_version=b['version'],reference='loss-batch-one',reason='核实短少',quantity=2,kind='shortage',details=meta()))
    login(c,'admin');req(c,f"supply-discrepancies/{claim['id']}/review",dict(expected_version=1,reference='approve-loss',reason='独立审核',decision='approve'))
    with e.connect() as conn:r=supply.view(conn,actor(e),'transfer',r['id'])
    assert batch(r,0)['in_transit']==0 and batch(r,1)['in_transit']==7 and r['status']=='partial'
    r=b_action(e,c,r,'receive',index=1,g=5,d=2)
    assert r['status']=='closed_difference' and r['lines'][0]['in_transit']==0

def test_cancel_releases_only_unshipped_batch(env):
    e,c=env;r=batches(e,c,transfer(e,c),6);r=batches(e,c,r,6)
    r=b_action(e,c,r,'ship',index=0);r=b_action(e,c,r,'cancel',index=1)
    assert balance(e)['t']==0 and balance(e)['g']==322 and r['lines'][0]['unbatched']==6
    assert b_action(e,c,r,'cancel',index=0,ok=409)['error']['code']=='INVALID_STATE'

def test_ship_cancel_competition_has_single_legal_effect(env):
    e,c=env;r=batches(e,c,transfer(e,c),4);b=batch(r);gate=Barrier(2);a=actor(e)
    def run(action):
        body=dict(expected_version=1,reference='race-'+action,reason='发出取消竞争',expected_balance_version=2)
        gate.wait()
        try:return supply.batch_action(e,a,b['id'],action,body,str(uuid4()))
        except BusinessError as exc:return exc.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['ship','cancel']))
    assert sum(isinstance(x,dict) for x in results)==1 and 'VERSION_CONFLICT' in results
    assert balance(e)['t']==0 and balance(e)['g'] in (328,324)

def test_two_level_receipt_dedup_and_content_conflict(env):
    e,c=env;r=b_action(e,c,batches(e,c,purchase(e,c),6),'ship');b=batch(r)
    body=dict(expected_version=b['version'],reference='unique-arrival',reason='同一到货',g=3,expected_balance_version=1)
    first=req(c,f"supply-batches/{b['id']}/receive",body,key='arrival-http-one');before=counts(e)
    assert req(c,f"supply-batches/{b['id']}/receive",body,key='arrival-http-one')['replayed']
    assert req(c,f"supply-batches/{b['id']}/receive",body,key='arrival-http-two')['business_replayed']
    assert req(c,f"supply-batches/{b['id']}/receive",body|{'g':4},ok=409)['error']['code']=='VOUCHER_CONFLICT'
    assert counts(e)['ledger']==before['ledger'] and first['lines'][0]['received']==3

def test_atomic_receipt_failure_rolls_back_inventory(env):
    e,c=env;r=b_action(e,c,batches(e,c,purchase(e,c),3),'ship');before=counts(e)
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO supply_receipts'):raise RuntimeError('crash before receipt evidence')
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError):b_action(e,c,r,'receive',g=3)
    finally:event.remove(e,'before_cursor_execute',crash)
    assert counts(e)==before and balance(e)['g']==328
    assert b_action(e,c,r,'receive',g=3)['lines'][0]['received']==3

def test_count_lock_blocks_reserved_ship_receive_and_quality(env):
    e,c=env;r=batches(e,c,transfer(e,c),2);p=b_action(e,c,batches(e,c,purchase(e,c),2),'ship');lock=started(c)
    assert b_action(e,c,r,'ship',ok=409)['error']['code']=='COUNT_LOCKED'
    assert b_action(e,c,p,'receive',g=2,ok=409)['error']['code']=='COUNT_LOCKED'
    count_act(c,lock,'cancel');assert b_action(e,c,r,'ship')['lines'][0]['sent']==2

def test_legacy_interfaces_cannot_bypass_supply_batches(env):
    e,c=env;r=purchase(e,c);t=transfer(e,c)
    assert send(c,f"/purchases/{r['id']}/receive",dict(reference='old-route',expected_version=1,lines=[dict(sku_id='sku-1',g=1,expected_version=1)])).json()['error']['code']=='MANAGED_SUPPLY'
    assert send(c,f"/purchases/{r['id']}/close",dict(expected_version=1,reason='旧关闭接口')).json()['error']['code']=='MANAGED_SUPPLY'
    assert send(c,f"/transfers/{t['id']}/reserve",dict(expected_version=1,reason='旧整体预占',lines=[dict(sku_id='sku-1',expected_version=1)])).json()['error']['code']=='MANAGED_SUPPLY'

def test_supply_pending_quality_cancel_returns_to_source_hold(env):
    e,c=env;r=b_action(e,c,batches(e,c,purchase(e,c),3),'ship');r=b_action(e,c,r,'receive',q=3);receipt=batch(r)['receipts'][0]
    q=req(c,f"supply-receipts/{receipt['id']}/inspect",dict(reference='inspect-first',quantity=2,expected_balance_version=2,details=meta()))
    qc.act(e,actor(e),q['id'],'cancel',dict(expected_version=q['version']),'cancel-quality')
    with e.connect() as conn:assert supply.pending_inspection(conn,receipt)==3 and qc.protected(conn,actor(e)['tenant_id'],'wh-cn','sku-1')[0]==3

def test_planning_hand_calculated_forecast_rounding_and_confirmation(env):
    e,c=env;p=rule(e,c);plan=calculate(c,p)
    # 300 / 3 * (5+2) + safety 10 - stock 328 = 382 -> 384 in packs of 6.
    assert plan['status']=='ready' and plan['suggested']==384
    assert plan['snapshot']['calculation']['eligible_supply']==0
    before=balance(e);result=req(c,f"planning/plans/{plan['id']}/confirm",dict(quantity=390,reason='人工调整采购包装'))
    assert result['order_id'] and result['quantity']==390 and balance(e)==before
    second=req(c,f"planning/plans/{plan['id']}/confirm",dict(quantity=390,reason='人工调整采购包装'))
    assert second['business_replayed'] and second['order_id']==result['order_id']
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(w.orders))==1

@pytest.mark.parametrize('mode',['missing','abnormal','unknown_inventory','outlier'])
def test_unknown_data_never_generates_zero_or_confirmable_plan(env,mode):
    e,c=env;p=rule(e,c,history=False,max_daily=50 if mode=='outlier' else 1000000)
    dates=['2026-10-22','2026-10-23'] if mode=='missing' else ['2026-10-22','2026-10-23','2026-10-24']
    for date in dates:req(c,'planning/demand',dict(warehouse_id='wh-cn',sku_id='sku-1',day=date,quantity=100,quality='abnormal' if mode=='abnormal' else 'known',source_reference=date))
    if mode=='unknown_inventory':
        with db.write(e) as conn:conn.execute(update(catalog.assortments).where(catalog.assortments.c.warehouse_id=='wh-cn',catalog.assortments.c.sku_id=='sku-1').values(inventory_known=0))
    plan=calculate(c,p);assert plan['status']=='unknown' and plan['suggested'] is None
    assert req(c,f"planning/plans/{plan['id']}/confirm",dict(quantity=0,reason='不能假定为零'),ok=409)['error']['code']=='PLAN_UNKNOWN'

def test_zero_history_is_known_and_safety_not_physical_hold(env):
    e,c=env;p=rule(e,c,quantity=0,safety=350);plan=calculate(c,p)
    assert plan['status']=='ready' and plan['suggested']==24 and balance(e)['b']==0

@pytest.mark.parametrize('includes,expected',[ (True,384),(False,414)])
def test_open_order_reservations_never_double_subtracted(env,includes,expected):
    e,c=env;p=rule(e,c,includes_open_orders=includes);reserved(e,c,30);plan=calculate(c,p)
    assert plan['suggested']==expected
    assert plan['snapshot']['calculation']['order_reserved_already_deducted']==30

def test_supply_switches_from_plan_to_intransit_to_stock_once(env):
    e,c=env;p=rule(e,c);r=purchase(e,c,n=12)
    initial=calculate(c,p);assert initial['snapshot']['calculation']['eligible_supply']==12
    r=batches(e,c,r,7);batched=calculate(c,p);assert batched['snapshot']['calculation']['eligible_supply']==12
    r=b_action(e,c,r,'ship');sent=calculate(c,p);assert sent['snapshot']['calculation']['eligible_supply']==12
    r=b_action(e,c,r,'receive',g=7);received=calculate(c,p)
    assert received['snapshot']['calculation']['eligible_supply']==5 and received['suggested']==initial['suggested']
    assert {x['phase'] for x in sent['snapshot']['inputs']['supplies']}=={'unbatched','in_transit'}

def test_overdue_unknown_and_outside_horizon_supply_is_risk(env):
    e,c=env;p=rule(e,c);r=purchase(e,c,n=30)
    r=req(c,f"purchase-plans/{r['id']}/promise",dict(expected_version=1,reference='past-promise',promised_at='2026-10-20T00:00:00Z',reason='已逾期供给'))
    plan=calculate(c,p);assert plan['snapshot']['calculation']['eligible_supply']==0 and plan['snapshot']['calculation']['risky_supply']==30

@pytest.mark.parametrize('change',['stock','policy','supply','clock'])
def test_confirmation_rechecks_inputs(env,change):
    e,c=env;p=rule(e,c);plan=calculate(c,p)
    if change=='stock':
        from app.domain import stock_action
        stock_action(e,actor(e),'receipt',dict(warehouse_id='wh-cn',sku_id='sku-1',source='模拟收货',g=1,q=0,d=0,expected_version=1),'changed-stock')
    elif change=='policy':
        body={k:v for k,v in p.items() if k not in ('id','tenant_id','version','created_at','replayed')};body.update(expected_version=1,safety=20,includes_open_orders=True,active=True);req(c,'planning/policies',body)
    elif change=='supply':purchase(e,c)
    else:e.business_clock.advance(seconds=86400)
    result=req(c,f"planning/plans/{plan['id']}/confirm",dict(quantity=384,reason='旧方案不应执行'),ok=409)
    assert result['error']['code'] in ('PLAN_STALE','PLAN_UNKNOWN')

def test_plan_wrong_creator_revoke_and_snapshot_visibility(env):
    e,c=env;p=rule(e,c);reserved(e,c,5);plan=calculate(c,p)
    login(c,'warehouse');assert req(c,f"planning/plans/{plan['id']}/confirm",dict(quantity=384,reason='其他人不能确认'),ok=403)['error']['code']=='PLAN_CREATOR_ONLY'
    from app.m4_schema import connections
    with db.write(e) as conn:conn.execute(update(connections).values(member_ids=[]))
    assert c.get('/api/v2/planning/plans').json()['items']==[]

def test_source_specific_history_is_immutable(env):
    e,c=env;p=rule(e,c);plan=calculate(c,p)
    for table in (s.plans,s.demand,s.history):
        with pytest.raises(DBAPIError):
            with db.write(e) as conn:conn.execute(update(table).values(tenant_id='changed'))

@pytest.mark.parametrize('quantity',[True,1.5,-1,1000001])
def test_strict_quantity_and_csrf(env,quantity):
    e,c=env;body=dict(code='invalid',warehouse_id='wh-cn',supplier_id=vendor(e)['id'],lines=[dict(sku_id='sku-1',quantity=quantity)])
    assert req(c,'purchase-plans',body,ok=422)['detail']

def test_cross_warehouse_request_replay_and_cashier_denied(env):
    e,c=env;p=rule(e,c);login(c,'warehouse');plan=req(c,f"planning/policies/{p['id']}/calculate",key='scope-plan-key')
    with db.write(e) as conn:conn.execute(update(db.users).where(db.users.c.id=='warehouse').values(warehouse_ids=['wh-us']))
    assert req(c,f"planning/policies/{p['id']}/calculate",key='scope-plan-key',ok=404)['error']['code']=='NOT_FOUND'
    login(c,'cashier');assert c.get('/api/v2/planning/policies').status_code==403


def test_supplier_return_preserves_received_history_and_no_replacement_supply(env):
    e,c=env;r=b_action(e,c,batches(e,c,purchase(e,c,n=3),3),'ship');r=b_action(e,c,r,'receive',g=3)
    receipt=batch(r)['receipts'][0];login(c,'warehouse')
    request=send(c,'/returns',dict(kind='supplier',original_id=receipt['document_id'],reference='source-return',g=2,q=0,d=0,
        expected_version=balance(e)['version'],reason='退回原供应商')).json()
    assert request['status']=='awaiting_review';login(c,'admin')
    req(c,f"quality-dispositions/{request['id']}/review",dict(expected_version=1,decision='approve',reason='独立核对退货凭证'))
    with e.connect() as conn:r=supply.view(conn,actor(e),'purchase',r['id'])
    assert r['status']=='completed' and r['lines'][0]['received']==3 and r['lines'][0]['unbatched']==0
    assert batch(r)['receipts'][0]['returned']==2 and balance(e)['g']==329
    with e.connect() as conn:returned=conn.scalar(select(op.returns.c.id).where(op.returns.c.original_id==receipt['document_id']))
    replacement=purchase(e,c,n=2,replacement_return_id=returned)
    assert replacement['terms']['replacement_return_id']==returned and replacement['lines'][0]['received']==0
    body=dict(code='over-replacement',warehouse_id='wh-cn',supplier_id=vendor(e)['id'],replacement_return_id=returned,lines=[dict(sku_id='sku-1',quantity=1)])
    assert req(c,'purchase-plans',body,ok=409)['error']['code']=='OVER_REPLACEMENT'
    assert req(c,'purchase-plans',body|{'lines':[dict(sku_id='sku-2',quantity=1)]},ok=422)['error']['code']=='REPLACEMENT_MISMATCH'
    assert req(c,'purchase-plans',body|{'replacement_return_id':'foreign-return'},ok=404)['error']['code']=='NOT_FOUND'


def test_purchase_close_protects_explicit_sales_dependency(env):
    e,c=env;r=purchase(e,c,n=12);sale=reserved(e,c,6)
    sale=sale_act(c,sale,'link',line_id=sale['lines'][0]['id'],source_type='purchase_line',source_id=r['lines'][0]['source_line_id'],quantity=6)
    body=dict(expected_version=r['version'],reference='protected-close',reason='先核实关联需求')
    assert req(c,f"supply-orders/purchase/{r['id']}/close",body,ok=409)['error']['code']=='SUPPLY_LINKED'
    sale_act(c,sale,'release',link_id=sale['supply_links'][0]['id'],quantity=6)
    assert req(c,f"supply-orders/purchase/{r['id']}/close",body)['status']=='closed'


def test_claim_self_review_and_stale_batch_require_recheck(env):
    e,c=env;r=b_action(e,c,batches(e,c,transfer(e,c,n=4),4),'ship');b=batch(r)
    claim=req(c,f"supply-batches/{b['id']}/discrepancy",dict(expected_version=b['version'],reference='stale-loss',reason='到货差异核对',quantity=2,kind='shortage',details=meta()))
    review=dict(expected_version=1,reference='review-loss',reason='复核运输差异',decision='approve')
    assert req(c,f"supply-discrepancies/{claim['id']}/review",review,ok=403)['error']['code']=='SELF_APPROVAL'
    r=b_action(e,c,r,'receive',g=1)
    with db.write(e) as conn:conn.execute(update(db.users).where(db.users.c.id=='warehouse').values(role='admin'))
    login(c,'warehouse')
    assert req(c,f"supply-discrepancies/{claim['id']}/review",review,ok=409)['error']['code']=='VERSION_CONFLICT'
    assert req(c,f"supply-discrepancies/{claim['id']}/review",review|{'decision':'reject'})['status']=='rejected'
    with e.connect() as conn:assert supply.get_batch(conn,actor(e),b['id'])['lost']==0


@pytest.mark.parametrize('field',['sku','assortment'])
def test_disabled_catalog_blocks_new_transport_reservation(env,field):
    e,c=env;r=transfer(e,c)
    with db.write(e) as conn:
        table=catalog.sku_settings if field=='sku' else catalog.assortments
        if field=='sku':conn.execute(insert(table).values(sku_id='sku-1',tenant_id=actor(e)['tenant_id'],active=0,version=1))
        else:conn.execute(update(table).where(table.c.sku_id=='sku-1').values(active=0))
    body=dict(expected_version=1,reference='disabled-reserve',reason='停用后新增预占',supply_line_id=r['lines'][0]['id'],quantity=1,expected_balance_version=1)
    assert req(c,f"supply-orders/transfer/{r['id']}/batches",body,ok=409)['error']['code'] in ('SKU_INACTIVE','ASSORTMENT_INACTIVE')
    assert balance(e)['t']==0


def test_global_batch_reference_conflict_is_business_error(env):
    e,c=env;r=purchase(e,c);other=purchase(e,c)
    body=dict(expected_version=1,reference='shared-transport',reason='独立批次编号',supply_line_id=r['lines'][0]['id'],quantity=1)
    req(c,f"supply-orders/purchase/{r['id']}/batches",body)
    assert req(c,f"supply-orders/purchase/{other['id']}/batches",body|{'supply_line_id':other['lines'][0]['id']},ok=409)['error']['code']=='BATCH_REFERENCE_USED'


def test_confirmation_constraint_expiry_and_rollback(env,monkeypatch):
    e,c=env;p=rule(e,c);plan=calculate(c,p);path=f"planning/plans/{plan['id']}/confirm"
    for n in (6,383):assert req(c,path,dict(quantity=n,reason='数量需满足约束'),ok=422)['error']['code']=='PURCHASE_CONSTRAINT'
    before=counts(e)
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO replenishment_decisions'):raise RuntimeError('crash after purchase before decision')
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError):req(c,path,dict(quantity=384,reason='事务回滚校验'))
    finally:event.remove(e,'before_cursor_execute',crash)
    assert counts(e)==before
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(w.orders))==0
    from datetime import datetime,timedelta,timezone
    class Expired:
        @staticmethod
        def now(zone):return datetime.now(timezone.utc)+timedelta(minutes=31)
    monkeypatch.setattr(planning,'datetime',Expired)
    assert req(c,path,dict(quantity=384,reason='过期需要重算'),ok=409)['error']['code']=='PLAN_EXPIRED'


def test_parallel_confirmation_creates_one_purchase(env):
    e,c=env;plan=calculate(c,rule(e,c));gate=Barrier(2);a=actor(e)
    def confirm(_):
        gate.wait();return planning.confirm(e,a,plan['id'],dict(quantity=384,reason='并发确认同一方案'),str(uuid4()))
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(confirm,range(2)))
    assert results[0]['order_id']==results[1]['order_id']
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(w.orders))==1
