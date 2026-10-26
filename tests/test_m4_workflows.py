"""M4 quantities, races, authorization and reliable simulated message effects."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import select, update, insert, func, event
from app import db, m4_schema as s, fulfillment as f, connectors as cn, delivery as dl, reconciliation as rc, commitments, inventory as inv
from app.domain import BusinessError
from test_inventory import env, actor, balance, counts
from test_m3_workflows import req, started, act as count_act
from test_phase2 import login


def connection(e, c, **kw):
    return req(c, 'connections', dict(source_account='sim-channel', name='模拟渠道', warehouse_ids=['wh-cn'], member_ids=[actor(e, 'warehouse')['id']], **kw))


def order(e, c, account=None, quantity=10, **kw):
    account = account or connection(e, c)
    return req(c, 'orders', dict(connection_id=account['id'], source_order_id=str(uuid4()), warehouse_ids=['wh-cn'],
        lines=[dict(source_line_id='line-1', sku_id='sku-1', quantity=quantity)], **kw))


def act(c, row, action, **kw):
    ok = kw.pop('ok', 200); key = kw.pop('key', None)
    return req(c, f"orders/{row['id']}/{action}", dict(expected_version=row['version'], action_id=str(uuid4()), reason='模拟业务核对', **kw), ok=ok, key=key)


def reserved(e, c, quantity=10):
    r = act(c, order(e, c, quantity=quantity), 'confirm')
    return act(c, r, 'allocate', line_id=r['lines'][0]['id'], quantity=quantity)


def ready(c, row):
    b = row['batches'][0]['id']
    for kind in ('pick', 'review'):
        for action in ('start', 'complete'): row = act(c, row, action, batch_id=b, kind=kind)
    return row


def ship(c, r, quantity=3, **kw):
    return act(c, r, 'ship', batch_id=r['batches'][0]['id'], quantity=quantity, reference=kw.pop('reference', str(uuid4())), occurred_at='2026-10-24T02:00:00Z', **kw)


def envelope(e, r, kind='order.completed', version=1, payload=None, **kw):
    return dict(run_id=e.runtime_settings.run_id, tenant_id=actor(e)['tenant_id'], source_event_id=str(uuid4()),
        business_action_id=str(uuid4()), entity_id=r['source_order_id'], source_version=version, kind=kind,
        occurred_at='2026-10-24T04:00:00Z', payload=payload or {}, **kw)


def test_order_partial_ship_cancel_and_return(env):
    e,c = env; r = ready(c, reserved(e,c)); r = ship(c,r,4)
    assert balance(e)['g'] == 324 and balance(e)['r'] == 6
    r = act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=6)
    assert r['status'] == 'shipped' and balance(e)['r'] == 0 and balance(e)['g'] == 324
    assert act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=1,ok=409)['error']['code'] == 'OVER_CANCELLATION'
    with e.connect() as conn:
        from app.workflows import original
        with pytest.raises(BusinessError,match='退货'): original(conn, actor(e), r['shipments'][0]['document_id'])
    from app import operations
    ret = operations.return_create(e,actor(e),dict(kind='customer',original_id=r['shipments'][0]['document_id'],
        reference='m4-return',quantity=2,expected_version=balance(e)['version'],reason='模拟客户实物退回'),'m4-return-key')
    assert ret['quantity'] == 2 and balance(e)['q'] == 2


def test_two_level_idempotency_and_changed_content(env):
    e,c = env; r = reserved(e,c); b = dict(expected_version=r['version'],action_id='same-cancel',reason='取消未发数量',line_id=r['lines'][0]['id'],quantity=2)
    path = f"orders/{r['id']}/cancel"
    first = req(c,path,b,key='same-request-key'); before = counts(e)
    assert req(c,path,b,key='same-request-key')['replayed']
    assert req(c,path,b,key='another-request-key')['business_replayed']
    assert req(c,path,{**b,'quantity':3},ok=409)['error']['code'] == 'BUSINESS_ACTION_CONFLICT'
    assert counts(e)['ledger'] == before['ledger'] and first['lines'][0]['cancelled'] == 2


def test_cancel_ship_race_has_one_legal_outcome(env):
    e,c=env; r=ready(c,reserved(e,c,1)); a=actor(e); gate=Barrier(2)
    base=dict(expected_version=r['version'],reason='竞争测试',quantity=1)
    def run(action):
        body={**base,'action_id':action}
        body.update(dict(line_id=r['lines'][0]['id']) if action=='cancel' else dict(batch_id=r['batches'][0]['id'],reference='race-shipment'))
        gate.wait()
        try:return f.act(e,a,r['id'],action,body,str(uuid4()))
        except BusinessError as ex:return ex.code
    with ThreadPoolExecutor(2) as pool: result=list(pool.map(run,('cancel','ship')))
    assert sum(isinstance(x,dict) for x in result)==1 and 'VERSION_CONFLICT' in result
    assert balance(e)['r']==0 and balance(e)['g'] in (327,328)


def test_count_lock_and_no_bypass_rollback(env):
    e,c=env; r=ready(c,reserved(e,c)); lock=started(c); before=counts(e)
    assert ship(c,r,ok=409)['error']['code']=='COUNT_LOCKED'
    assert act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=1,ok=409)['error']['code']=='COUNT_LOCKED'
    assert counts(e)==before and balance(e)['r']==10
    count_act(c,lock,'cancel'); assert ship(c,r)['status']=='partially_shipped'


def test_allocation_shortage_task_and_unknown_not_zero(env):
    e,c=env; r=act(c,order(e,c,quantity=500),'confirm')
    r=act(c,r,'allocate',line_id=r['lines'][0]['id'],quantity=500)
    assert r['allocation_result']=='shortage' and balance(e)['r']==0
    with e.connect() as conn: assert conn.scalar(select(func.count()).select_from(s.batches))==0
    from app.catalog import assortments
    with db.write(e) as conn: conn.execute(update(assortments).where(assortments.c.warehouse_id=='wh-cn',assortments.c.sku_id=='sku-1').values(inventory_known=0))
    assert act(c,r,'allocate',line_id=r['lines'][0]['id'],quantity=1)['allocation_result']=='unknown'


def test_grant_conversion_cancel_and_legacy_guard(env):
    e,c=env; account=connection(e,c); a=actor(e)
    grant=commitments.authorize(e,a,dict(warehouse_id='wh-cn',sku_id='sku-1',quantity=100,source_account='sim-channel',reference='grant',action_id='authorize',restore_on_cancel=True,expected_version=1),'grant-create')['grant']
    r=act(c,order(e,c,account,30),'confirm'); r=act(c,r,'allocate',line_id=r['lines'][0]['id'],quantity=30,warehouse_id='wh-cn',grant_id=grant['id'])
    assert (balance(e)['online'],balance(e)['r'])==(70,30)
    with pytest.raises(BusinessError) as error:
        commitments.cancel(e,a,r['batches'][0]['commitment_id'],dict(quantity=1,action_id='legacy-cancel',reason='不能旁路',expected_version=3,grant_version=2,reservation_version=1),'legacy-cancel-key')
    assert error.value.code=='MANAGED_COMMITMENT'
    r=ready(c,r); r=ship(c,r,10); r=act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=20)
    assert (balance(e)['g'],balance(e)['online'],balance(e)['r'])==(318,90,0)


def test_roles_account_members_and_permission_change_on_replay(env):
    e,c=env; r=reserved(e,c); login(c,'warehouse')
    body=dict(expected_version=r['version'],action_id='scope-cancel',reason='撤权重试',line_id=r['lines'][0]['id'],quantity=1)
    path=f"orders/{r['id']}/cancel"; req(c,path,body,key='scope-cancel-key')
    with db.write(e) as conn:conn.execute(update(s.connections).values(member_ids=[]))
    assert req(c,path,body,key='scope-cancel-key',ok=404)['error']['code']=='NOT_FOUND'
    assert c.get('/api/v2/orders').json()['items']==[]
    login(c,'cashier'); assert c.get('/api/v2/orders').status_code==403


def test_ship_atomic_rollback_before_outbox_commit(env):
    e,c=env; r=ready(c,reserved(e,c)); before=counts(e)
    def crash(conn,cursor,statement,params,context,many):
        if statement.startswith('INSERT INTO connector_outbox'): raise RuntimeError('before outbox commit')
    event.listen(e,'before_cursor_execute',crash)
    try:
        with pytest.raises(RuntimeError):ship(c,r)
    finally:event.remove(e,'before_cursor_execute',crash)
    assert counts(e)==before and balance(e)['g']==328 and balance(e)['r']==10
    assert ship(c,r)['status']=='partially_shipped'


def test_inbox_dedup_conflict_unknown_verify_and_stale(env):
    e,c=env; account=connection(e,c); payload=dict(connection_id=account['id'],source_order_id='source-order',warehouse_ids=['wh-cn'],lines=[dict(source_line_id='1',sku_id='sku-1',quantity=4)])
    b=envelope(e,{'source_order_id':'source-order'},'order.created',None,payload)
    a=actor(e); first=cn.receive(e,a,account['id'],b)
    assert cn.process(e,a,first['id'])['status']=='unknown'
    cn.source_fixture(e,a,account['id'],{**b,'source_version':1})
    assert cn.process(e,a,first['id'],verify=True)['status']=='processed'
    assert cn.receive(e,a,account['id'],b)['replayed']
    duplicate=cn.receive(e,a,account['id'],{**b,'source_event_id':'redelivered','source_version':1})
    assert cn.process(e,a,duplicate['id'])['status']=='duplicate'
    conflict=cn.receive(e,a,account['id'],{**b,'payload':{**payload,'planned_at':'2026-11-01T00:00:00Z'}})
    assert conflict['status']=='conflict'
    assert len(f.listing(e,a)['items'])==1


def test_inbox_cross_run_and_tenant_rejected(env):
    e,c=env; r=order(e,c); b=envelope(e,r)
    for changed in ({'run_id':'other-run'},{'tenant_id':'other-tenant'}):
        with pytest.raises(BusinessError) as error: cn.receive(e,actor(e),r['connection_id'],{**b,**changed})
        assert error.value.code=='RUNTIME_MISMATCH'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.inbox))==0


def test_channel_completion_cannot_ship_and_delivery_is_separate(env):
    e,c=env; r=ready(c,reserved(e,c,2)); a=actor(e)
    message=cn.receive(e,a,r['connection_id'],envelope(e,r))
    assert cn.process(e,a,message['id'])['error']=='PHYSICAL_SHIPMENT_REQUIRED'
    r=ship(c,r,2); message=cn.process(e,a,message['id'])
    assert message['status']=='processed'
    with e.connect() as conn:
        r=f.view(conn,a,r['id']); assert r['status']=='completed' and r['shipments'][0]['delivered_at'] is None
    delivered=cn.receive(e,a,r['connection_id'],{**envelope(e,r,'shipment.delivered'), 'entity_id':r['shipments'][0]['id']})
    assert cn.process(e,a,delivered['id'])['status']=='processed' and balance(e)['g']==326


def test_timeout_after_external_success_queries_same_number(env):
    e,c=env; r=ship(c,ready(c,reserved(e,c))); a=actor(e); clock=[1000.0]
    result=dl.tick(e,a,adapter=dl.SimulatedAdapter(e,a,['timeout_after']),clock=lambda:clock[0])
    assert result['status']=='unknown' and result['id']==r['outbox_id']
    clock[0]+=10; result=dl.tick(e,a,clock=lambda:clock[0]); assert result['status']=='succeeded'
    with e.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(s.effects))==1
        assert list(conn.execute(select(s.attempts.c.payload).where(s.attempts.c.action=='claimed')).scalars())[-1]['mode']=='query'
    assert balance(e)['g']==325


def test_expired_lease_fenced_and_crash_after_success_recovered(env):
    e,c=env; ship(c,ready(c,reserved(e,c))); a=actor(e); clock=[1000.0]
    first=dl.claim(e,a,clock=lambda:clock[0],lease_seconds=1)
    dl.SimulatedAdapter(e,a).execute(first); clock[0]+=2
    second=dl.claim(e,a,clock=lambda:clock[0]); assert second['lease_mode']=='query'
    assert dl.finish(e,a,first,{'status':'succeeded'},clock=lambda:clock[0])['status']=='lease_lost'
    assert dl.finish(e,a,second,dl.SimulatedAdapter(e,a).query(second),clock=lambda:clock[0])['status']=='succeeded'


def test_outbox_resource_order_dead_letter_and_manual_retry(env):
    e,c=env; r=ship(c,ready(c,reserved(e,c)),2); r=ship(c,r,2); a=actor(e); clock=[1000.0]
    first=dl.tick(e,a,adapter=dl.SimulatedAdapter(e,a,['failure']),clock=lambda:clock[0]); assert first['status']=='dead'
    assert dl.tick(e,a,clock=lambda:clock[0])['status']=='idle'
    dl.retry(e,a,first['id'],{'reason':'核实来源失败后重试原动作'},'manual-retry')
    assert dl.tick(e,a,clock=lambda:clock[0])['status']=='retry'
    clock[0]+=20; assert dl.tick(e,a,clock=lambda:clock[0])['status']=='succeeded'
    assert dl.tick(e,a,clock=lambda:clock[0])['status']=='succeeded'
    with e.connect() as conn: assert conn.scalar(select(func.count()).select_from(s.effects))==2


def test_unknown_without_query_stops_automatic_execution(env):
    e,c=env; r=ship(c,ready(c,reserved(e,c))); a=actor(e)
    with db.write(e) as conn:conn.execute(update(s.connections).values(supports_query=0))
    assert dl.tick(e,a,adapter=dl.SimulatedAdapter(e,a,['timeout_after']))['status']=='manual'
    assert dl.tick(e,a)['status']=='idle'


@pytest.mark.parametrize('order_of_arrival',['snapshot_first','delta_first','no_watermark'])
def test_snapshot_watermark_never_double_posts(env,order_of_arrival):
    e,c=env; account=connection(e,c); a=actor(e)
    snap=dict(snapshot_id='snapshot',warehouse_id='wh-cn',sku_id='sku-1',covered_watermark=None if order_of_arrival=='no_watermark' else 10,
        snapshot_at='2026-10-24T00:00:00Z',quantities={'g':328,'q':0,'d':0})
    def delta():
        event_body=envelope(e,{'source_order_id':'stock:wh-cn:sku-1'},'stock.delta',9,dict(warehouse_id='wh-cn',sku_id='sku-1',sequence=9,g=-5))
        row=cn.receive(e,a,account['id'],event_body); assert cn.process(e,a,row['id'])['status']=='processed'
    if order_of_arrival=='delta_first': delta()
    row=rc.snapshot(e,a,account['id'],snap,'snapshot-create')
    if order_of_arrival!='delta_first':delta()
    before=counts(e); result=rc.compare(e,a,row['id'],'compare-key')
    assert result['status']==('unknown' if order_of_arrival=='no_watermark' else 'matched')
    assert result['inputs']['external']==(None if order_of_arrival=='no_watermark' else {'g':328,'q':0,'d':0})
    assert balance(e)['g']==328 and counts(e)['ledger']==before['ledger']


def test_uncovered_delta_and_gap_preserved(env):
    e,c=env; account=connection(e,c); a=actor(e)
    snap=rc.snapshot(e,a,account['id'],dict(snapshot_id='s',warehouse_id='wh-cn',sku_id='sku-1',covered_watermark=10,
        snapshot_at='2026-10-24T00:00:00Z',quantities={'g':330,'q':0,'d':0}),'snapshot-key')
    def delta(seq,n):
        b=envelope(e,{'source_order_id':'stock:wh-cn:sku-1'},'stock.delta',seq,dict(warehouse_id='wh-cn',sku_id='sku-1',sequence=seq,g=n))
        row=cn.receive(e,a,account['id'],b); assert cn.process(e,a,row['id'])['status']=='processed'
    delta(12,-1); assert rc.compare(e,a,snap['id'],'gap-compare')['status']=='unknown'
    delta(11,-1); assert rc.compare(e,a,snap['id'],'covered-compare')['status']=='matched'
    assert balance(e)['g']==328


def test_shipment_voucher_dedup_with_new_action(env):
    e,c=env; r=ready(c,reserved(e,c)); first=ship(c,r,2,reference='same-handover')
    again=ship(c,r,2,reference='same-handover')
    assert again['business_replayed'] and first['document_id']==again['document_id']
    assert ship(c,r,3,reference='same-handover',ok=409)['error']['code']=='SHIPMENT_REFERENCE_CONFLICT'
    assert balance(e)['g']==326


def test_operations_can_plan_analyst_read_only_and_csrf(env):
    e,c=env
    from app import accounts
    for role in ('operations','analyst'):
        accounts.create_user(e,actor(e),dict(username=role,name=role,role=role,warehouse_ids=['wh-cn'],password='test-password'),role+'-account')
    account=connection(e,c)
    cn.edit(e,actor(e),account['id'],dict(expected_version=1,active=True,member_ids=[actor(e,x)['id'] for x in ('operations','analyst')]),'add-members')
    login(c,'operations'); r=order(e,c,account); r=act(c,r,'confirm'); r=act(c,r,'allocate',line_id=r['lines'][0]['id'],quantity=5)
    assert act(c,r,'start',batch_id=r['batches'][0]['id'],kind='pick',ok=403)['error']['code']=='FORBIDDEN'
    login(c,'analyst'); assert c.get('/api/v2/orders').json()['total']==1
    assert act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=1,ok=403)['error']['code']=='FORBIDDEN'
    login(c,'admin')
    response=c.post('/api/v2/orders/'+r['id']+'/confirm',json=dict(expected_version=r['version'],action_id='csrf',reason='csrf测试'),headers={'X-CSRF-Token':'','Idempotency-Key':'csrf-test-key'})
    assert response.status_code==403


def test_fixed_warehouse_order_splits_and_cancel_is_atomic(env):
    e,c=env; a=actor(e)
    from app.workflows import catalog_create
    catalog_create(e,a,'warehouses',dict(code='LOCAL2',name='第二本地仓',country='CN',timezone='Asia/Shanghai',authority='local'),'local-wh-2')
    with e.connect() as conn:w2=conn.scalar(select(db.warehouses.c.id).where(db.warehouses.c.code=='LOCAL2'))
    with db.write(e) as conn:
        stock=inv.load_balance(conn,a,w2,'sku-1');inv.movement(conn,a,stock,{'g':10},'opening','test','独立期初',0)
    account=cn.create(e,a,dict(source_account='split',name='分仓渠道',warehouse_ids=[w2,'wh-cn']),'split-account')
    r=f.create(e,a,dict(connection_id=account['id'],source_order_id='split-order',warehouse_ids=[w2,'wh-cn'],lines=[dict(source_line_id='1',sku_id='sku-1',quantity=15)]),'split-order-create')
    r=act(c,r,'confirm');r=act(c,r,'allocate',line_id=r['lines'][0]['id'],quantity=15)
    assert {x['warehouse_id']:x['reserved'] for x in r['batches']}=={w2:10,'wh-cn':5}
    lock=started(c);before=counts(e)
    assert act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=15,ok=409)['error']['code']=='COUNT_LOCKED'
    with e.connect() as conn:assert inv.load_balance(conn,a,w2,'sku-1')['r']==10
    assert counts(e)==before
    count_act(c,lock,'cancel');r=act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=15)
    assert r['status']=='cancelled'


def test_source_cancel_before_creation_and_version_gap(env):
    e,c=env; account=connection(e,c); a=actor(e)
    cancellation={**envelope(e,{'source_order_id':'late-order'},'order.cancelled',2,dict(source_line_id='1',quantity=2,reason='来源取消请求')),'warehouse_id':'wh-cn','sku_id':'sku-1'}
    pending=cn.receive(e,a,account['id'],cancellation)
    assert cn.process(e,a,pending['id'])['error']=='SOURCE_VERSION_GAP'
    body=dict(connection_id=account['id'],source_order_id='late-order',warehouse_ids=['wh-cn'],lines=[dict(source_line_id='1',sku_id='sku-1',quantity=3)])
    first=cn.receive(e,a,account['id'],envelope(e,{'source_order_id':'late-order'},'order.created',1,body))
    assert cn.process(e,a,first['id'])['status']=='processed'
    assert cn.process(e,a,pending['id'])['status']=='processed'
    old=cn.receive(e,a,account['id'],{**cancellation,'source_event_id':'old-cancel','business_action_id':'old-action','source_version':1})
    assert cn.process(e,a,old['id'])['status']=='stale'
    assert f.listing(e,a)['items'][0]['lines'][0]['cancelled']==2


def test_changed_business_action_isolated_and_no_second_effect(env):
    e,c=env; account=connection(e,c);a=actor(e)
    def b(n):return envelope(e,{'source_order_id':'stock:wh-cn:sku-1'},'stock.delta',1,dict(warehouse_id='wh-cn',sku_id='sku-1',sequence=1,g=n))
    original=b(-1);message=cn.receive(e,a,account['id'],original);assert cn.process(e,a,message['id'])['status']=='processed'
    changed=cn.receive(e,a,account['id'],{**b(-2),'business_action_id':original['business_action_id']})
    assert cn.process(e,a,changed['id'])['status']=='conflict'
    with e.connect() as conn:assert conn.scalar(select(func.count()).select_from(s.deltas))==1
    assert balance(e)['g']==328


def test_rate_limit_backoff_exhaustion_and_other_resources_continue(env):
    e,c=env;r=ship(c,ready(c,reserved(e,c)));a=actor(e);wall=[1000.0]
    second=order(e,c,{'id':r['connection_id']});second=act(c,second,'confirm');second=act(c,second,'allocate',line_id=second['lines'][0]['id'],quantity=2);ship(c,ready(c,second),1)
    adapter=dl.SimulatedAdapter(e,a,['rate_limit']*5)
    first=dl.tick(e,a,adapter=adapter,clock=lambda:wall[0]);assert first['status']=='retry'
    claimed=dl.claim(e,a,clock=lambda:wall[0]);assert claimed and claimed['id']!=first['id']
    dl.finish(e,a,claimed,dl.SimulatedAdapter(e,a).execute(claimed),clock=lambda:wall[0])
    for _ in range(4):wall[0]+=400;first=dl.tick(e,a,adapter=adapter,clock=lambda:wall[0])
    assert first['status']=='dead' and first['attempts']==5


def test_lease_before_execute_queries_then_reuses_original_id(env):
    e,c=env;ship(c,ready(c,reserved(e,c)));a=actor(e);wall=[1000.0]
    first=dl.claim(e,a,clock=lambda:wall[0],lease_seconds=1);wall[0]+=2
    result=dl.tick(e,a,clock=lambda:wall[0]);assert result['id']==first['id'] and result['status']=='retry'
    wall[0]+=400;result=dl.tick(e,a,clock=lambda:wall[0]);assert result['status']=='succeeded'
    with e.connect() as conn:assert conn.scalar(select(s.effects.c.business_number))==first['id']


def test_two_workers_claim_once(env):
    e,c=env;ship(c,ready(c,reserved(e,c)));gate=Barrier(2);a=actor(e)
    def claim(_):gate.wait();return dl.claim(e,a)
    with ThreadPoolExecutor(2) as pool:rows=list(pool.map(claim,(1,2)))
    assert sum(x is not None for x in rows)==1


def test_reconciliation_task_closing_never_changes_stock_and_revoked_scope(env):
    e,c=env;account=connection(e,c);a=actor(e)
    snap=rc.snapshot(e,a,account['id'],dict(snapshot_id='mismatch',warehouse_id='wh-cn',sku_id='sku-1',covered_watermark=1,
        snapshot_at='2026-10-24T00:00:00Z',quantities={'g':327,'q':0,'d':0}),'mismatch-snapshot')
    rc.compare(e,a,snap['id'],'mismatch-compare')
    from app import exception_tasks as tasks
    from app.m3_models import Details
    warehouse=actor(e,'warehouse');task=tasks.listing(e,a)['items'][0]
    task=tasks.act(e,a,task['id'],'assign',dict(expected_version=task['version'],reason='核实观察差异',owner_id=warehouse['id']),'assign-mismatch')
    task=tasks.act(e,warehouse,task['id'],'resolve',dict(expected_version=task['version'],reason='来源口径核实',result='来源观察时间不同，保留独立观察',details=Details(opinion='已核对原始快照证据').model_dump()),'resolve-mismatch')
    before=balance(e);tasks.act(e,a,task['id'],'approve',dict(expected_version=task['version'],reason='独立复核完成'),'review-mismatch')
    assert balance(e)==before
    with db.write(e) as conn:conn.execute(update(s.connections).values(member_ids=[]))
    assert tasks.listing(e,warehouse)['items']==[]
    with e.connect() as conn:
        with pytest.raises(BusinessError):tasks.view(conn,warehouse,task['id'])


def test_supply_links_capacity_release_and_immutable_history(env):
    e,c=env;a=actor(e);r=reserved(e,c)
    from app import workflows
    purchase=workflows.purchase_create(e,a,dict(warehouse_id='wh-cn',code='supply-test',supplier='模拟供应商',note='',lines=[dict(sku_id='sku-1',quantity=12)]),'supply-create')
    source=purchase['lines'][0]['id']
    r=act(c,r,'link',line_id=r['lines'][0]['id'],source_type='purchase_line',source_id=source,quantity=10)
    assert act(c,r,'link',line_id=r['lines'][0]['id'],source_type='purchase_line',source_id=source,quantity=1,ok=409)['error']['code']=='OVER_SUPPLY_LINK'
    r=act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=3)
    assert r['supply_links'][0]['released']==3 and any(h['action']=='supply_released' for h in r['history'])
    with e.begin() as conn:
        with pytest.raises(Exception):conn.execute(update(s.history).values(action='rewritten'))


@pytest.mark.parametrize('quantity',[0,-1,True,1.5])
def test_strict_quantity_boundaries(env,quantity):
    e,c=env;r=reserved(e,c)
    assert act(c,r,'cancel',line_id=r['lines'][0]['id'],quantity=quantity,ok=422)
    assert balance(e)['r']==10
