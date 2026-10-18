"""Runs M3 public commands inside the existing restricted business worker."""
from sqlalchemy import select
from app import db,counting as ct,exception_tasks as tasks,quality_control as qc,inventory as inv,operations as op,accounts,commitments
from app.domain import BusinessError
from app.m3_models import Details


def execute(driver,masters,case):
    engine=driver.engine;trace=[]
    def check(name,value):
        trace.append({'name':name,'passed':bool(value)})
        if not value:raise AssertionError(name)
    def deny(name,call,expected):
        try:call()
        except BusinessError as error:check(name,error.code==expected)
        else:check(name,False)
    def details():return Details(opinion='独立模拟证据核实',evidence_refs=['synthetic-m3-evidence']).model_dump()
    def stock(a,w,s):
        with engine.connect() as c:return inv.load_balance(c,a,w,s)
    for master in masters:
        tenant=master['tenant_id'];admin=driver.actor(tenant);wh=master['warehouses'][0]['id'];sid=master['skus'][0]['id']
        from .common import read_json
        password=read_json(driver.directory/'login.json')['password']
        accounts.create_user(engine,admin,dict(username=tenant+'-counter',name='模拟盘点负责人',role='warehouse',warehouse_ids=[x['id'] for x in master['warehouses']],password=password),'m3-create-counter')
        with engine.connect() as c:counter=dict(c.execute(select(db.users).where(db.users.c.username==tenant+'-counter')).mappings().one())
        original=master['skus'][0]['opening']
        if case['conflict']:
            commitments.authorize(engine,admin,dict(warehouse_id=wh,sku_id=sid,quantity=original-2,expected_version=stock(admin,wh,sid)['version'],source_account='m3-channel',reference='shared-quota',action_id='shared-quota',restore_on_cancel=True),'m3-quota')
        def ca(row,action,extra=None):return ct.act(engine,counter,row['id'],action,dict(expected_version=row['version'],reason='模拟盘点完整流程',**(extra or {})),row['id']+':'+action+':'+str(row['version']))
        def create_count(code,delta):
            row=ct.create(engine,counter,dict(code=code,reason='额外独立数据盘点',lines=[dict(warehouse_id=wh,sku_id=sid)]),code)
            row=ca(row,'confirm');row=ca(row,'start')
            deny(tenant+':locked-receipt',lambda:inv.receive(engine,admin,dict(warehouse_id=wh,sku_id=sid,expected_version=stock(admin,wh,sid)['version'],source='模拟到货',source_account='m3',reference='locked',g=1,q=0,d=0),'m3-locked-receipt'),'COUNT_LOCKED')
            given=[dict(line_id=l['id'],observed=dict(g=l['snapshot']['g']+delta,q=l['snapshot']['q'],d=l['snapshot']['d']),details=details()) for l in row['lines']]
            row=ca(row,'initial',{'lines':given});row=ca(row,'recount',{'lines':given});return ca(row,'submit')
        row=create_count('shared-count',-5 if case['conflict'] else case['count_delta'])
        otherwh=master['warehouses'][1]['id']
        inv.receive(engine,admin,dict(warehouse_id=otherwh,sku_id=sid,expected_version=0,source='模拟到货',source_account='m3',reference='outside-count',g=1,q=0,d=0),'outside-count')
        before=stock(admin,wh,sid)
        approved=ct.act(engine,admin,row['id'],'approve',dict(expected_version=row['version'],reason='其他人员审核差异'),'m3-approve')
        if case['conflict']:
            check(tenant+':coverage-conflict',approved['status']=='conflict' and stock(admin,wh,sid)==before)
            ts=tasks.listing(engine,admin)['items'];t=ts[0]
            t=tasks.act(engine,admin,t['id'],'assign',dict(expected_version=t['version'],reason='分派冲突核查',owner_id=counter['id']),'m3-assign-conflict')
            t=tasks.act(engine,counter,t['id'],'resolve',dict(expected_version=t['version'],reason='实盘证据更正',result='取消后按核实数量重新盘点',details=details()),'m3-resolve-conflict')
            tasks.act(engine,admin,t['id'],'approve',dict(expected_version=t['version'],reason='人工复核通过'),'m3-review-conflict')
            check(tenant+':task-close-no-stock-change',stock(admin,wh,sid)==before)
            ca(approved,'cancel');row=create_count('corrected-count',3)
            approved=ct.act(engine,admin,row['id'],'approve',dict(expected_version=row['version'],reason='更正后复核'),'m3-corrected-approve')
        check(tenant+':posted',approved['status']=='posted')
        # Two stock classes plus a customer return provide independent quality lots.
        receipt_body=dict(warehouse_id=wh,sku_id=sid,expected_version=stock(admin,wh,sid)['version'],source='模拟供应商',source_account='m3',reference='quality-arrival',g=10,q=case['quality_good']+case['quality_bad'],d=0)
        receipt=inv.receive(engine,admin,receipt_body,'m3-arrival')
        sold=inv.sell(engine,admin,dict(warehouse_id=wh,sku_id=sid,expected_version=stock(admin,wh,sid)['version'],source='模拟客户',source_account='m3',reference='quality-sale',quantity=case['sale']),'m3-sale-command')
        rb=dict(kind='customer',original_id=sold['document_id'],reference='shared-return',quantity=case['sale'],expected_version=stock(admin,wh,sid)['version'],reason='模拟客户退回',details=details())
        ret=op.return_create(engine,counter,rb,'m3-return')
        again=op.return_create(engine,counter,rb,'m3-return-second-key');check(tenant+':return-business-dedup',ret['id']==again['id'])
        deny(tenant+':return-changed-voucher',lambda:op.return_create(engine,counter,{**rb,'quantity':1},'m3-return-changed'),'VOUCHER_CONFLICT')
        for batch,good,bad,rid in [('ordinary',case['quality_good'],case['quality_bad'],None),('returned',case['return_good'],case['sale']-case['return_good'],ret['id'])]:
            q=qc.start(engine,counter,dict(warehouse_id=wh,sku_id=sid,return_id=rid,reference='shared-'+batch,quantity=good+bad,expected_balance_version=stock(admin,wh,sid)['version'],details=details()),'m3-start-'+batch)
            q=qc.act(engine,counter,q['id'],'complete',dict(expected_version=q['version'],reason='分批检验完成',good=good,bad=bad,expected_balance_version=stock(admin,wh,sid)['version'],details=details()),'m3-complete-'+batch)
            for i,n in enumerate([1,good-1] if good>1 else [good]):
                q=qc.act(engine,counter,q['id'],'restock',dict(expected_version=q['version'],reason='合格品重新上架',quantity=n,expected_balance_version=stock(admin,wh,sid)['version']),'m3-restock-'+batch+str(i))
            if bad:
                disp=qc.request_disposition(engine,counter,dict(kind='scrap',session_id=q['id'],reference='scrap-'+batch,quantity=bad,expected_balance_version=stock(admin,wh,sid)['version'],details=details()),'m3-scrap-'+batch)
                before=stock(admin,wh,sid)
                result=qc.review(engine,admin,disp['id'],dict(expected_version=1,decision='approve',reason='实物报废复核'),'m3-scrap-review-'+batch)
                replay=qc.review(engine,admin,disp['id'],dict(expected_version=1,decision='approve',reason='实物报废复核'),'m3-scrap-review-'+batch)
                check(tenant+':scrap-once-'+batch,result['document_id']==replay['document_id'] and stock(admin,wh,sid)['d']==before['d']-bad)
        disp=qc.request_disposition(engine,counter,dict(kind='supplier_return',original_id=receipt['document_id'],reference='shared-supplier-return',g=1,q=0,d=0,expected_balance_version=stock(admin,wh,sid)['version'],details=details()),'m3-supplier-return')
        qc.review(engine,admin,disp['id'],dict(expected_version=1,decision='approve',reason='退供应商单据复核'),'m3-supplier-review')
        t=tasks.listing(engine,admin)['items'][0]
        # Identical rule-instance updates keep the same task and append only changes.
        tb={k:t[k] for k in ('warehouse_id','sku_id','object_type','object_id','rule','rule_version','instance','type','priority','impact_quantity','due_at','details','requires_review')}
        same=tasks.create(engine,admin,tb,'m3-task-rescan');check(tenant+':task-instance-dedup',same['id']==t['id'] and same['version']==t['version'])
    foreign=driver.actor(masters[0]['tenant_id'])
    with engine.connect() as connection:
        deny('cross-tenant-count-hidden',lambda:ct.get(connection,foreign,approved['id']),'NOT_FOUND')
    with engine.connect() as c:
        from sqlalchemy import func
        check('all-count-locks-released',c.scalar(select(func.count()).select_from(ct.locks))==0)
    snapshot=driver.snapshot()
    check('ledger-atomic-links',snapshot['ledger_consistent'])
    check('stock-document-outbox-count',len(set(snapshot['counts'][n] for n in ('documents','ledger','outbox')))==1)
    return dict(trace=trace,snapshot=snapshot)
