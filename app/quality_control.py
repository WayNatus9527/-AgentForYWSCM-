"""Inspection batches, protected inspected lots and reviewed dispositions."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U
from sqlalchemy import select, insert, update, func
from . import db, inventory as inv
from .domain import require_role, uid, now
from .m3_common import command, fail, scope, version, evidence, read_actor
from .counting import assert_unlocked

DEFAULT_DETAILS=dict(reason_code='unknown',defect_category='unknown',responsibility_type='unknown',
    responsibility_ref='',evidence_refs=[],opinion='历史兼容入口，未提供结构化原因')
sessions=Table('quality_sessions',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),
    C('warehouse_id',S,nullable=False),C('sku_id',S,nullable=False),C('return_id',S),C('reference',S,nullable=False),
    C('quantity',I,nullable=False),C('good',I,nullable=False),C('bad',I,nullable=False),C('restocked',I,nullable=False),C('disposed',I,nullable=False),
    C('status',S,nullable=False),C('version',I,nullable=False),C('details',JSON,nullable=False),
    C('actor_id',S,nullable=False),C('document_id',S),C('created_at',S,nullable=False),U('tenant_id','reference'))
dispositions=Table('quality_dispositions',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),
    C('warehouse_id',S,nullable=False),C('sku_id',S,nullable=False),C('kind',S,nullable=False),C('reference',S,nullable=False),
    C('session_id',S),C('original_id',S),C('quantity',I,nullable=False),C('body',JSON,nullable=False),C('details',JSON,nullable=False),
    C('status',S,nullable=False),C('version',I,nullable=False),C('requester_id',S,nullable=False),C('reviewer_id',S),
    C('document_id',S),C('created_at',S,nullable=False),U('tenant_id','kind','reference'))
details=Table('quality_details',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),
    C('object_type',S,nullable=False),C('object_id',S,nullable=False),C('details',JSON,nullable=False),U('tenant_id','object_type','object_id'))
vouchers=Table('quality_vouchers',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),
    C('kind',S,nullable=False),C('reference',S,nullable=False),C('fingerprint',S,nullable=False),
    C('warehouse_id',S,nullable=False),C('sku_id',S,nullable=False),C('response',JSON,nullable=False),U('tenant_id','kind','reference'))
history=Table('quality_action_history',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),
    C('object_id',S,nullable=False),C('action',S,nullable=False),C('actor_id',S,nullable=False),C('payload',JSON,nullable=False),C('created_at',S,nullable=False))
TABLE_NAMES=tuple(x.name for x in (sessions,dispositions,details,vouchers,history))


def protected(c,tenant,warehouse,sku):
    rows=c.execute(select(sessions).where(sessions.c.tenant_id==tenant,sessions.c.warehouse_id==warehouse,sessions.c.sku_id==sku)).mappings()
    q=d=0
    for row in rows:
        if row['status']=='started' and not row['return_id']:q+=row['quantity']
        if row['status']=='completed':q+=row['good']-row['restocked'];d+=row['bad']-row['disposed']
    return q,d


def get(c,actor,identity,table=sessions):
    row=c.execute(select(table).where(table.c.id==identity,table.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:fail('NOT_FOUND','质量记录不存在或未授权',404)
    scope(c,actor,row['warehouse_id'],row['sku_id'])
    return dict(row)


def log(c,actor,row,action,payload,*,kind='inspection',lifecycle='registered'):
    c.execute(insert(history).values(id=uid(),tenant_id=actor['tenant_id'],object_id=row['id'],action=action,actor_id=actor['id'],payload=payload,created_at=now()))
    evidence(c,actor,kind,row,action,lifecycle=lifecycle,quantity=row.get('quantity'),payload=payload)


def remember(c,actor,kind,row,body):
    meta=body.get('details') or DEFAULT_DETAILS
    c.execute(insert(details).values(id=uid(),tenant_id=actor['tenant_id'],object_type=kind,object_id=row['id'],details=meta))
    eventrow={**row,'details':meta}
    log(c,actor,eventrow,'returned' if kind=='return' else 'completed',{'reference':body['reference']},kind=kind,lifecycle='completed')
    if kind=='return' and row['kind']=='customer':
        log(c,actor,eventrow,'waiting_inspection',{},kind=kind,lifecycle='queued')
    elif kind=='quality':
        # Legacy combined inspection/restock remains an explicit combined command.
        # It does NOT fabricate a past start time. New staged inspections use start().
        log(c,actor,eventrow,'restocked',{'good':row['good'],'mode':'combined_inspect_and_restock'},kind=kind,lifecycle='completed')


def voucher(c,actor,kind,body,apply):
    digest=inv.fingerprint({k:v for k,v in body.items() if k not in ('expected_version','return_version')})
    old=c.execute(select(vouchers).where(vouchers.c.tenant_id==actor['tenant_id'],vouchers.c.kind==kind,vouchers.c.reference==body['reference'])).mappings().first()
    if old:
        scope(c,actor,old['warehouse_id'],old['sku_id'])
        if old['fingerprint']!=digest:fail('VOUCHER_CONFLICT','该质量或退货凭证已用于不同内容，请核对原单')
        return {**old['response'],'business_replayed':True}
    result=apply()
    c.execute(insert(vouchers).values(id=uid(),tenant_id=actor['tenant_id'],kind=kind,reference=body['reference'],fingerprint=digest,
        warehouse_id=result['warehouse_id'],sku_id=result['sku_id'],response=result))
    return {**result,'business_replayed':False}


def start(engine,actor,body,key):
    if type(body['quantity']) is not int or not 1<=body['quantity']<=1_000_000:fail('INVALID_QUANTITY','检验量须为正整数',422)
    require_role(actor,'admin','warehouse')
    def authorize(c):
        scope(c,actor,body['warehouse_id'],body['sku_id'])
        if body.get('return_id'):
            from .operations import returns
            ret=c.execute(select(returns).where(returns.c.id==body['return_id'],returns.c.tenant_id==actor['tenant_id'])).mappings().first()
            if not ret or ret['kind']!='customer' or ret['warehouse_id']!=body['warehouse_id'] or ret['sku_id']!=body['sku_id']:
                fail('NOT_FOUND','退货单不存在或与当前范围不一致',404)
    def apply(c):
        old=c.execute(select(sessions).where(sessions.c.tenant_id==actor['tenant_id'],sessions.c.reference==body['reference'])).mappings().first()
        if old:
            if any(old[k]!=body.get(k) for k in ('warehouse_id','sku_id','return_id','quantity','details')):fail('VOUCHER_CONFLICT','检验批次凭证已用于其他内容')
            return {**dict(old),'business_replayed':True}
        assert_unlocked(c,actor,body['warehouse_id'],body['sku_id'])
        stock=inv.load_balance(c,actor,body['warehouse_id'],body['sku_id'])
        if stock['version']!=body['expected_balance_version']:fail('VERSION_CONFLICT','库存已变化')
        from .operations import returns,quality_hold
        if body.get('return_id'):
            ret=c.execute(select(returns).where(returns.c.id==body['return_id'])).mappings().one()
            reserved=c.scalar(select(func.coalesce(func.sum(sessions.c.quantity),0)).where(sessions.c.return_id==ret['id'],sessions.c.status=='started'))
            available=ret['quantity']-ret['good']-ret['bad']-reserved
        else:
            held,_=protected(c,actor['tenant_id'],body['warehouse_id'],body['sku_id'])
            available=stock['q']-quality_hold(c,actor['tenant_id'],body['warehouse_id'],body['sku_id'])-held
        if body['quantity']>available:fail('QUALITY_RESERVED','检验数量超过本批可用待验量')
        row={k:body.get(k) for k in ('warehouse_id','sku_id','return_id','reference','quantity','details')}
        row.update(id=uid(),tenant_id=actor['tenant_id'],good=0,bad=0,restocked=0,disposed=0,status='started',version=1,actor_id=actor['id'],document_id=None,created_at=now())
        c.execute(insert(sessions).values(**row));log(c,actor,row,'started',body,lifecycle='started');return row
    return command(engine,actor,'inspection.start',body,key,apply,authorize)


def act(engine,actor,identity,action,body,key):
    for name in (('good','bad') if action=='complete' else ('quantity',) if action=='restock' else ()):
        if type(body[name]) is not int or not (0 if action=='complete' else 1)<=body[name]<=1_000_000:fail('INVALID_QUANTITY','数量无效',422)
    require_role(actor,'admin','warehouse')
    def apply(c):
        row=get(c,actor,identity);version(row,body)
        assert_unlocked(c,actor,row['warehouse_id'],row['sku_id'])
        if action=='cancel':
            if row['status']!='started':fail('INVALID_STATE','仅未完成检验可以取消')
            changes={'status':'cancelled'}
        elif action=='complete':
            if row['status']!='started':fail('INVALID_STATE','请先开始检验')
            if body['good']+body['bad']!=row['quantity']:fail('INSPECTION_TOTAL','本次检验结果须等于本批次检验量',422)
            from .operations import returns
            if row['return_id']:
                ret=c.execute(select(returns).where(returns.c.id==row['return_id'])).mappings().one()
                if row['quantity']>ret['quantity']-ret['good']-ret['bad']:fail('OVER_INSPECTION','已超过退货单剩余量')
                good=ret['good']+body['good'];bad=ret['bad']+body['bad']
                c.execute(update(returns).where(returns.c.id==ret['id']).values(good=good,bad=bad,version=ret['version']+1,status='completed' if good+bad==ret['quantity'] else 'awaiting_quality'))
            changes={'status':'completed','good':body['good'],'bad':body['bad'],'details':body['details']}
        elif action=='restock':
            if row['status']!='completed':fail('INVALID_STATE','请先完成检验')
            if body['quantity']>row['good']-row['restocked']:fail('OVER_RESTOCK','超过已检合格且未上架数量')
            changes={'restocked':row['restocked']+body['quantity']}
        else:fail('INVALID_ACTION','不支持的检验操作',422)
        changes['version']=row['version']+1
        c.execute(update(sessions).where(sessions.c.id==identity).values(**changes));row.update(changes)
        if action!='cancel':
            stock=inv.load_balance(c,actor,row['warehouse_id'],row['sku_id'])
            delta={'q':-body['bad'],'d':body['bad']} if action=='complete' else {'q':-body['quantity'],'g':body['quantity']}
            doc=inv.movement(c,actor,stock,delta,'quality_inspect' if action=='complete' else 'quality_restock',identity,
                body.get('details',row['details'])['opinion'],body['expected_balance_version'],business_reference=row['reference'])
            c.execute(update(sessions).where(sessions.c.id==identity).values(document_id=doc));row['document_id']=doc
        log(c,actor,row,action,body,lifecycle='cancelled' if action=='cancel' else 'completed')
        if action=='complete' and body['bad']:
            from .exception_tasks import upsert
            upsert(c,actor,dict(object_type='inspection',object_id=identity,warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],
                rule='quality_defect',rule_version='m3-v1',instance=identity,type='quality',priority='high',impact_quantity=body['bad'],due_at=None,details=row['details'],requires_review=True),automatic=True)
        return row
    return command(engine,actor,'inspection.'+action+':'+identity,body,key,apply,lambda c:get(c,actor,identity))


def request_disposition(engine,actor,body,key):
    fields=('quantity',) if body['kind']=='scrap' else ('g','q','d')
    if any(type(body[f]) is not int or not 0<=body[f]<=1_000_000 for f in fields) or sum(body[f] for f in fields)<1:fail('INVALID_QUANTITY','处置量须为非负整数且合计大于零',422)
    require_role(actor,'admin','warehouse')
    def authorize(c):
        if body['kind']=='scrap':get(c,actor,body['session_id'])
        else:
            original=c.execute(select(db.documents).where(db.documents.c.id==body['original_id'],db.documents.c.tenant_id==actor['tenant_id'])).mappings().first()
            if not original or original['kind'] not in ('receipt','purchase_receipt'):fail('NOT_FOUND','供应商退货须关联有效收货单',404)
            scope(c,actor,original['warehouse_id'],original['sku_id'])
    def apply(c):
        old=c.execute(select(dispositions).where(dispositions.c.tenant_id==actor['tenant_id'],dispositions.c.kind==body['kind'],dispositions.c.reference==body['reference'])).mappings().first()
        if old:
            canonical=lambda b:{k:v for k,v in b.items() if k not in ('expected_balance_version','expected_version')}
            if canonical(old['body'])!=canonical(body):fail('VOUCHER_CONFLICT','处置凭证已用于其他内容')
            return {**dict(old),'business_replayed':True}
        if body['kind']=='scrap':
            source=get(c,actor,body['session_id'])
            if source['status']!='completed' or body['quantity']>source['bad']-source['disposed']:fail('OVER_DISPOSITION','超过已检残次可处置量')
            quantity=body['quantity']
        else:
            source=dict(c.execute(select(db.documents).where(db.documents.c.id==body['original_id'])).mappings().one())
            quantity=sum(body[f] for f in ('g','q','d'))
            from . import operations, workflows
            if c.execute(select(workflows.reversals.c.original_id).where(workflows.reversals.c.original_id==source['id'])).first():
                fail('ALREADY_REVERSED','原收货单已冲正，不能据此退货')
            returned=c.scalar(select(func.coalesce(func.sum(operations.returns.c.quantity),0)).where(operations.returns.c.original_id==source['id'],operations.returns.c.tenant_id==actor['tenant_id']))
            if returned+quantity>source['quantity']:fail('OVER_RETURN','累计退货超过原收货数量')
        assert_unlocked(c,actor,source['warehouse_id'],source['sku_id'])
        stock=inv.load_balance(c,actor,source['warehouse_id'],source['sku_id'])
        if stock['version']!=body['expected_balance_version']:fail('VERSION_CONFLICT','库存已变化')
        row=dict(id=uid(),tenant_id=actor['tenant_id'],warehouse_id=source['warehouse_id'],sku_id=source['sku_id'],kind=body['kind'],
            reference=body['reference'],session_id=body.get('session_id'),original_id=body.get('original_id'),quantity=quantity,body=body,details=body['details'],
            status='awaiting_review',version=1,requester_id=actor['id'],reviewer_id=None,document_id=None,created_at=now())
        c.execute(insert(dispositions).values(**row));log(c,actor,row,'requested',body,kind='quality_disposition');return row
    return command(engine,actor,'disposition.create',body,key,apply,authorize)


def review(engine,actor,identity,body,key):
    require_role(actor,'admin')
    def apply(c):
        row=get(c,actor,identity,dispositions);version(row,body)
        if row['status']!='awaiting_review':fail('INVALID_STATE','处置单已审核')
        if row['requester_id']==actor['id']:fail('SELF_APPROVAL','处置须由另一管理员审核',403)
        doc=None
        if body['decision']=='approve':
            payload=row['body'];stock=inv.load_balance(c,actor,row['warehouse_id'],row['sku_id'])
            if row['kind']=='scrap':
                batch=get(c,actor,row['session_id'])
                if row['quantity']>batch['bad']-batch['disposed']:fail('OVER_DISPOSITION','可报废数量已变化')
                c.execute(update(sessions).where(sessions.c.id==batch['id']).values(disposed=batch['disposed']+row['quantity'],version=batch['version']+1))
                doc=inv.movement(c,actor,stock,{'d':-row['quantity']},'quality_scrap',identity,row['details']['opinion'],payload['expected_balance_version'],business_reference=row['reference'])
            else:
                from .operations import apply_return
                legacy=dict(kind='supplier',original_id=row['original_id'],reference=row['reference'],expected_version=payload['expected_balance_version'],
                    g=payload['g'],q=payload['q'],d=payload['d'],reason=row['details']['opinion'],details=row['details'])
                result=voucher(c,actor,'return.supplier',legacy,lambda:apply_return(c,actor,legacy))
                doc=result['document_id']
        changes=dict(status='approved' if body['decision']=='approve' else 'rejected',version=row['version']+1,reviewer_id=actor['id'],document_id=doc)
        c.execute(update(dispositions).where(dispositions.c.id==identity).values(**changes));row.update(changes)
        log(c,actor,row,row['status'],body,kind='quality_disposition',lifecycle='completed');return row
    return command(engine,actor,'disposition.review:'+identity,body,key,apply,lambda c:get(c,actor,identity,dispositions))


def listing(engine,actor,table=sessions,offset=0,limit=50):
    require_role(actor,'admin','warehouse')
    with engine.connect() as c:
        read_actor(c,actor);query=select(table).where(table.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(table.c.warehouse_id.in_(actor['warehouse_ids']))
        rows=c.execute(query.order_by(table.c.created_at.desc(),table.c.id).offset(offset).limit(limit+1)).mappings().all()
        items=[]
        for x in rows[:limit]:
            item=dict(x);item['history']=[dict(y) for y in c.execute(select(history).where(history.c.object_id==x['id'],history.c.tenant_id==actor['tenant_id']).order_by(history.c.created_at,history.c.id)).mappings()];items.append(item)
        return {'items':items,'has_more':len(rows)>limit,'offset':offset,'limit':limit}
