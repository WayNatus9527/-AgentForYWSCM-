"""Purchase commitments and independently received transport batches."""
from sqlalchemy import select, insert, update, func, or_
from . import db, workflows as w, operations as op, inventory as inv, m5_schema as s, m5_models as m
from .domain import uid, now, require_role, get_warehouse
from .clock import business_now, parse_timestamp
from .m5_common import scope, supplier, command, voucher, validate, version, fail, record, history, anomaly, READ, WRITE
from .accounts import assert_current


def managed(c, kind, identity):
    return bool(c.scalar(select(s.lines.c.id).where(s.lines.c.kind==kind,s.lines.c.order_id==identity).limit(1)))

def get_order(c, actor, kind, identity, side=None):
    require_role(actor,*READ); assert_current(c,actor)
    row = w.get_order(c,actor,identity) if kind=='purchase' else op.get_transfer(c,actor,identity,side)
    if not managed(c,kind,identity): fail('LEGACY_ORDER', '此单保留原流程，请从原采购或调拨入口处理')
    return row

def get_line(c, actor, identity, side=None):
    row=c.execute(select(s.lines).where(s.lines.c.id==identity,s.lines.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND','供给行不存在或未授权',404)
    get_order(c,actor,row['kind'],row['order_id'],side)
    return dict(row)

def get_batch(c, actor, identity, side=None):
    row=c.execute(select(s.batches).where(s.batches.c.id==identity,s.batches.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND','运输批次不存在或未授权',404)
    get_line(c,actor,row['supply_line_id'],side)
    return dict(row)

def get_receipt(c,actor,identity):
    row=c.execute(select(s.receipts).where(s.receipts.c.id==identity,s.receipts.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND','到货凭证不存在或未授权',404)
    scope(c,actor,row['warehouse_id'],row['sku_id']);return dict(row)

def batch_rows(c,line_id):
    return [dict(x) for x in c.execute(select(s.batches).where(s.batches.c.supply_line_id==line_id).order_by(s.batches.c.created_at,s.batches.c.id)).mappings()]

def check_linked_capacity(c,line,capacity):
    from .m4_schema import links
    kind='purchase_line' if line['kind']=='purchase' else 'transfer_line'
    linked=c.scalar(select(func.coalesce(func.sum(links.c.quantity-links.c.released),0)).where(links.c.tenant_id==line['tenant_id'],links.c.source_type==kind,links.c.source_id==line['source_line_id']))
    if linked>capacity:fail('SUPPLY_LINKED','余量仍关联销售需求，请先核实并解除超出供给量的关联')

def inspection_rows(c,receipt_id):
    from .quality_control import sessions
    return [dict(x) for x in c.execute(select(sessions).join(s.inspections,s.inspections.c.session_id==sessions.c.id)
        .where(s.inspections.c.receipt_id==receipt_id,sessions.c.status!='cancelled')).mappings()]

def pending_inspection(c,receipt):
    return receipt['q']-sum(x['quantity'] for x in inspection_rows(c,receipt['id']))

def quality_hold(c,tenant,warehouse,sku):
    return sum(pending_inspection(c,r) for r in c.execute(select(s.receipts).where(s.receipts.c.tenant_id==tenant,
        s.receipts.c.warehouse_id==warehouse,s.receipts.c.sku_id==sku)).mappings())

def view(c,actor,kind,identity):
    order=get_order(c,actor,kind,identity)
    order['kind']=kind;order['managed']=True
    order['terms']=dict(c.execute(select(s.terms).where(s.terms.c.order_id==identity)).mappings().one()) if kind=='purchase' else None
    order['history']=history(c,kind,identity);order['lines']=[]
    for line in c.execute(select(s.lines).where(s.lines.c.order_id==identity,s.lines.c.kind==kind).order_by(s.lines.c.id)).mappings():
        row=dict(line);row['batches']=[]
        for b in batch_rows(c,line['id']):
            b['in_transit']=b['sent']-b['received']-b['lost'];b['history']=history(c,'supply_batch',b['id'])
            b['receipts']=[]
            for receipt in c.execute(select(s.receipts).where(s.receipts.c.batch_id==b['id']).order_by(s.receipts.c.created_at)).mappings():
                receipt=dict(receipt);receipt['inspections']=inspection_rows(c,receipt['id']);receipt['uninspected']=pending_inspection(c,receipt)
                receipt['pending_quality']=receipt['q']-sum(x['bad']+x['restocked'] for x in receipt['inspections'] if x['status']=='completed')
                receipt['became_available']=receipt['g']+sum(x['restocked'] for x in receipt['inspections'])
                receipt['returned']=c.scalar(select(func.coalesce(func.sum(op.returns.c.quantity),0)).where(op.returns.c.original_id==receipt['document_id']))
                b['receipts'].append(receipt)
            b['claims']=[dict(x) for x in c.execute(select(s.claims).where(s.claims.c.batch_id==b['id'])).mappings()]
            row['batches'].append(b)
        active=[b for b in row['batches'] if b['status']!='cancelled']
        row.update(reserved=sum(b['reserved'] for b in active),sent=sum(b['sent'] for b in active),received=sum(b['received'] for b in active),
            in_transit=sum(b['in_transit'] for b in active),unbatched=row['quantity']-row['closed_quantity']-sum(b['quantity'] for b in active))
        row['pending_quality']=sum(r['pending_quality'] for b in active for r in b['receipts'])
        row['became_available']=sum(r['became_available'] for b in active for r in b['receipts'])
        order['lines'].append(row)
    return order

def listing(engine,actor,kind,offset=0,limit=20):
    require_role(actor,*READ)
    with engine.connect() as c:
        assert_current(c,actor);table=w.orders if kind=='purchase' else op.transfers
        query=select(table.c.id).where(table.c.tenant_id==actor['tenant_id'],table.c.id.in_(select(s.lines.c.order_id).where(s.lines.c.kind==kind)))
        if actor['role']!='admin':
            clause=table.c.warehouse_id.in_(actor['warehouse_ids'])
            if kind=='transfer':clause=or_(clause,table.c.destination_id.in_(actor['warehouse_ids']))
            query=query.where(clause)
        ids=list(c.execute(query.order_by(table.c.created_at.desc(),table.c.id).offset(offset).limit(limit+1)).scalars())
        return dict(items=[view(c,actor,kind,x) for x in ids[:limit]],offset=offset,limit=limit,has_more=len(ids)>limit)

def create_in_transaction(c,actor,kind,body):
    if kind=='purchase':
        source=supplier(c,actor,body['supplier_id'])
        for line in body['lines']:supplier(c,actor,body['supplier_id'],line['sku_id'])
        replacement=body.get('replacement_return_id')
        if replacement:
            returned=c.execute(select(op.returns).where(op.returns.c.id==replacement,op.returns.c.tenant_id==actor['tenant_id'],op.returns.c.kind=='supplier',op.returns.c.status=='completed')).mappings().first()
            if not returned:fail('NOT_FOUND','已执行的供应商退货单不存在或未授权',404)
            scope(c,actor,returned['warehouse_id'],returned['sku_id'])
            receipt=c.execute(select(s.receipts).where(s.receipts.c.document_id==returned['original_id'],s.receipts.c.tenant_id==actor['tenant_id'])).mappings().first()
            if not receipt:fail('REPLACEMENT_SOURCE_REQUIRED','补发须关联此采购计划流程产生的已执行退货',422)
            original=get_line(c,actor,receipt['supply_line_id'])
            terms=c.execute(select(s.terms).where(s.terms.c.order_id==original['order_id'])).mappings().one()
            if body['supplier_id']!=terms['supplier_id'] or body['warehouse_id']!=returned['warehouse_id'] or len(body['lines'])!=1 or body['lines'][0]['sku_id']!=returned['sku_id']:
                fail('REPLACEMENT_MISMATCH','补发供应商、收货仓库和商品须与原退货一致',422)
            allocated=c.scalar(select(func.coalesce(func.sum(w.lines.c.ordered),0)).join(s.terms,s.terms.c.order_id==w.lines.c.order_id).where(s.terms.c.replacement_return_id==replacement,s.terms.c.tenant_id==actor['tenant_id']))
            if allocated+body['lines'][0]['quantity']>returned['quantity']:fail('OVER_REPLACEMENT','累计补发计划不得超过已执行退货数量')
        row=w.purchase_create_in_transaction(c,actor,{k:body[k] for k in ('warehouse_id','code','note','lines')}|{'supplier':source['name']})
        c.execute(insert(s.terms).values(id=uid(),tenant_id=actor['tenant_id'],order_id=row['id'],supplier_id=source['id'],
            replacement_return_id=replacement,
            ordered_at=body['ordered_at'],original_due=body['promised_at'],latest_due=body['promised_at'],fingerprint=inv.fingerprint(body)))
        destination=body['warehouse_id'];origin=None;due=body['promised_at']
    else:
        scope(c,actor,body['destination_id'])
        row=op.transfer_create_in_transaction(c,actor,{k:v for k,v in body.items() if k!='expected_at'})
        destination=body['destination_id'];origin=body['warehouse_id'];due=body['expected_at']
    for line in row['lines']:
        c.execute(insert(s.lines).values(id=uid(),tenant_id=actor['tenant_id'],kind=kind,order_id=row['id'],source_line_id=line['id'],
            warehouse_id=destination,source_warehouse_id=origin,sku_id=line['sku_id'],quantity=line['ordered'] if kind=='purchase' else line['quantity'],
            closed_quantity=0,expected_at=due,created_at=now()))
    record(c,actor,kind,row,'created',body,lifecycle='queued',observed=False,occurred_at=body.get('ordered_at'),quantity=sum(x['quantity'] for x in body['lines']))
    return view(c,actor,kind,row['id'])

def create(engine,actor,kind,body,key):
    require_role(actor,*WRITE);body=validate(m.Purchase if kind=='purchase' else m.Transfer,body)
    def authorize(c):
        for line in body['lines']:scope(c,actor,body['warehouse_id'],line['sku_id'])
        if kind=='transfer':scope(c,actor,body['destination_id'])
    return command(engine,actor,kind+'.create',body,key,
        lambda c:voucher(c,actor,kind+'.create',body['code'],body,lambda:create_in_transaction(c,actor,kind,body)),authorize)

def refresh(c,actor,kind,identity):
    table=w.orders if kind=='purchase' else op.transfers;row=get_order(c,actor,kind,identity)
    lines=list(c.execute(select(s.lines).where(s.lines.c.order_id==identity,s.lines.c.kind==kind)).mappings())
    batches=[b for line in lines for b in batch_rows(c,line['id']) if b['status']!='cancelled']
    quantity=sum(x['quantity']-x['closed_quantity'] for x in lines);sent=sum(x['sent'] for x in batches)
    received=sum(x['received'] for x in batches);lost=sum(x['lost'] for x in batches);reserved=sum(x['reserved'] for x in batches)
    if received+lost==quantity:status='closed_difference' if kind=='transfer' and lost else ('closed' if any(x['closed_quantity'] for x in lines) else 'completed')
    elif kind=='purchase':status='partial' if received else 'open'
    elif sent:status='partial' if received+lost else 'in_transit'
    else:status='reserved' if reserved else 'draft'
    c.execute(update(table).where(table.c.id==identity).values(status=status,version=row['version']+1))

def update_promise(engine,actor,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Promise,body)
    def apply(c):
        order=get_order(c,actor,'purchase',identity);version(order,body)
        if order['status'] not in ('open','partial'):fail('ORDER_CLOSED','采购单已结束')
        old=c.execute(select(s.terms).where(s.terms.c.order_id==identity)).mappings().one()
        c.execute(update(s.terms).where(s.terms.c.order_id==identity).values(latest_due=body['promised_at']))
        c.execute(update(s.lines).where(s.lines.c.order_id==identity,s.lines.c.kind=='purchase').values(expected_at=body['promised_at']))
        c.execute(update(w.orders).where(w.orders.c.id==identity).values(version=order['version']+1))
        record(c,actor,'purchase',order,'promise_changed',{**body,'original_due':old['original_due'],'previous_due':old['latest_due']})
        sync_delay(c,actor,identity);return view(c,actor,'purchase',identity)
    return command(engine,actor,'purchase.promise:'+identity,body,key,lambda c:voucher(c,actor,'purchase.promise:'+identity,body['reference'],body,lambda:apply(c)),lambda c:get_order(c,actor,'purchase',identity))

def make_batch(engine,actor,kind,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Batch,body)
    def authorize(c):
        get_order(c,actor,kind,identity,'source' if kind=='transfer' else None)
        line=get_line(c,actor,body['supply_line_id'],'source' if kind=='transfer' else None)
        if line['kind']!=kind or line['order_id']!=identity:fail('NOT_FOUND','供给行不属于此单',404)
    def apply(c):
        order=get_order(c,actor,kind,identity);version(order,body)
        line=get_line(c,actor,body['supply_line_id']);active=[b for b in batch_rows(c,line['id']) if b['status']!='cancelled']
        if c.scalar(select(s.batches.c.id).where(s.batches.c.tenant_id==actor['tenant_id'],s.batches.c.kind==kind,s.batches.c.reference==body['reference'])):
            fail('BATCH_REFERENCE_USED','批次凭证号已被其他批次使用')
        if body['quantity']>line['quantity']-line['closed_quantity']-sum(b['quantity'] for b in active):fail('OVER_BATCH','超过计划未分批量')
        batch={k:body[k] for k in ('reference','quantity','carrier','transport_mode','tracking_number','planned_ship_at','expected_at')}
        batch.update(id=uid(),tenant_id=actor['tenant_id'],kind=kind,order_id=identity,supply_line_id=line['id'],warehouse_id=line['warehouse_id'],
            source_warehouse_id=line['source_warehouse_id'],sku_id=line['sku_id'],reserved=body['quantity'] if kind=='transfer' else 0,
            sent=0,received=0,lost=0,status='reserved' if kind=='transfer' else 'planned',version=1,shipped_at=None,arrived_at=None,created_at=now())
        if kind=='transfer':
            from .fulfillment import stock_known
            from . import catalog
            if c.scalar(select(catalog.sku_settings.c.active).where(catalog.sku_settings.c.tenant_id==actor['tenant_id'],catalog.sku_settings.c.sku_id==line['sku_id']))==0:
                fail('SKU_INACTIVE','已停用商品不可新增预占')
            if c.scalar(select(catalog.assortments.c.active).where(catalog.assortments.c.tenant_id==actor['tenant_id'],catalog.assortments.c.warehouse_id==line['source_warehouse_id'],catalog.assortments.c.sku_id==line['sku_id']))==0:
                fail('ASSORTMENT_INACTIVE','来源仓库商品经营关系已停用')
            if not stock_known(c,actor,line['source_warehouse_id'],line['sku_id']):fail('INVENTORY_UNKNOWN','来源库存基准未知，不能预占')
            stock=inv.load_balance(c,actor,line['source_warehouse_id'],line['sku_id'])
            inv.movement(c,actor,stock,{'t':body['quantity']},'transfer_reserve',identity,body['reason'],body['expected_balance_version'],business_reference=body['reference'])
        c.execute(insert(s.batches).values(**batch));refresh(c,actor,kind,identity)
        record(c,actor,'supply_batch',batch,'reserved' if kind=='transfer' else 'planned',body,quantity=body['quantity'],lifecycle='queued')
        return view(c,actor,kind,identity)
    return command(engine,actor,'batch.create:'+identity,body,key,lambda c:voucher(c,actor,'batch.create:'+identity,body['reference'],body,lambda:apply(c)),authorize)

def batch_action(engine,actor,identity,action,body,key):
    require_role(actor,*WRITE)
    body=validate({'ship':m.BatchShip,'receive':m.BatchReceive,'cancel':m.BatchCancel}[action],body)
    side='destination' if action=='receive' else 'source'
    def apply(c):
        row=get_batch(c,actor,identity,side);version(row,body);line=get_line(c,actor,row['supply_line_id'])
        doc=None
        if action=='ship':
            if row['status'] not in ('planned','reserved'):fail('INVALID_STATE','仅未发批次可以发出')
            if row['kind']=='transfer':
                stock=inv.load_balance(c,actor,row['source_warehouse_id'],row['sku_id'])
                doc=inv.movement(c,actor,stock,{'t':-row['reserved'],'g':-row['quantity']},'transfer_ship',row['order_id'],body['reason'],body['expected_balance_version'],business_reference=body['reference'])
                original=c.execute(select(op.transfer_lines).where(op.transfer_lines.c.id==line['source_line_id'])).mappings().one()
                c.execute(update(op.transfer_lines).where(op.transfer_lines.c.id==original['id']).values(sent=original['sent']+row['quantity']))
            row.update(reserved=0,sent=row['quantity'],status='in_transit',shipped_at=body['occurred_at'])
        elif action=='receive':
            if row['status'] not in ('in_transit','partial'):fail('INVALID_STATE','批次不在待签收状态')
            n=body['g']+body['q']+body['d']
            if n>row['sent']-row['received']-row['lost']:fail('OVER_RECEIPT','签收超过本批剩余在途')
            stock=inv.load_balance(c,actor,row['warehouse_id'],row['sku_id'])
            doc=inv.movement(c,actor,stock,{k:body[k] for k in ('g','q','d')},'purchase_receipt' if row['kind']=='purchase' else 'transfer_receive',
                row['order_id'],body['reason'],body['expected_balance_version'],business_reference=body['reference'],occurred_at=body['occurred_at'])
            receipt=dict(id=uid(),tenant_id=actor['tenant_id'],batch_id=identity,supply_line_id=line['id'],warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],
                reference=body['reference'],document_id=doc,occurred_at=body['occurred_at'],created_at=now(),**{k:body[k] for k in ('g','q','d')})
            c.execute(insert(s.receipts).values(**receipt))
            table=w.lines if row['kind']=='purchase' else op.transfer_lines
            original=c.execute(select(table).where(table.c.id==line['source_line_id'])).mappings().one()
            c.execute(update(table).where(table.c.id==original['id']).values(received=original['received']+n))
            if row['kind']=='purchase':
                c.execute(insert(w.receipts).values(id=uid(),tenant_id=actor['tenant_id'],order_id=row['order_id'],reference='m5:'+receipt['id'],document_ids=[doc],created_at=now()))
            row.update(received=row['received']+n,arrived_at=body['occurred_at'])
            row['status']='received' if row['received']+row['lost']==row['sent'] else 'partial'
        else:
            if row['sent'] or row['status']=='cancelled':fail('INVALID_STATE','仅未发批次可以取消')
            if row['reserved']:
                stock=inv.load_balance(c,actor,row['source_warehouse_id'],row['sku_id'])
                doc=inv.movement(c,actor,stock,{'t':-row['reserved']},'transfer_cancel',row['order_id'],body['reason'],body['expected_balance_version'],business_reference=body['reference'])
            row.update(status='cancelled',reserved=0)
        row['version']+=1;c.execute(update(s.batches).where(s.batches.c.id==identity).values(**row))
        if row['kind']=='transfer':op.event(c,actor,row['order_id'],action,'m5:'+identity+':'+body['reference'],[{'batch_id':identity,**body}],[] if doc is None else [doc])
        record(c,actor,'supply_batch',row,action,{**body,'document_id':doc},quantity=(body['g']+body['q']+body['d']) if action=='receive' else row['quantity'],
            lifecycle='cancelled' if action=='cancel' else 'completed' if action=='receive' else 'started',observed=action=='cancel',occurred_at=body.get('occurred_at'))
        refresh(c,actor,row['kind'],row['order_id'])
        if row['kind']=='purchase':sync_delay(c,actor,row['order_id'])
        return view(c,actor,row['kind'],row['order_id'])
    return command(engine,actor,'batch.'+action+':'+identity,body,key,
        lambda c:voucher(c,actor,'batch.'+action+':'+identity,body['reference'],body,lambda:apply(c)),lambda c:get_batch(c,actor,identity,side))

def close(engine,actor,kind,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Action,body)
    def apply(c):
        row=get_order(c,actor,kind,identity,'source' if kind=='transfer' else None);version(row,body)
        all_lines=list(c.execute(select(s.lines).where(s.lines.c.order_id==identity,s.lines.c.kind==kind)).mappings())
        changed=False;closed_quantities={}
        for line in all_lines:
            batches=[b for b in batch_rows(c,line['id']) if b['status']!='cancelled']
            if any(b['reserved'] or not b['sent'] for b in batches):fail('PENDING_BATCH','请先取消未发批次，再关闭未分批余量')
            closed=line['quantity']-sum(b['quantity'] for b in batches)
            changed=changed or closed>line['closed_quantity']
            check_linked_capacity(c,line,line['quantity']-closed-sum(b['lost'] for b in batches))
            c.execute(update(s.lines).where(s.lines.c.id==line['id']).values(closed_quantity=closed))
            closed_quantities[line['id']]=closed
        if not changed:fail('ORDER_CLOSED','没有可关闭的未分批余量')
        refresh(c,actor,kind,identity)
        if kind=='purchase':c.execute(update(w.orders).where(w.orders.c.id==identity).values(close_reason=body['reason']))
        record(c,actor,kind,row,'remaining_closed',{**body,'closed_quantities':closed_quantities},lifecycle='cancelled')
        if kind=='purchase':sync_delay(c,actor,identity)
        return view(c,actor,kind,identity)
    return command(engine,actor,kind+'.close:'+identity,body,key,lambda c:voucher(c,actor,kind+'.close:'+identity,body['reference'],body,lambda:apply(c)),lambda c:get_order(c,actor,kind,identity,'source' if kind=='transfer' else None))

def sync_delay(c,actor,identity):
    from .exception_tasks import tasks
    terms=c.execute(select(s.terms).where(s.terms.c.order_id==identity)).mappings().one()
    current=parse_timestamp(business_now(c.engine))
    for line in c.execute(select(s.lines).where(s.lines.c.order_id==identity,s.lines.c.kind=='purchase')).mappings():
        received=sum(b['received'] for b in batch_rows(c,line['id']));remaining=line['quantity']-line['closed_quantity']-received
        overdue=bool(terms['latest_due'] and parse_timestamp(terms['latest_due'])<current and remaining)
        exists=c.scalar(select(tasks.c.id).where(tasks.c.tenant_id==actor['tenant_id'],tasks.c.object_type=='supply_line',tasks.c.object_id==line['id'],tasks.c.rule=='purchase_late'))
        if overdue or exists:anomaly(c,actor,line,'purchase_late',remaining if overdue else 0,due_at=terms['latest_due'])

def scan_delays(engine,actor,key):
    require_role(actor,*WRITE)
    def apply(c):
        query=select(w.orders.c.id).join(s.terms,s.terms.c.order_id==w.orders.c.id).where(w.orders.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(w.orders.c.warehouse_id.in_(actor['warehouse_ids']))
        ids=list(c.execute(query).scalars())
        for identity in ids:sync_delay(c,actor,identity)
        return {'checked':len(ids),'as_of':business_now(engine)}
    return command(engine,actor,'purchase.scan-delays',{},key,apply,lambda c:assert_current(c,actor))

def inspect(engine,actor,receipt_id,body,key):
    require_role(actor,*WRITE);body=validate(m.Inspect,body)
    def apply(c):
        from .quality_control import start_in_transaction
        receipt=get_receipt(c,actor,receipt_id)
        return start_in_transaction(c,actor,{**body,'warehouse_id':receipt['warehouse_id'],'sku_id':receipt['sku_id'],'return_id':None},receipt=receipt)
    return command(engine,actor,'receipt.inspect:'+receipt_id,body,key,apply,lambda c:get_receipt(c,actor,receipt_id))

def discrepancy(engine,actor,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Claim,body)
    def apply(c):
        batch=get_batch(c,actor,identity,'destination');version(batch,body)
        if batch['kind']!='transfer':fail('INVALID_KIND','此差异入口用于调拨运输批次')
        if body['quantity']>batch['sent']-batch['received']-batch['lost']:fail('OVER_LOSS','差异量超过未签收在途')
        if body['kind']=='damaged':fail('RECEIVE_DAMAGED','已到货破损品请按残次签收；未到实物使用短少或拒收处置',422)
        if c.scalar(select(s.claims.c.id).where(s.claims.c.batch_id==identity,s.claims.c.status=='pending')):fail('PENDING_CLAIM','此批次已有待审差异')
        row=dict(id=uid(),tenant_id=actor['tenant_id'],batch_id=identity,warehouse_id=batch['warehouse_id'],sku_id=batch['sku_id'],reference=body['reference'],
            quantity=body['quantity'],kind=body['kind'],details=body['details'],status='pending',version=1,batch_version=batch['version'],requester_id=actor['id'],reviewer_id=None,created_at=now())
        c.execute(insert(s.claims).values(**row));record(c,actor,'supply_discrepancy',row,'requested',body,quantity=row['quantity'],lifecycle='queued');return row
    return command(engine,actor,'batch.discrepancy:'+identity,body,key,lambda c:voucher(c,actor,'batch.discrepancy:'+identity,body['reference'],body,lambda:apply(c)),lambda c:get_batch(c,actor,identity,'destination'))

def review(engine,actor,identity,body,key):
    require_role(actor,'admin');body=validate(m.Review,body)
    def get(c):
        row=c.execute(select(s.claims).where(s.claims.c.id==identity,s.claims.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not row:fail('NOT_FOUND','运输差异申请不存在',404)
        scope(c,actor,row['warehouse_id'],row['sku_id']);return dict(row)
    def apply(c):
        row=get(c);version(row,body)
        if row['requester_id']==actor['id']:fail('SELF_APPROVAL','差异申请须另一管理员复核',403)
        if row['status']!='pending':fail('ALREADY_REVIEWED','申请已处理')
        batch=get_batch(c,actor,row['batch_id'])
        if body['decision']=='approve':
            if batch['version']!=row['batch_version']:fail('VERSION_CONFLICT','批次已有新签收或处置，请驳回后重新核对')
            if row['quantity']>batch['sent']-batch['received']-batch['lost']:fail('OVER_LOSS','已超剩余在途')
            line=get_line(c,actor,batch['supply_line_id'])
            check_linked_capacity(c,line,line['quantity']-line['closed_quantity']-sum(b['lost'] for b in batch_rows(c,line['id']))-row['quantity'])
            batch['lost']+=row['quantity'];batch['version']+=1
            batch['status']='closed_difference' if batch['received']+batch['lost']==batch['sent'] else 'partial'
            c.execute(update(s.batches).where(s.batches.c.id==batch['id']).values(**batch))
            line=get_line(c,actor,batch['supply_line_id']);old=c.execute(select(op.transfer_lines).where(op.transfer_lines.c.id==line['source_line_id'])).mappings().one()
            c.execute(update(op.transfer_lines).where(op.transfer_lines.c.id==old['id']).values(lost=old['lost']+row['quantity']))
            op.event(c,actor,batch['order_id'],'loss',identity,[{'batch_id':batch['id'],'quantity':row['quantity'],'kind':row['kind']}],[])
            refresh(c,actor,'transfer',batch['order_id']);record(c,actor,'supply_batch',batch,'loss_approved',body|{'claim_id':identity},quantity=row['quantity'],lifecycle='completed')
        row.update(status='approved' if body['decision']=='approve' else 'rejected',version=row['version']+1,reviewer_id=actor['id'])
        c.execute(update(s.claims).where(s.claims.c.id==identity).values(**row));record(c,actor,'supply_discrepancy',row,row['status'],body,quantity=row['quantity'],lifecycle='completed');return row
    return command(engine,actor,'discrepancy.review:'+identity,body,key,lambda c:voucher(c,actor,'discrepancy.review:'+identity,body['reference'],body,lambda:apply(c)),get)
