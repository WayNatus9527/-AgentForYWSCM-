"""Actual M6 business workflows; never read the controller's expected facts."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select,func
from app import db,inventory,catalog,supply,fulfillment,connectors,quality_control as qc,review,review_agent,m6_schema as s,m5_schema as ps
from app.clock import parse_timestamp,business_now
from app.domain import BusinessError
from app.m3_models import Details

def key():return str(uuid4())

def execute(driver,masters,case):
    engine=driver.engine;base=parse_timestamp(business_now(engine));trace=[];contexts=[];schedule=[]
    def instant(hour):return (base+timedelta(hours=hour)).isoformat()
    detail=Details(opinion='独立模拟复盘业务凭证',evidence_refs=['m6-public-case']).model_dump()
    def stock(ctx):
        with engine.connect() as c:return inventory.load_balance(c,ctx['actor'],ctx['warehouse'],ctx['sku'])
    def sale_action(ctx,action,**fields):
        sale=ctx['sale'];ctx['sale']=fulfillment.act(engine,ctx['actor'],sale['id'],action,
            dict(expected_version=sale['version'],action_id=key(),reason='模拟明确业务动作',**fields),key())
    def add(hour,fn):schedule.append((hour,len(schedule),fn))
    for master in masters:
        actor=driver.actor(master['tenant_id']);warehouse=master['warehouses'][0]['id'];sku=master['skus'][0]['id'];n=sum(case['quantities'])
        ctx=dict(actor=actor,warehouse=warehouse,sku=sku,lots=[]);contexts.append(ctx)
        if not case['stock_available']:
            with db.write(engine,actor['tenant_id']) as c:
                b=inventory.load_balance(c,actor,warehouse,sku)
                inventory.movement(c,actor,b,dict(h=b['g']),'hold','m6-opening-hold','模拟期初现货冻结',b['version'])
        vendor=catalog.save_supplier(engine,actor,dict(code='m6-vendor',name='模拟复盘供货商',default_lead_days=5,sku_ids=[sku]),key())
        ctx['purchase']=supply.create(engine,actor,'purchase',dict(code='m6-purchase',warehouse_id=warehouse,supplier_id=vendor['id'],ordered_at=instant(0),
            promised_at=instant(120),lines=[dict(sku_id=sku,quantity=n)]),key())
        account=connectors.create(engine,actor,dict(source_account='m6-orders',name='模拟复盘订单',warehouse_ids=[warehouse],member_ids=[]),key())
        ctx['sale']=fulfillment.create(engine,actor,dict(connection_id=account['id'],source_order_id='m6-demand',warehouse_ids=[warehouse],planned_at=instant(500),
            lines=[dict(source_line_id='line',sku_id=sku,quantity=n)]),key())
        sale_action(ctx,'confirm')
        if case['linked']:sale_action(ctx,'link',line_id=ctx['sale']['lines'][0]['id'],source_type='purchase_line',source_id=ctx['purchase']['lines'][0]['source_line_id'],quantity=n)
        for index,quantity in enumerate(case['quantities']):
            purchase=ctx['purchase'];arrive=120+case['arrival_offsets'][index]
            ctx['purchase']=supply.make_batch(engine,actor,'purchase',purchase['id'],dict(expected_version=purchase['version'],reference='batch-'+str(index),
                reason='模拟并行批次',supply_line_id=purchase['lines'][0]['id'],quantity=quantity,expected_at=instant(arrive)),key())
            batch=ctx['purchase']['lines'][0]['batches'][-1];lot=dict(batch_id=batch['id'],quantity=quantity,arrival=arrive);ctx['lots'].append(lot)
            ctx['purchase']=supply.batch_action(engine,actor,batch['id'],'ship',dict(expected_version=batch['version'],reference='ship-'+str(index),reason='按凭证交运',occurred_at=instant(0)),key())
            def receive(ctx=ctx,lot=lot):
                with engine.connect() as c:batch=dict(c.execute(select(ps.batches).where(ps.batches.c.id==lot['batch_id'])).mappings().one())
                actual=None if case['actual_quality']=='unknown' else instant(lot['arrival']+(24 if case['actual_quality']=='invalid_clock' else 0))
                ctx['purchase']=supply.batch_action(engine,ctx['actor'],lot['batch_id'],'receive',dict(expected_version=batch['version'],reference='receive-'+lot['batch_id'],reason='模拟实际到货',
                    occurred_at=actual,expected_balance_version=stock(ctx)['version'],g=lot['quantity'] if case['registration_hours'] else 0,q=0 if case['registration_hours'] else lot['quantity']),key())
                batch=next(b for b in ctx['purchase']['lines'][0]['batches'] if b['id']==lot['batch_id']);lot['receipt']=batch['receipts'][0]
            add(arrive+case['registration_hours'],receive)
            if not case['registration_hours']:
                def inspect(ctx=ctx,lot=lot):
                    lot['inspection']=supply.inspect(engine,ctx['actor'],lot['receipt']['id'],dict(reference='inspect-'+lot['batch_id'],quantity=lot['quantity'],expected_balance_version=stock(ctx)['version'],details=detail),key())
                def finish(ctx=ctx,lot=lot):
                    inspection=lot['inspection'];inspection=qc.act(engine,ctx['actor'],inspection['id'],'complete',dict(expected_version=inspection['version'],good=lot['quantity'],bad=0,
                        expected_balance_version=stock(ctx)['version'],details=detail),key())
                    qc.act(engine,ctx['actor'],inspection['id'],'restock',dict(expected_version=inspection['version'],quantity=lot['quantity'],expected_balance_version=stock(ctx)['version']),key())
                add(arrive+case['quality_queue_hours'],inspect);add(arrive+case['quality_queue_hours']+1,finish)
        available=120+max(case['arrival_offsets'])+case['registration_hours']+(0 if case['registration_hours'] else case['quality_queue_hours']+1)
        allocated=0 if case['stock_available'] else available
        def allocate(ctx=ctx,n=n):sale_action(ctx,'allocate',line_id=ctx['sale']['lines'][0]['id'],quantity=n)
        add(allocated,allocate)
        for offset,action,kind in [(0,'start','pick'),(1,'complete','pick'),(1,'start','review'),(1.5,'complete','review')]:
            def task(ctx=ctx,action=action,kind=kind):sale_action(ctx,action,batch_id=ctx['sale']['batches'][0]['id'],kind=kind)
            add(allocated+case['picking_queue_hours']+offset,task)
        def ship(ctx=ctx,n=n):sale_action(ctx,'ship',batch_id=ctx['sale']['batches'][0]['id'],quantity=n,reference='actual-handoff',occurred_at=business_now(engine))
        add(allocated+case['picking_queue_hours']+1.5,ship)
    for hour,_,action in sorted(schedule,key=lambda x:(x[0],x[1])):driver.advance(instant(hour));action()
    for ctx in contexts:
        body=dict(warehouse_id=ctx['warehouse'],sku_id=ctx['sku'],start_at=instant(0),end_at=business_now(engine),min_samples=1)
        job=review.create(engine,ctx['actor'],body,'m6-analysis-job')
        if case['fault']=='none':review.process(engine,job['id'])
        traced=review_agent.execute(engine,ctx['actor'],dict(tool='track_purchase',identity=ctx['purchase']['id']),'m6-track-purchase')
        trace.append(dict(name=ctx['actor']['tenant_id']+':execution-tracking-conserved',passed=traced['result']['purchase']['lines'][0]['received']==sum(case['quantities'])))
    result=dict(trace=trace,snapshot=driver.snapshot())
    if case['fault']=='none':result['diagnoses']=diagnoses(driver,masters)
    return result

def first_job(driver,tenant):
    with driver.engine.connect() as c:identity=c.scalar(select(s.jobs.c.id).where(s.jobs.c.tenant_id==tenant))
    return review.process(driver.engine,identity)

def diagnoses(driver,masters):
    result={}
    with driver.engine.connect() as c:
        for master in masters:
            actor=driver.actor(master['tenant_id']);job=dict(c.execute(select(s.jobs).where(s.jobs.c.tenant_id==actor['tenant_id'])).mappings().one())
            report=review.get(c,actor,job['report_id']);candidates=review.page(c,actor,report['id'],'candidate',limit=100)['items'];summary=report['summary'];metric=summary['metrics']
            result[actor['tenant_id']]=dict(constraints=sorted(x['payload']['metric'] for x in candidates if x['payload']['classification']=='evidenced_constraint'),
                constraint_quantity=summary['constraint_quantity'],data_delay=any(x['payload']['classification']=='data_delay' for x in candidates),
                long_transport=any(x['payload']['metric']=='transit' and x['payload']['classification']=='long_duration' for x in candidates),
                unknown_operations=sorted(k for k in ('quality_wait','transit') if metric.get(k,{}).get('unknown_samples')),
                order_ship_hours=metric['order_ship']['median_hours'],transit_hours=metric['transit']['median_hours'])
    return result

def recover(driver,masters,case):
    trace=[]
    for master in masters:
        first=first_job(driver,master['tenant_id']);second=first_job(driver,master['tenant_id'])
        trace.append(dict(name=master['tenant_id']+':one-report-after-crash',passed=first==second and bool(first)))
    with driver.engine.connect() as c:trace.append(dict(name='unique-report-per-job',passed=c.scalar(select(func.count()).select_from(s.reports))==len(masters)))
    return dict(trace=trace,snapshot=driver.snapshot(),diagnoses=diagnoses(driver,masters))
