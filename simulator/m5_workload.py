"""Actual M5 services in the isolated worker; no access to private expected data."""
from uuid import uuid4
from sqlalchemy import select,func
from app import db,catalog,supply,planning,m5_schema as s,quality_control as qc,accounts,connectors,fulfillment
from app.domain import BusinessError
from app.m3_models import Details

def execute(driver,masters,case):
    engine=driver.engine;trace=[];actual_plans={}
    driver.advance('2026-10-05T01:00:00+00:00')
    def check(name,value):
        trace.append(dict(name=name,passed=bool(value)))
        if not value:raise AssertionError(name)
    def key():return str(uuid4())
    detail=Details(opinion='独立模拟来源凭证核对',evidence_refs=['scenario-m5']).model_dump()
    for master in masters:
        actor=driver.actor(master['tenant_id']);warehouse=master['warehouses'][0]['id'];destination=master['warehouses'][1]['id'];sku=master['skus'][0]['id']
        def stock(w=warehouse):
            from app.inventory import load_balance
            with engine.connect() as c:return load_balance(c,actor,w,sku)
        vendor=catalog.save_supplier(engine,actor,dict(code='shared-vendor',name='模拟供货商',default_lead_days=case['lead'],sku_ids=[sku]),key())
        count=case['good']+case['quality_good']+case['quality_bad']+case['bad']
        purchase=supply.create(engine,actor,'purchase',dict(code='shared-purchase',warehouse_id=warehouse,supplier_id=vendor['id'],ordered_at='2026-10-01T01:00:00Z',
            promised_at='2026-10-06T01:00:00Z',lines=[dict(sku_id=sku,quantity=count+case['remaining'])]),key())
        original=purchase['terms']['original_due']
        due={'timely':'2026-10-06T01:00:00Z','overdue':'2026-10-03T01:00:00Z','unknown':None,'outside':'2026-12-01T01:00:00Z'}[case['supply_due']]
        purchase=supply.update_promise(engine,actor,purchase['id'],dict(expected_version=purchase['version'],reference='promise-change',reason='模拟供应商承诺变更',promised_at=due),key())
        check(master['tenant_id']+':original-promise-preserved',purchase['terms']['original_due']==original)
        def make(row,n,reference):
            return supply.make_batch(engine,actor,row['kind'],row['id'],dict(expected_version=row['version'],reference=reference,reason='模拟批次计划',
                supply_line_id=row['lines'][0]['id'],quantity=n,expected_at=due,expected_balance_version=stock()['version'] if row['kind']=='transfer' else None),key())
        def action(row,name,index=-1,**extra):
            b=row['lines'][0]['batches'][index];body=dict(expected_version=b['version'],reference=name+'-'+b['reference'],reason='模拟实际凭证')
            if name in ('ship','receive'):body['occurred_at']='2026-10-02T01:00:00Z'
            if name=='receive':body['expected_balance_version']=stock(b['warehouse_id'])['version']
            elif row['kind']=='transfer':body['expected_balance_version']=stock()['version']
            body.update(extra);return supply.batch_action(engine,actor,b['id'],name,body,key())
        purchase=action(make(purchase,count,'purchase-batch'),'ship')
        purchase=action(purchase,'receive',g=case['good'],q=case['quality_good']+case['quality_bad'],d=case['bad'])
        receipt=purchase['lines'][0]['batches'][0]['receipts'][0]
        inspection=supply.inspect(engine,actor,receipt['id'],dict(reference='purchase-inspect',quantity=receipt['q'],expected_balance_version=stock()['version'],details=detail),key())
        inspection=qc.act(engine,actor,inspection['id'],'complete',dict(expected_version=inspection['version'],good=case['quality_good'],bad=case['quality_bad'],expected_balance_version=stock()['version'],details=detail),key())
        if case['restocked']:qc.act(engine,actor,inspection['id'],'restock',dict(expected_version=inspection['version'],quantity=case['restocked'],expected_balance_version=stock()['version']),key())
        if case['close_remaining']:purchase=supply.close(engine,actor,'purchase',purchase['id'],dict(expected_version=purchase['version'],reference='close-unshipped',reason='取消未来剩余计划'),key())
        supply.scan_delays(engine,actor,key());supply.scan_delays(engine,actor,key())
        from app.exception_tasks import tasks
        with engine.connect() as c:
            check(master['tenant_id']+':late-task-not-duplicated',c.scalar(select(func.count()).select_from(tasks).where(tasks.c.tenant_id==actor['tenant_id'],tasks.c.rule=='purchase_late'))<=1)
        transport=supply.create(engine,actor,'transfer',dict(code='shared-transfer',warehouse_id=warehouse,destination_id=destination,reason='模拟分批调拨',expected_at=due,
            lines=[dict(sku_id=sku,quantity=case['transfer_sent']+2)]),key())
        transport=make(transport,case['transfer_sent'],'transport-one');transport=make(transport,2,'transport-two')
        transport=action(transport,'ship',index=0);transport=action(transport,'cancel',index=1)
        transport=action(transport,'receive',index=0,g=case['transfer_received']-case['transfer_bad'],d=case['transfer_bad'])
        if case['transfer_lost']:
            batch=transport['lines'][0]['batches'][0]
            claim=supply.discrepancy(engine,actor,batch['id'],dict(expected_version=batch['version'],reference='partial-loss',reason='模拟运输差异',quantity=case['transfer_lost'],kind='shortage',details=detail),key())
            reviewer=accounts.create_user(engine,actor,dict(username=master['tenant_id']+'-reviewer',name='独立复核',role='admin',warehouse_ids=[],password=uuid4().hex),key())
            with engine.connect() as c:reviewer=dict(c.execute(select(db.users).where(db.users.c.id==reviewer['id'])).mappings().one())
            supply.review(engine,reviewer,claim['id'],dict(expected_version=claim['version'],reference='review-loss',reason='独立核实后批准',decision='approve'),key())
        with engine.connect() as c:transport=supply.view(c,actor,'transfer',transport['id'])
        supply.close(engine,actor,'transfer',transport['id'],dict(expected_version=transport['version'],reference='close-transfer-rest',reason='关闭未发余量'),key())
        check(master['tenant_id']+':transport-conservation',transport['lines'][0]['in_transit']==case['transfer_sent']-case['transfer_received']-case['transfer_lost'])
        connection=connectors.create(engine,actor,dict(source_account='shared-sales',name='模拟需求渠道',warehouse_ids=[warehouse],member_ids=[]),key())
        sale=fulfillment.create(engine,actor,dict(connection_id=connection['id'],source_order_id='shared-order',warehouse_ids=[warehouse],lines=[dict(source_line_id='1',sku_id=sku,quantity=case['open_order'])]),key())
        sale=fulfillment.act(engine,actor,sale['id'],'confirm',dict(expected_version=1,action_id='confirm',reason='确认需求'),key())
        if case['reserve_order']:fulfillment.act(engine,actor,sale['id'],'allocate',dict(expected_version=sale['version'],action_id='reserve',reason='需求占用',line_id=sale['lines'][0]['id'],quantity=case['open_order']),key())
        for index,day in enumerate(('2026-10-02','2026-10-03','2026-10-04')):
            if case['demand_quality']=='missing' and index==1:continue
            body=dict(warehouse_id=warehouse,sku_id=sku,day=day,quantity=case['daily'][index],quality='abnormal' if case['demand_quality']=='abnormal' else 'known',source_reference='scenario-day-'+day)
            planning.demand_day(engine,actor,body,key())
            check(master['tenant_id']+':demand-voucher-'+day,planning.demand_day(engine,actor,body,key())['business_replayed'])
        policy=planning.policy(engine,actor,dict(warehouse_id=warehouse,sku_id=sku,supplier_id=vendor['id'],window_days=3,coverage_days=case['coverage'],lead_days=case['lead'],
            safety=case['safety'],minimum=case['minimum'],pack=case['pack'],includes_open_orders=case['includes_open_orders']),key())
        plan=planning.plan(engine,actor,policy['id'],key());actual_plans[master['tenant_id']]=plan['suggested']
        check(master['tenant_id']+':unknown-demand-explicit',(plan['status']=='unknown')==(case['demand_quality']!='known'))
        if case['demand_quality']=='known' and case['fault']=='none':
            result=planning.confirm(engine,actor,plan['id'],dict(quantity=plan['suggested'],reason='模拟人工核对采购'),key())
            repeated=planning.confirm(engine,actor,plan['id'],dict(quantity=plan['suggested'],reason='模拟人工核对采购'),key())
            check(master['tenant_id']+':one-purchase-per-confirmation',repeated['business_replayed'] and repeated['order_id']==result['order_id'])
    with engine.connect() as c:
        try:supply.get_order(c,driver.actor(masters[0]['tenant_id']),'purchase',purchase['id'])
        except BusinessError as ex:check('foreign-purchase-hidden',ex.code=='NOT_FOUND')
        else:check('foreign-purchase-hidden',False)
    snapshot=driver.snapshot();check('ledger-consistent',snapshot['ledger_consistent'])
    return dict(trace=trace,snapshot=snapshot,planning=actual_plans)

def confirm_first(driver,tenant):
    with driver.engine.connect() as c:plan=dict(c.execute(select(s.plans).where(s.plans.c.tenant_id==tenant)).mappings().one())
    return planning.confirm(driver.engine,driver.actor(tenant),plan['id'],dict(quantity=plan['suggested'],reason='模拟崩溃恢复确认'),'m5-confirm-crash')

def recover(driver,masters,case):
    trace=[]
    for master in masters:
        first=confirm_first(driver,master['tenant_id']);second=confirm_first(driver,master['tenant_id'])
        trace.append(dict(name=master['tenant_id']+':confirmation-recovery-same-order',passed=second['replayed'] and first['order_id']==second['order_id']))
    with driver.engine.connect() as c:
        trace.append(dict(name='one-confirmation-per-plan',passed=c.scalar(select(func.count()).select_from(s.decisions))==len(masters)))
    return dict(trace=trace,snapshot=driver.snapshot())
