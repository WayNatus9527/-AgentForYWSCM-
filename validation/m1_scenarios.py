"""Generate fresh M1 datasets and check quantities with independent arithmetic.

Never copies the development database. Identical business IDs are intentional:
the separate files/engines must keep effects isolated. Full process/PG role
isolation, failure replay and simulator truth permissions belong to M2.
"""
import hashlib
import json
from pathlib import Path
import random
import secrets
from sqlalchemy import insert, select, func
from app import db, inventory as inv, commitments as cm, events
from app.config import load_settings
from app.migrations import migrate
from app.domain import BusinessError


def run_scenario(seed, directory):
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    rng=random.Random(seed)
    inputs={'seed':seed,'scenario_version':'m1-3','purpose':'validation',
            'skus':[{'id':'sku-'+str(n),'opening':rng.randint(50,1000),'receipt':rng.randint(1,80),
                     'quota':rng.randint(20,40),'reserve':rng.randint(5,20),
                     'restore':bool(rng.randrange(2))} for n in range(rng.randint(1,5))]}
    sale_rng=random.Random(seed ^ 0x51A1E)
    for item in inputs['skus']:
        item['sale']=sale_rng.randint(1,min(10,item['opening']-item['quota']))
    canonical=json.dumps(inputs,sort_keys=True,separators=(',',':'))
    (directory/'inputs.json').write_text(canonical)
    settings=load_settings({'APP_DATA_DIR':str(directory),'APP_RUNTIME_MODE':'test',
                            'APP_RUN_ID':'m1-validation-'+str(seed)})
    engine=db.make_engine(settings=settings)
    report={'seed':seed,'run_id':settings.run_id,'purpose':'validation','scenario_version':'m1-3',
            'input_sha256':hashlib.sha256(canonical.encode()).hexdigest(),'database_fingerprint':engine.runtime_fingerprint,
            'checks':0,'status':'failed'}
    try:
        migrate(engine)
        actor={'id':'operator','tenant_id':'tenant-sim','username':'operator','name':'模拟操作员',
               'role':'admin','warehouse_ids':[],'password_hash':secrets.token_hex(16)+'$'+secrets.token_hex(32)}
        with db.write(engine,actor['tenant_id']) as c:
            c.execute(insert(db.tenants).values(id=actor['tenant_id'],name='独立验证商户'))
            c.execute(insert(db.users).values(**actor))
            c.execute(insert(db.warehouses).values(id='wh-01',tenant_id=actor['tenant_id'],code='WH-01',
                      name='模拟仓',country='CN',timezone='Asia/Shanghai',authority='local'))
            for item in inputs['skus']:
                c.execute(insert(db.skus).values(id=item['id'],tenant_id=actor['tenant_id'],code=item['id'],
                          name='模拟商品'+item['id'],spec='基础规格',unit='件',barcode=''))
                stock=inv.load_balance(c,actor,'wh-01',item['id'])
                inv.movement(c,actor,stock,{'g':item['opening']},'opening','validation-opening','独立生成期初',0)
        for index,item in enumerate(inputs['skus']):
            sid=item['id'];prefix=str(index)+'-'
            received=inv.receive(engine,actor,dict(warehouse_id='wh-01',sku_id=sid,expected_version=1,
                source='模拟供应商',source_account='SUP-01',reference=prefix+'DEL-01',g=item['receipt'],q=0,d=0,
                note='',occurred_at='2026-10-01T00:00:00Z'),prefix+'receipt-request')
            replay=inv.receive(engine,actor,dict(warehouse_id='wh-01',sku_id=sid,expected_version=2,
                source='模拟供应商',source_account='SUP-01',reference=prefix+'DEL-01',g=item['receipt'],q=0,d=0,
                note='',occurred_at='2026-10-01T00:00:00Z'),prefix+'changed-request')
            assert replay['document_id']==received['document_id'] and replay['business_replayed']
            grant=cm.authorize(engine,actor,dict(warehouse_id='wh-01',sku_id=sid,expected_version=2,quantity=item['quota'],
                    source_account='SHOP-01',reference=prefix+'AUTH-01',action_id=prefix+'authorize',
                    restore_on_cancel=item['restore']),prefix+'authorize-request')['grant']
            reservation=cm.reserve(engine,actor,grant['id'],dict(order_id='ORDER-01',line_id=prefix+'line',
                action_id=prefix+'reserve',quantity=item['reserve'],grant_version=1,expected_version=3),prefix+'reserve-request')['reservation']
            cancelled=rng.randint(1,item['reserve'])
            result=cm.cancel(engine,actor,reservation['id'],dict(action_id=prefix+'cancel',quantity=cancelled,
                reason='模拟部分取消',grant_version=2,reservation_version=1,expected_version=4),prefix+'cancel-request')
            available_quota=item['quota']-item['reserve']+(cancelled if item['restore'] else 0)
            withdraw_qty=rng.randint(1,available_quota) if available_quota else 0
            withdrawal=dict(action_id=prefix+'withdraw',quantity=withdraw_qty or 1,
                            reason='模拟待确认撤回',grant_version=3,expected_version=5)
            if withdraw_qty:
                cm.withdraw(engine,actor,grant['id'],withdrawal,prefix+'withdraw-request')
            else:
                try:cm.withdraw(engine,actor,grant['id'],withdrawal,prefix+'withdraw-request')
                except BusinessError as exc:assert exc.code=='INSUFFICIENT_QUOTA'
                else:raise AssertionError('Exhausted quota allowed withdrawal')
            # Expected values are calculated from generated inputs, not SUT helpers.
            expected={'g':item['opening']+item['receipt'],'q':0,'d':0,
                      'r':item['reserve']-cancelled,'online':available_quota-withdraw_qty,'withdrawing':withdraw_qty}
            with engine.connect() as c:
                actual=dict(c.execute(select(db.balances).where(db.balances.c.sku_id==sid)).mappings().one())
                assert all(actual[f]==value for f,value in expected.items())
                assert actual['g']-actual['r']-actual['online']-actual['withdrawing']==(
                    item['opening']+item['receipt']-item['quota']+(0 if item['restore'] else cancelled))
            if withdraw_qty:
                cm.withdraw(engine,actor,grant['id'],dict(action_id=prefix+'ack',quantity=withdraw_qty,
                    reason='模拟来源确认',grant_version=4,expected_version=6),prefix+'ack-request',confirm=True)
            with engine.connect() as c:
                actual=dict(c.execute(select(db.balances).where(db.balances.c.sku_id==sid)).mappings().one())
                assert actual['withdrawing']==0 and actual['g']==expected['g']
            try:
                cm.reserve(engine,actor,grant['id'],dict(order_id='ORDER-01',line_id=prefix+'line',
                    action_id=prefix+'second-reserve',quantity=1,grant_version=5,expected_version=7),prefix+'duplicate-line')
            except BusinessError as exc:
                assert exc.code=='ORDER_LINE_EXISTS'
            else:
                raise AssertionError('Duplicate source order line was accepted')
            shipment=dict(warehouse_id='wh-01',sku_id=sid,expected_version=7 if withdraw_qty else 5,
                          source='模拟门店交货',source_account='STORE-01',reference=prefix+'SHIP-01',
                          quantity=item['sale'],note='',occurred_at=None)
            sold=inv.sell(engine,actor,shipment,prefix+'sale-request')
            repeated=inv.sell(engine,actor,shipment|{'expected_version':shipment['expected_version']+1},
                              prefix+'new-sale-request')
            assert repeated['business_replayed'] and repeated['document_id']==sold['document_id']
            with engine.connect() as c:
                after_sale=dict(c.execute(select(db.balances).where(db.balances.c.sku_id==sid)).mappings().one())
            assert after_sale['g']==item['opening']+item['receipt']-item['sale']
            assert all(after_sale[f]==expected[f] for f in ('r','online')) and after_sale['withdrawing']==0
            report['checks']+=11
        with engine.connect() as c:
            stocks=[dict(r) for r in c.execute(select(db.balances).order_by(db.balances.c.sku_id)).mappings()]
            report['stock_digest']=hashlib.sha256(json.dumps([{k:v for k,v in r.items() if k not in ('id','updated_at')} for r in stocks],sort_keys=True).encode()).hexdigest()
            docs=c.scalar(select(func.count()).select_from(db.documents))
            assert docs==c.scalar(select(func.count()).select_from(db.ledger))
            assert docs==c.scalar(select(func.count()).select_from(db.outbox))
            assert docs==c.scalar(select(func.count()).select_from(events.business_events))
        report['checks']+=3;report['status']='passed'
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
        raise
    finally:
        engine.dispose()
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report
