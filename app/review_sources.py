"""Authorized operational evidence adapters. Never import simulator or private truth."""
from collections import defaultdict
from sqlalchemy import select
from . import db,events,m4_schema as sales,m5_schema as supply,quality_control as qc,operations as op
from .clock import parse_timestamp
from .domain import get_warehouse


def normalize(raw):
    """Resolve append-only correction chains; forks are ambiguous, never arbitrary."""
    children=defaultdict(list);originals=[]
    for e in raw:
        if e['correction_of']:children[e['correction_of']].append(e)
        else:originals.append(e)
    result=[]
    for root in originals:
        head=root;chain=[root['id']];fork=False
        while children[head['id']]:
            if len(children[head['id']])!=1:fork=True;break
            head=children[head['id']][0];chain.append(head['id'])
        row=dict(root)
        if head is not root:
            row.update({k:head[k] for k in ('occurred_at','planned_at','time_quality')})
            # A later correction is not a later physical registration of the original.
            row['quality_flags']=[]
            if row['occurred_at'] and parse_timestamp(row['occurred_at'])>parse_timestamp(root['received_at']):row['quality_flags'].append('occurred_after_received')
            if parse_timestamp(root['received_at'])>parse_timestamp(root['registered_at']):row['quality_flags'].append('received_after_registered')
        if fork:row['quality_flags']=[*row['quality_flags'],'ambiguous_correction']
        row.update(evidence_ids=chain,effective_id=head['id']);result.append(row)
    return result


def capture(c,actor,body):
    warehouse=body['warehouse_id'];sku=body.get('sku_id');scope_connections={};allowed_orders={};hidden=False
    accounts={x['id']:dict(x) for x in c.execute(select(sales.connections).where(sales.connections.c.tenant_id==actor['tenant_id'])).mappings()}
    visible_warehouses={x['id'] for x in c.execute(select(db.warehouses.c.id).where(db.warehouses.c.tenant_id==actor['tenant_id'])).mappings()
        if actor['role']=='admin' or x['id'] in actor['warehouse_ids']}
    for row in c.execute(select(sales.orders).where(sales.orders.c.tenant_id==actor['tenant_id'])).mappings():
        if warehouse not in row['warehouse_ids'] or body.get('connection_id') and row['connection_id']!=body['connection_id']:continue
        a=accounts.get(row['connection_id'])
        if not a or not a['active'] or actor['role']!='admin' and actor['id'] not in a['member_ids'] or not set(row['warehouse_ids'])<=visible_warehouses or not set(row['warehouse_ids'])<=set(a['warehouse_ids']):
            hidden=True;continue
        allowed_orders[row['id']]=dict(row)
        scope_connections.setdefault(a['id'],set()).update(row['warehouse_ids'])
    allowed=set(allowed_orders)
    def rows(table,scoped=True):
        query=select(table).where(table.c.tenant_id==actor['tenant_id'])
        if scoped:query=query.where(table.c.warehouse_id==warehouse)
        if sku and 'sku_id' in table.c:query=query.where(table.c.sku_id==sku)
        return [dict(x) for x in c.execute(query.order_by(table.c.id)).mappings()]
    data=dict(orders=sorted(allowed_orders.values(),key=lambda x:x['id']),lines=[x for x in rows(sales.lines,False) if x['order_id'] in allowed],
        sales_batches=[x for x in rows(sales.batches) if x['order_id'] in allowed],tasks=[x for x in rows(sales.tasks,False) if x['order_id'] in allowed],
        links=[x for x in rows(sales.links) if x['order_id'] in allowed],shipments=[x for x in rows(sales.shipments) if x['order_id'] in allowed],
        supply_lines=rows(supply.lines),supply_batches=rows(supply.batches),receipts=rows(supply.receipts),inspections=rows(qc.sessions),
        dispositions=rows(qc.dispositions),returns=rows(op.returns),terms=rows(supply.terms,False),inspection_links=rows(supply.inspections,False),balances=rows(db.balances))
    purchase_ids={x['order_id'] for x in data['supply_lines'] if x['kind']=='purchase'}
    receipt_ids={x['id'] for x in data['receipts']};inspection_ids={x['id'] for x in data['inspections']}
    data['terms']=[x for x in data['terms'] if x['order_id'] in purchase_ids]
    data['inspection_links']=[x for x in data['inspection_links'] if x['receipt_id'] in receipt_ids and x['session_id'] in inspection_ids]
    known={'inspection','quality_disposition','supply_batch','purchase','transfer','return','stock_document','stock_count','quality'}
    raw=[]
    for r in c.execute(select(events.business_events).where(events.business_events.c.tenant_id==actor['tenant_id']).order_by(events.business_events.c.registered_at,events.business_events.c.id)).mappings():
        if r['object_type']=='sales_order':
            if r['object_id'] not in allowed:continue
        elif r['object_type'] not in known or r['warehouse_id']!=warehouse:continue
        if sku and r['sku_id'] not in (None,sku):continue
        raw.append(dict(r))
    data['events']=normalize(raw)
    data['raw_events']=raw
    data['scope']=dict(warehouse_id=warehouse,connections=[dict(id=k,warehouses=sorted(v)) for k,v in sorted(scope_connections.items())])
    data['scope_incomplete']=hidden
    data['timezone']=get_warehouse(c,actor,warehouse)['timezone']
    data['event_hashes']=[(r['id'],r['content_hash']) for r in sorted(raw,key=lambda x:x['id'])]
    return data


