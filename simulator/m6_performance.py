"""Capacity fixtures use real order commands inside the isolated business worker."""
import json,math,platform,resource,time
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select,func
from app import db,connectors,fulfillment,review,inventory,m4_schema as sales
from app.clock import business_now,parse_timestamp

def populate(driver,master,offset,count):
    actor=driver.actor(master['tenant_id']);engine=driver.engine
    account=connectors.create(engine,actor,dict(source_account='capacity',name='模拟容量测试',warehouse_ids=[x['id'] for x in master['warehouses']],member_ids=[]),'capacity-connection')
    with db.write(engine,actor['tenant_id']) as c:
        for index in range(offset,offset+count):
            sku=master['skus'][index%len(master['skus'])]['id'];warehouse=master['warehouses'][index%len(master['warehouses'])]['id']
            row=fulfillment.create_in_transaction(c,actor,dict(connection_id=account['id'],source_order_id='capacity-'+str(index),warehouse_ids=[warehouse],
                occurred_at=business_now(engine),lines=[dict(source_line_id='line',sku_id=sku,quantity=1)]))
            fulfillment.act_in_transaction(c,actor,row['id'],'confirm',dict(expected_version=row['version'],action_id='confirm-'+str(index),reason='模拟确定需求'))
    return dict(created=count)

def measure(driver,master,orders):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from .common import read_json
    engine=driver.engine;actor=driver.actor(master['tenant_id']);warehouse=master['warehouses'][0]['id'];sku=master['skus'][0]['id']
    start=business_now(engine);driver.advance((parse_timestamp(start)+timedelta(days=2)).isoformat())
    durations={};failures={};samples=20;resource_start=resource.getrusage(resource.RUSAGE_SELF)
    with TestClient(create_app(engine)) as client:
        response=client.post('/api/v1/session',json=dict(username=actor['username'],password=read_json(driver.directory/'login.json')['password']));assert response.status_code==200
        client.headers['X-CSRF-Token']=response.json()['csrf']
        def timed(name,call):
            values=[];bad=0
            for _ in range(samples):
                began=time.perf_counter();response=call();values.append(time.perf_counter()-began);bad+=response.status_code>=400
            values.sort();durations[name]=dict(samples=samples,p50_seconds=values[math.ceil(.5*samples)-1],p95_seconds=values[math.ceil(.95*samples)-1],
                max_seconds=max(values),requests_per_second=samples/sum(values),failed=bad);failures[name]=bad
        timed('order_page',lambda:client.get('/api/v2/orders?limit=20&offset='+str(max(0,orders-20))))
        timed('event_page',lambda:client.get('/api/v2/events?limit=20&offset=100'))
        def receive():
            with engine.connect() as c:stock=inventory.load_balance(c,actor,warehouse,sku)
            return client.post('/api/v2/inventory/receipts',headers={'Idempotency-Key':str(uuid4())},json=dict(warehouse_id=warehouse,sku_id=sku,
                expected_version=stock['version'],source='模拟容量凭证',source_account='capacity',reference=str(uuid4()),g=1,q=0,d=0,note='',occurred_at=business_now(engine)))
        timed('receipt_write',receive)
        job=review.create(engine,actor,dict(warehouse_id=warehouse,start_at=start,end_at=business_now(engine)),'capacity-analysis')
        began=time.perf_counter();report_id=review.process(engine,job['id']);analysis_seconds=time.perf_counter()-began
        timed('report_page',lambda:client.get(f'/api/v2/reviews/reports/{report_id}/rows/sample?limit=20&offset=100'))
        with engine.connect() as c:
            report=review.get(c,actor,report_id);observed=c.scalar(select(func.count()).select_from(sales.orders).where(sales.orders.c.tenant_id==actor['tenant_id']))
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return dict(sku_count=len(master['skus']),warehouse_count=len(master['warehouses']),orders=observed,backend=engine.dialect.name,
        concurrent_clients=1,fixture='真实建单并确认；均未预占，按SKU与仓库均匀分配，稀疏期初库存；非完成订单混合压测',
        measurements=durations,analysis=dict(seconds=analysis_seconds,samples=report['summary']['sample_count'],open_orders=observed,job_status='completed'),
        cpu_seconds=usage.ru_utime+usage.ru_stime-resource_start.ru_utime-resource_start.ru_stime,
        peak_rss_bytes=usage.ru_maxrss*(1 if platform.system()=='Darwin' else 1024),failed_requests=sum(failures.values()),
        passed=observed==orders and not any(failures.values()) and all(v['p95_seconds']<=(3 if k=='receipt_write' else 2) for k,v in durations.items()))
