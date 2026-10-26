"""Deterministic replenishment with disjoint supply phases and frozen evidence."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, insert, update
from . import db, catalog, inventory as inv, workflows as w, operations as op, supply, m5_schema as s, m5_models as m
from .m5_common import scope, supplier, validate, command, record, READ, WRITE, fail
from .accounts import assert_current, safe_user
from .domain import uid, now, require_role, unallocated, BusinessError, get_warehouse
from .clock import business_now, parse_timestamp

RULE = 'replenishment-v1'

def local_day(c,actor,warehouse,instant):
    row=get_warehouse(c,actor,warehouse,True)
    return parse_timestamp(instant).astimezone(ZoneInfo(row['timezone'])).date()

def get(c,actor,table,identity):
    require_role(actor,*READ)
    row=c.execute(select(table).where(table.c.id==identity,table.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:fail('NOT_FOUND','计划记录不存在或未授权',404)
    scope(c,actor,row['warehouse_id'],row['sku_id'])
    if table is s.plans:
        from .fulfillment import get as get_order
        for order in row['snapshot']['inputs']['orders']['lines']:
            try:get_order(c,actor,order['order_id'])
            except BusinessError:fail('NOT_FOUND','方案含当前未授权订单证据',404)
    return dict(row)

def listing(engine,actor,table,offset=0,limit=20):
    require_role(actor,*READ)
    with engine.connect() as c:
        assert_current(c,actor)
        query=select(table).where(table.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(table.c.warehouse_id.in_(actor['warehouse_ids']))
        rows=[];seen=0
        for raw in c.execute(query.order_by(table.c.created_at.desc(),table.c.id)).mappings():
            try:row=get(c,actor,table,raw['id'])
            except BusinessError as exc:
                if exc.code=='NOT_FOUND':continue
                raise
            if seen>=offset:rows.append(row)
            seen+=1
            if len(rows)>limit:break
        if table is s.plans:
            for row in rows:
                decision=c.execute(select(s.decisions).where(s.decisions.c.plan_id==row['id'])).mappings().first()
                row['decision']=dict(decision) if decision else None
        return dict(items=rows[:limit],offset=offset,limit=limit,has_more=len(rows)>limit)

def demand_day(engine,actor,body,key):
    require_role(actor,*WRITE);body=validate(m.Demand,body)
    def apply(c):
        if body['day']>=local_day(c,actor,body['warehouse_id'],business_now(engine)).isoformat():fail('FUTURE_DEMAND','需求历史必须早于当前业务日',422)
        old=c.execute(select(s.demand).where(s.demand.c.tenant_id==actor['tenant_id'],s.demand.c.warehouse_id==body['warehouse_id'],
            s.demand.c.sku_id==body['sku_id'],s.demand.c.day==body['day'])).mappings().first()
        if old:
            if any(old[k]!=v for k,v in body.items()):fail('DEMAND_CONFLICT','同一需求日已有不同证据，原记录不能覆盖')
            return dict(old)|{'business_replayed':True}
        row=dict(id=uid(),tenant_id=actor['tenant_id'],created_at=now(),**body);c.execute(insert(s.demand).values(**row))
        record(c,actor,'demand_day',row,'registered',body,quantity=body['quantity']);return row
    return command(engine,actor,'demand.day',body,key,apply,lambda c:scope(c,actor,body['warehouse_id'],body['sku_id']))

def policy(engine,actor,body,key):
    require_role(actor,*WRITE);body=validate(m.Policy,body)
    def authorize(c):
        scope(c,actor,body['warehouse_id'],body['sku_id']);supplier(c,actor,body['supplier_id'],body['sku_id'])
    def apply(c):
        old=c.execute(select(s.policies).where(s.policies.c.tenant_id==actor['tenant_id'],s.policies.c.warehouse_id==body['warehouse_id'],s.policies.c.sku_id==body['sku_id'])).mappings().first()
        if body['expected_version']!=(old['version'] if old else 0):fail('VERSION_CONFLICT','补货策略已变化')
        row={k:int(v) if type(v) is bool else v for k,v in body.items() if k!='expected_version'}
        row.update(id=old['id'] if old else uid(),tenant_id=actor['tenant_id'],version=old['version']+1 if old else 1,created_at=old['created_at'] if old else now())
        if old:c.execute(update(s.policies).where(s.policies.c.id==old['id']).values(**row))
        else:c.execute(insert(s.policies).values(**row))
        record(c,actor,'replenishment_policy',row,'updated' if old else 'created',{'before':dict(old) if old else None,'after':row});return row
    return command(engine,actor,'replenishment.policy',body,key,apply,authorize)

def supply_inputs(c,actor,warehouse,sku,instant,horizon):
    rows=[];now_at=parse_timestamp(instant)
    def add(identity,line,phase,quantity,expected,credible,version):
        if not quantity:return
        reason=None
        if not credible:reason='uncommitted'
        elif not expected:reason='unknown_time'
        elif parse_timestamp(expected)<now_at:reason='overdue'
        elif local_day(c,actor,warehouse,expected)>horizon:reason='outside_horizon'
        rows.append(dict(id=identity,supply_line_id=line,phase=phase,quantity=quantity,expected_at=expected,risk=reason,eligible=reason is None,version=version))
    query=select(s.lines).where(s.lines.c.tenant_id==actor['tenant_id'],s.lines.c.warehouse_id==warehouse,s.lines.c.sku_id==sku)
    for line in c.execute(query).mappings():
        table=w.orders if line['kind']=='purchase' else op.transfers
        parent=c.execute(select(table).where(table.c.id==line['order_id'])).mappings().one()
        batches=[b for b in supply.batch_rows(c,line['id']) if b['status']!='cancelled']
        unbatched=line['quantity']-line['closed_quantity']-sum(b['quantity'] for b in batches)
        add('plan:'+line['id'],line['id'],'unbatched',unbatched,line['expected_at'],line['kind']=='purchase',parent['version'])
        for batch in batches:
            phase='in_transit' if batch['sent'] else 'unshipped_batch'
            quantity=batch['sent']-batch['received']-batch['lost'] if batch['sent'] else batch['quantity']
            add('batch:'+batch['id'],line['id'],phase,quantity,batch['expected_at'],line['kind']=='purchase' or bool(batch['reserved'] or batch['sent']),batch['version'])
    # Legacy promises are unknown; report their existence without manufacturing dates.
    managed_orders=select(s.lines.c.order_id)
    legacy=c.execute(select(w.lines,w.orders.c.version).join(w.orders,w.orders.c.id==w.lines.c.order_id).where(w.orders.c.tenant_id==actor['tenant_id'],
        w.orders.c.warehouse_id==warehouse,w.lines.c.sku_id==sku,w.orders.c.status.in_(('open','partial')),w.orders.c.id.not_in(managed_orders))).mappings()
    for line in legacy:add('legacy_purchase:'+line['id'],None,'legacy_unreceived',line['ordered']-line['received'],None,True,line['version'])
    legacy=c.execute(select(op.transfer_lines,op.transfers.c.version).join(op.transfers,op.transfers.c.id==op.transfer_lines.c.transfer_id).where(op.transfers.c.tenant_id==actor['tenant_id'],
        op.transfers.c.destination_id==warehouse,op.transfer_lines.c.sku_id==sku,op.transfers.c.id.not_in(managed_orders))).mappings()
    for line in legacy:add('legacy_transfer:'+line['id'],None,'legacy_in_transit',line['sent']-line['received']-line['lost'],None,True,line['version'])
    return sorted(rows,key=lambda r:r['id'])

def order_inputs(c,actor,warehouse,sku):
    from . import m4_schema as sales, fulfillment
    rows=[];unknown=False;total=reserved=0
    for line in c.execute(select(sales.lines).where(sales.lines.c.tenant_id==actor['tenant_id'],sales.lines.c.sku_id==sku)).mappings():
        order=c.execute(select(sales.orders).where(sales.orders.c.id==line['order_id'])).mappings().one()
        if warehouse not in order['warehouse_ids'] or line['quantity']==line['shipped']+line['cancelled']:continue
        try:fulfillment.get(c,actor,order['id'])
        except BusinessError as exc:
            if exc.code in ('NOT_FOUND','CONNECTION_DISABLED'):unknown=True;continue
            raise
        local_reserved=sum(b['reserved'] for b in c.execute(select(sales.batches).where(sales.batches.c.line_id==line['id'],sales.batches.c.warehouse_id==warehouse)).mappings())
        # Fixed first warehouse owns still-unassigned demand; allocated lots use their real warehouse.
        unassigned=line['quantity']-line['shipped']-line['cancelled']-line['reserved'] if order['warehouse_ids'][0]==warehouse else 0
        total+=local_reserved+unassigned;reserved+=local_reserved
        rows.append(dict(order_id=order['id'],line_id=line['id'],order_version=order['version'],unassigned=unassigned,reserved=local_reserved))
    return dict(total=total,reserved=reserved,unknown=unknown,lines=sorted(rows,key=lambda r:r['line_id']))

def calculate(c,actor,rule,instant):
    warehouse,sku=rule['warehouse_id'],rule['sku_id'];scope(c,actor,warehouse,sku)
    day=local_day(c,actor,warehouse,instant);horizon=day+timedelta(days=rule['lead_days']+rule['coverage_days'])
    wanted=[(day-timedelta(days=i)).isoformat() for i in range(rule['window_days'],0,-1)]
    days=[dict(x) for x in c.execute(select(s.demand).where(s.demand.c.tenant_id==actor['tenant_id'],s.demand.c.warehouse_id==warehouse,
        s.demand.c.sku_id==sku,s.demand.c.day.in_(wanted)).order_by(s.demand.c.day)).mappings()]
    stock=inv.load_balance(c,actor,warehouse,sku)
    assortment=c.execute(select(catalog.assortments).where(catalog.assortments.c.tenant_id==actor['tenant_id'],catalog.assortments.c.warehouse_id==warehouse,catalog.assortments.c.sku_id==sku)).mappings().first()
    sku_config=c.execute(select(catalog.sku_settings).where(catalog.sku_settings.c.sku_id==sku)).mappings().first()
    reasons=[];supplier_record=None
    try:supplier_record=supplier(c,actor,rule['supplier_id'],sku)
    except BusinessError as exc:reasons.append(exc.code)
    if not rule['active']:reasons.append('POLICY_DISABLED')
    if not assortment or not assortment['inventory_known']:reasons.append('INVENTORY_UNKNOWN')
    if not assortment or not assortment['active'] or not assortment['replenishment'] or (sku_config and not sku_config['active']):reasons.append('REPLENISHMENT_DISABLED')
    if [x['day'] for x in days]!=wanted:reasons.append('DEMAND_INCOMPLETE')
    if any(x['quality']!='known' or x['quantity']>rule['max_daily'] for x in days):reasons.append('DEMAND_UNRELIABLE')
    orders=order_inputs(c,actor,warehouse,sku)
    if orders['unknown']:reasons.append('ORDER_SCOPE_INCOMPLETE')
    supplies=supply_inputs(c,actor,warehouse,sku,instant,horizon)
    incoming=sum(x['quantity'] for x in supplies if x['eligible'])
    total=sum(x['quantity'] for x in days)
    forecast=(total*(rule['lead_days']+rule['coverage_days'])+rule['window_days']-1)//rule['window_days']
    residual=max(forecast,orders['total'])-orders['reserved'] if rule['includes_open_orders'] else forecast+orders['total']-orders['reserved']
    available=max(0,unallocated(stock));raw=max(0,residual+rule['safety']-available-incoming)
    proposed=((max(raw,rule['minimum'])+rule['pack']-1)//rule['pack'])*rule['pack'] if raw else 0
    if proposed>1000000:reasons.append('QUANTITY_EXCEEDS_LIMIT')
    inputs=dict(policy=rule,day=day.isoformat(),horizon=horizon.isoformat(),demand_days=days,missing_days=sorted(set(wanted)-{x['day'] for x in days}),
        stock={k:stock[k] for k in ('g','q','d','r','t','h','b','offline','online','pending','withdrawing','version')},
        assortment=dict(assortment) if assortment else None,sku_config=dict(sku_config) if sku_config else None,supplier=supplier_record,
        orders=orders,supplies=supplies,rule_version=RULE)
    return dict(status='unknown' if reasons else 'ready',suggested=None if reasons else proposed,reasons=reasons,inputs=inputs,
        calculation=dict(historical_total=total,window_days=rule['window_days'],forecast=None if any(x.startswith('DEMAND_') for x in reasons) else forecast,
            order_unfulfilled=orders['total'],order_reserved_already_deducted=orders['reserved'],demand_after_reservations=None if reasons else residual,
            unallocated_stock=available,eligible_supply=incoming,risky_supply=sum(x['quantity'] for x in supplies if not x['eligible']),
            target_safety=rule['safety'],net_before_purchase_constraints=None if reasons else raw),
        expected_arrival=(parse_timestamp(instant)+timedelta(days=rule['lead_days'])).isoformat(),rule_version=RULE)

def plan_in_transaction(c,actor,identity):
    require_role(actor,*WRITE)
    rule=get(c,actor,s.policies,identity);instant=business_now(c.engine);result=calculate(c,actor,rule,instant)
    current=dict(c.execute(select(db.users).where(db.users.c.id==actor['id'])).mappings().one())
    row=dict(id=uid(),tenant_id=actor['tenant_id'],policy_id=identity,warehouse_id=rule['warehouse_id'],sku_id=rule['sku_id'],
        creator_id=actor['id'],auth_version=safe_user(c,current)['version'],as_of=instant,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
        status=result['status'],suggested=result['suggested'],snapshot=result,fingerprint=inv.fingerprint(result['inputs']),created_at=now())
    c.execute(insert(s.plans).values(**row));record(c,actor,'replenishment_plan',row,'calculated',{'status':result['status'],'suggested':result['suggested'],'rule_version':RULE,'snapshot_id':row['id']});return row

def plan(engine,actor,identity,key):
    require_role(actor,*WRITE)
    return command(engine,actor,'replenishment.plan:'+identity,{},key,lambda c:plan_in_transaction(c,actor,identity),lambda c:get(c,actor,s.policies,identity))

def confirm(engine,actor,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Confirm,body)
    def authorize(c):
        row=get(c,actor,s.plans,identity)
        if row['creator_id']!=actor['id']:fail('PLAN_CREATOR_ONLY','只能由方案创建者确认',403)
        return row
    def apply(c):
        row=authorize(c)
        old=c.execute(select(s.decisions).where(s.decisions.c.plan_id==identity)).mappings().first()
        if old:
            if old['quantity']!=body['quantity'] or old['reason']!=body['reason']:fail('PLAN_ALREADY_CONFIRMED','方案已经按其他数量或意见确认')
            return dict(old)|{'business_replayed':True}
        if parse_timestamp(row['expires_at'])<=datetime.now(timezone.utc):fail('PLAN_EXPIRED','方案已过期，请重新计算')
        current=dict(c.execute(select(db.users).where(db.users.c.id==actor['id'])).mappings().one())
        if safe_user(c,current)['version']!=row['auth_version']:fail('PERMISSION_CHANGED','授权版本变化，请重新计算')
        rule=get(c,actor,s.policies,row['policy_id']);result=calculate(c,actor,rule,business_now(engine))
        if row['status']!='ready' or result['status']!='ready':fail('PLAN_UNKNOWN','需求或供给依据不足，不能确认采购')
        if inv.fingerprint(result['inputs'])!=row['fingerprint']:fail('PLAN_STALE','库存、需求、供给或策略已变化，请重新计算')
        quantity=body['quantity'];order=None
        if quantity:
            if quantity<rule['minimum'] or quantity%rule['pack']:fail('PURCHASE_CONSTRAINT','确认量须满足最小采购量与包装倍数',422)
            payload=validate(m.Purchase,dict(warehouse_id=row['warehouse_id'],code='PLAN-'+identity,supplier_id=rule['supplier_id'],
                note=body['reason'],ordered_at=business_now(engine),promised_at=result['expected_arrival'],lines=[dict(sku_id=row['sku_id'],quantity=quantity)]))
            order=supply.create_in_transaction(c,actor,'purchase',payload)
        decision=dict(id=uid(),tenant_id=actor['tenant_id'],plan_id=identity,quantity=quantity,reason=body['reason'],actor_id=actor['id'],order_id=order['id'] if order else None,created_at=now())
        c.execute(insert(s.decisions).values(**decision));record(c,actor,'replenishment_plan',row,'confirmed' if quantity else 'declined',decision|{'suggested':row['suggested']},quantity=quantity,lifecycle='completed')
        return decision
    return command(engine,actor,'replenishment.confirm:'+identity,body,key,apply,authorize)