def spans(data,body):
    result=[];by_object=defaultdict(list);by_task=defaultdict(list);by_batch=defaultdict(list);end=parse_timestamp(body['end_at'])
    for e in data['events']:
        by_object[(e['object_type'],e['object_id'])].append(e)
        if e['task_id']:by_task[e['task_id']].append(e)
        if e['batch_id']:by_batch[e['batch_id']].append(e)
    def occurred(e):return parse_timestamp(e['occurred_at'] if e['occurred_at'] and e['time_quality']!='unknown' and not e['quality_flags'] else e['registered_at'])
    def visible(e):return occurred(e)<=end
    def event(kind,identity,suffix,filter=None):
        found=[e for e in by_object[(kind,identity)] if visible(e) and e['event_type'].endswith('.'+suffix) and (filter is None or filter(e))]
        return sorted(found,key=lambda x:(x['registered_at'],x['payload'].get('expected_version',0),x['id']))[0] if found else None
    def time(e):return e['occurred_at'] if e and e['time_quality']!='unknown' else None
    def add(metric,obj,start,finish,quantity,*,completed=False,due=None,sources=None,extra=None):
        if quantity<=0:return
        start_at=time(start);finish_at=time(finish)
        # Unknown completed occurrences remain completed-but-unknown, not open work.
        if start_at and not start['quality_flags'] and parse_timestamp(start_at)>end:return
        if finish_at and not finish['quality_flags'] and parse_timestamp(finish_at)>end:finish=None;finish_at=None;completed=False
        if finish_at and parse_timestamp(finish_at)<parse_timestamp(body['start_at']):return
        supplier=obj.get('supplier_id');account=obj.get('connection_id')
        if body.get('supplier_id') and supplier!=body['supplier_id']:return
        row=dict(metric=metric,object_id=obj['id'],warehouse_id=body['warehouse_id'],sku_id=obj.get('sku_id'),
            supplier_id=supplier,connection_id=account,quantity=quantity,start_at=start_at,end_at=finish_at,completed=completed,
            planned_at=due,sources=sources or [],order_id=obj.get('order_id') if account else None,
            dependency_branch=obj.get('receipt_id',obj.get('batch_id') if account else None),
            event_ids=list(dict.fromkeys(x for e in (start,finish) if e for x in e['evidence_ids'])),
            flags=list(dict.fromkeys(x for e in (start,finish) if e for x in e['quality_flags'])))
        row.update(extra or {});result.append(row)
    lines={x['id']:x for x in data['supply_lines']};terms={x['order_id']:x for x in data['terms']};batches={x['id']:x for x in data['supply_batches']}
    receipts={x['id']:x for x in data['receipts']};inspections={x['id']:x for x in data['inspections']}
    inspection_sources={x['session_id']:receipts[x['receipt_id']] for x in data['inspection_links'] if x['receipt_id'] in receipts}
    def source(line):return dict(type='purchase_line' if line['kind']=='purchase' else 'transfer_line',id=line['source_line_id'])
    def context(receipt):
        batch=batches[receipt['batch_id']];line=lines[batch['supply_line_id']]
        return dict(receipt,receipt_id=receipt['id'],supplier_id=terms.get(line['order_id'],{}).get('supplier_id')),line,batch
    def arrival(receipt):return event('supply_batch',receipt['batch_id'],'receive',lambda e:e['payload'].get('document_id')==receipt['document_id'])
    for receipt in receipts.values():
        obj,line,batch=context(receipt);arrived=arrival(receipt);shipped=event('supply_batch',batch['id'],'ship');quantity=receipt['g']+receipt['q']+receipt['d']
        if not arrived:continue
        add('transit',obj,shipped,arrived,quantity,completed=True,due=batch['expected_at'],sources=[source(line)])
        if line['kind']=='purchase':
            t=terms[line['order_id']];changes=[e for e in by_object[('purchase',line['order_id'])] if e['event_type']=='purchase.promise_changed' and arrived and e['registered_at']<=arrived['registered_at']]
            latest=sorted(changes,key=lambda e:(e['registered_at'],e['payload'].get('expected_version',0),e['id']))[-1]['payload']['promised_at'] if changes else t['original_due']
            created=event('purchase',line['order_id'],'created')
            for metric,due in [('purchase_original',t['original_due']),('purchase_latest',latest)]:add(metric,obj,created,arrived,quantity,completed=True,due=due,sources=[source(line)])
        add('arrival_available',obj,arrived,arrived,receipt['g'],completed=True,sources=[source(line)])
        inspected=sum(inspections[x['session_id']]['quantity'] for x in data['inspection_links'] if x['receipt_id']==receipt['id'] and x['session_id'] in inspections and event('inspection',x['session_id'],'started') and not event('inspection',x['session_id'],'cancel'))
        add('quality_wait',obj,arrived,None,receipt['q']-inspected,sources=[source(line)])
        add('arrival_available',obj,arrived,None,receipt['q']-inspected,sources=[source(line)])
    for inspection in inspections.values():
        if event('inspection',inspection['id'],'cancel'):continue
        receipt=inspection_sources.get(inspection['id']);obj=dict(inspection);sources=[dict(type='inspection',id=inspection['id'])];queued=None
        if receipt:
            rec,line,batch=context(receipt);obj.update(supplier_id=rec['supplier_id'],receipt_id=receipt['id']);sources.append(source(line));queued=arrival(receipt)
        elif inspection['return_id']:queued=event('return',inspection['return_id'],'waiting_inspection')
        started=event('inspection',inspection['id'],'started');done=event('inspection',inspection['id'],'complete')
        if not started:continue
        add('quality_wait',obj,queued,started,inspection['quantity'],completed=True,sources=sources)
        add('quality_work',obj,started,done,inspection['quantity'],completed=done is not None,sources=sources)
        restocks=[e for e in by_object[('inspection',inspection['id'])] if visible(e) and e['event_type']=='inspection.restock']
        for e in restocks:
            add('arrival_available',obj,queued,e,e['payload']['quantity'],completed=True,sources=sources)
            add('restock_wait',obj,done,e,e['payload']['quantity'],completed=True,sources=sources)
        left=inspection['quantity']-(done['payload'].get('bad',0) if done else 0)-sum(e['payload']['quantity'] for e in restocks)
        add('arrival_available',obj,queued,None,left,sources=sources)
        if done:add('restock_wait',obj,done,None,left,sources=sources)
    for batch in batches.values():
        line=lines[batch['supply_line_id']];shipped=event('supply_batch',batch['id'],'ship')
        if not shipped:continue
        received=sum(x['g']+x['q']+x['d'] for x in receipts.values() if x['batch_id']==batch['id'] and arrival(x))
        lost=sum(e['quantity'] or 0 for e in by_object[('supply_batch',batch['id'])] if visible(e) and e['event_type']=='supply_batch.loss_approved')
        left=(shipped['quantity'] or batch['sent'])-received-lost
        add('transit',dict(batch,supplier_id=terms.get(line['order_id'],{}).get('supplier_id')),shipped,None,left,due=batch['expected_at'],sources=[source(line)])
    for line in lines.values():
        if line['kind']!='purchase':continue
        created=event('purchase',line['order_id'],'created')
        if not created:continue
        history=by_object[('purchase',line['order_id'])];t=terms[line['order_id']]
        changes=[e for e in history if visible(e) and e['event_type']=='purchase.promise_changed']
        latest=sorted(changes,key=lambda e:(e['registered_at'],e['payload'].get('expected_version',0),e['id']))[-1]['payload']['promised_at'] if changes else t['original_due']
        closures=sorted((e for e in history if e['event_type']=='purchase.remaining_closed'),key=lambda e:(e['registered_at'],e['payload'].get('expected_version',0),e['id']))
        visible_closures=[e for e in closures if visible(e)];closed=0;flags=[]
        if visible_closures:
            last=visible_closures[-1];snapshot=last['payload'].get('closed_quantities',{})
            if line['id'] in snapshot:closed=snapshot[line['id']]
            elif last['id']==closures[-1]['id']:closed=line['closed_quantity']
            else:
                # Old events lack per-line snapshots. Never apply a later close
                # to an earlier report or manufacture a historical quantity.
                flags=['missing_closed_quantity_history']
        received=sum(x['g']+x['q']+x['d'] for x in receipts.values() if x['supply_line_id']==line['id'] and arrival(x))
        obj=dict(line,supplier_id=t['supplier_id'])
        for metric,due in [('purchase_original',t['original_due']),('purchase_latest',latest)]:
            add(metric,obj,created,None,line['quantity']-closed-received,due=due,sources=[source(line)],extra=dict(quantity_known=not flags,flags=list(dict.fromkeys([*created['quality_flags'],*flags]))))
    for d in data['dispositions']:
        done=event('quality_disposition',d['id'],'approved') or event('quality_disposition',d['id'],'rejected')
        requested=event('quality_disposition',d['id'],'requested')
        if requested:add('approval_wait',d,requested,done,d['quantity'],completed=done is not None)
    orders={x['id']:x for x in data['orders']};sale_lines={x['id']:x for x in data['lines']};sale_batches={x['id']:x for x in data['sales_batches']}
    for task in data['tasks']:
        batch=sale_batches.get(task['batch_id'])
        if not batch:continue
        order=orders[task['order_id']];obj=dict(batch,id=task['id'],batch_id=batch['id'],connection_id=order['connection_id'],order_id=order['id'])
        queued=event('sales_order',order['id'],'allocated' if task['kind']=='pick' else 'review_queued',lambda e:e['batch_id']==batch['id'])
        if not queued or event('sales_order',order['id'],'batch_cancelled',lambda e:e['batch_id']==batch['id']):continue
        start=event('sales_order',order['id'],task['kind']+'_start',lambda e:e['task_id']==task['id'])
        done=event('sales_order',order['id'],task['kind']+'_complete',lambda e:e['task_id']==task['id'])
        extra=dict(direct_line_id=batch['line_id'])
        add(task['kind']+'_wait',obj,queued,start,task['quantity'],completed=start is not None,due=order['planned_at'],extra=extra)
        if start:add(task['kind']+'_work',obj,start,done,task['quantity'],completed=done is not None,due=order['planned_at'],extra=extra)
    for order in orders.values():
        confirmed=event('sales_order',order['id'],'confirmed');complete=event('sales_order',order['id'],'channel_completed')
        if not confirmed:continue
        for line in (x for x in sale_lines.values() if x['order_id']==order['id']):
            obj=dict(line,connection_id=order['connection_id'])
            relevant=[e for e in by_object[('sales_order',order['id'])] if visible(e) and e['object_line_id']==line['id']]
            shipped=cancelled=0
            for e in relevant:
                if e['event_type']=='fulfillment.shipped':add('order_ship',obj,confirmed,e,e['quantity'],completed=True,due=order['planned_at']);shipped+=e['quantity']
                elif e['event_type']=='fulfillment.cancelled':cancelled+=e['quantity']
            add('order_ship',obj,confirmed,None,line['quantity']-shipped-cancelled,due=order['planned_at'])
            add('channel_complete',obj,confirmed,complete,line['quantity']-cancelled,completed=complete is not None,due=order['planned_at'])
    for e in data['events']:
        if e['event_type']=='inventory.posted':continue  # Domain event is the same business action, not a second latency sample.
        if e['registered_at']<body['start_at'] or e['registered_at']>body['end_at']:continue
        registered=dict(e,occurred_at=e['registered_at'],time_quality='observed',quality_flags=[])
        obj=dict(id=e['id'],sku_id=e['sku_id'],supplier_id=e['supplier_id'])
        add('registration_delay',obj,e,registered,e['quantity'] or 1,completed=True,extra=dict(original_object_id=e['object_id'],original_object_type=e['object_type']))
    return result
