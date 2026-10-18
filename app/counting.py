"""Range-locked stock counts. Locks and postings share the tenant transaction."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U
from sqlalchemy import select, insert, update, delete
from . import db, inventory as inv
from .domain import require_role, uid, now, FIELDS
from .m3_common import command, fail, scope, version, evidence, read_actor

counts = Table('stock_counts', db.metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('code', S, nullable=False), C('status', S, nullable=False), C('version', I, nullable=False),
    C('creator_id', S, nullable=False), C('submitter_id', S), C('reviewer_id', S),
    C('reason', S, nullable=False), C('created_at', S, nullable=False), C('updated_at', S, nullable=False),
    U('tenant_id', 'code'))
lines = Table('stock_count_lines', db.metadata, C('id', S, primary_key=True), C('count_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('snapshot', JSON),
    C('snapshot_version', I), C('observed', JSON), C('details', JSON), C('document_id', S),
    U('count_id', 'warehouse_id', 'sku_id'))
locks = Table('stock_count_locks', db.metadata, C('id', S, primary_key=True), C('tenant_id', S, nullable=False),
    C('warehouse_id', S, nullable=False), C('sku_id', S, nullable=False), C('count_id', S, nullable=False),
    C('created_at', S, nullable=False), U('tenant_id', 'warehouse_id', 'sku_id'))
observations = Table('stock_count_observations', db.metadata, C('id', S, primary_key=True),
    C('count_id', S, nullable=False), C('line_id', S, nullable=False), C('round', I, nullable=False),
    C('kind', S, nullable=False), C('observed', JSON, nullable=False), C('details', JSON, nullable=False),
    C('actor_id', S, nullable=False), C('created_at', S, nullable=False), U('line_id', 'round'))
history = Table('stock_count_history', db.metadata, C('id', S, primary_key=True),
    C('count_id', S, nullable=False), C('version', I, nullable=False), C('action', S, nullable=False),
    C('actor_id', S, nullable=False), C('payload', JSON, nullable=False), C('created_at', S, nullable=False),
    U('count_id', 'version'))
TABLE_NAMES = tuple(t.name for t in (counts, lines, locks, observations, history))


def assert_unlocked(c, actor, warehouse_id, sku_id):
    lock = c.execute(select(locks.c.count_id).where(locks.c.tenant_id == actor['tenant_id'],
        locks.c.warehouse_id == warehouse_id, locks.c.sku_id == sku_id)).first()
    if lock:
        fail('COUNT_LOCKED', f'此仓库与商品正在盘点（{lock[0]}），库存和承诺暂不可变更')


def get(c, actor, identity):
    row = c.execute(select(counts).where(counts.c.id == identity, counts.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row:
        fail('NOT_FOUND', '盘点单不存在或未授权', 404)
    items = [dict(x) for x in c.execute(select(lines).where(lines.c.count_id == identity).order_by(lines.c.id)).mappings()]
    for item in items:
        scope(c, actor, item['warehouse_id'], item['sku_id'])
    return dict(row), items


def view(c, actor, identity):
    row, items = get(c, actor, identity)
    for item in items:
        item['difference'] = {f: item['observed'][f]-item['snapshot'][f] for f in ('g','q','d')} if item['observed'] else None
    return {**row, 'lines': items,
        'observations': [dict(x) for x in c.execute(select(observations).where(observations.c.count_id == identity).order_by(observations.c.round, observations.c.line_id)).mappings()],
        'history': [dict(x) for x in c.execute(select(history).where(history.c.count_id == identity).order_by(history.c.version)).mappings()]}


def change(c, actor, row, action, body, **updates):
    changes = dict(version=row['version']+1, updated_at=now(), **updates)
    c.execute(update(counts).where(counts.c.id == row['id']).values(**changes))
    row.update(changes)
    c.execute(insert(history).values(id=uid(), count_id=row['id'], version=row['version'], action=action,
        actor_id=actor['id'], payload=body, created_at=now()))
    lifecycle={'created':'registered','confirm':'queued','start':'started','initial':'completed','recount':'completed',
               'submit':'queued','approve':'completed','reject':'paused','coverage_conflict':'paused','cancel':'cancelled'}[action]
    for item in get(c,actor,row['id'])[1]:
        evidence(c,actor,'stock_count',{**row,'line_id':item['id'],'warehouse_id':item['warehouse_id'],
                 'sku_id':item['sku_id'],'details':item['details']},action,lifecycle=lifecycle,
                 payload={'version':row['version'],'status':row['status']})
    return view(c, actor, row['id'])


def create(engine, actor, body, key):
    require_role(actor, 'admin', 'warehouse')
    def authorize(c):
        for item in body['lines']:
            scope(c, actor, item['warehouse_id'], item['sku_id'])
    def apply(c):
        if c.execute(select(counts.c.id).where(counts.c.tenant_id==actor['tenant_id'], counts.c.code==body['code'])).first():
            fail('DUPLICATE_COUNT', '盘点编号已存在，请查看原盘点单')
        row = dict(id=uid(), tenant_id=actor['tenant_id'], code=body['code'], status='draft', version=0,
            creator_id=actor['id'], submitter_id=None, reviewer_id=None, reason=body['reason'], created_at=now(), updated_at=now())
        c.execute(insert(counts).values(**row))
        seen = set()
        for item in body['lines']:
            pair = (item['warehouse_id'], item['sku_id'])
            if pair in seen: fail('DUPLICATE_LINE', '盘点范围不可重复', 422)
            seen.add(pair)
            c.execute(insert(lines).values(id=uid(), count_id=row['id'], **item))
        return change(c, actor, row, 'created', body)
    return command(engine, actor, 'count.create', body, key, apply, authorize)


def act(engine, actor, identity, action, body, key):
    require_role(actor, 'admin' if action in ('approve','reject') else 'warehouse', 'admin')
    def apply(c):
        row, items = get(c, actor, identity)
        version(row, body)
        status = row['status']
        if action == 'confirm':
            if status != 'draft': fail('INVALID_STATE', '仅草稿可以确认范围')
            return change(c, actor, row, action, body, status='confirmed')
        if action == 'start':
            if status != 'confirmed': fail('INVALID_STATE', '请先确认盘点范围')
            for item in items:
                assert_unlocked(c, actor, item['warehouse_id'], item['sku_id'])
                stock = inv.load_balance(c, actor, item['warehouse_id'], item['sku_id'])
                if not stock['version']:
                    from .catalog import assortments
                    known=c.scalar(select(assortments.c.inventory_known).where(assortments.c.tenant_id==actor['tenant_id'],
                        assortments.c.warehouse_id==item['warehouse_id'],assortments.c.sku_id==item['sku_id']))
                    if not known:fail('UNKNOWN_BASELINE', '请先建立该仓库商品的库存基准，再开始盘点')
                c.execute(insert(locks).values(id=uid(), tenant_id=actor['tenant_id'], count_id=identity,
                    warehouse_id=item['warehouse_id'], sku_id=item['sku_id'], created_at=now()))
                c.execute(update(lines).where(lines.c.id==item['id']).values(snapshot={f:stock[f] for f in FIELDS}, snapshot_version=stock['version']))
            return change(c, actor, row, action, body, status='counting')
        if action == 'cancel':
            if status in ('posted','cancelled'): fail('INVALID_STATE', '已结束盘点不可取消')
            c.execute(delete(locks).where(locks.c.count_id==identity, locks.c.tenant_id==actor['tenant_id']))
            return change(c, actor, row, action, body, status='cancelled')
        if action in ('initial','recount'):
            if status not in (('counting',) if action=='initial' else ('initial_counted','recounted','conflict')):
                fail('INVALID_STATE', '当前盘点状态不能录入此轮结果')
            given = {x['line_id']: x for x in body['lines']}
            if len(given)!=len(body['lines']) or set(given)!={x['id'] for x in items}:
                fail('COUNT_SCOPE_MISMATCH', '每轮必须包含全部盘点行且不能重复', 422)
            for item in items:
                obs = given[item['id']]
                if any(type(obs['observed'].get(f)) is not int or not 0<=obs['observed'][f]<=1_000_000 for f in ('g','q','d')):
                    fail('INVALID_QUANTITY','实盘数量须为非负整数',422)
                if obs['observed'] != {f:item['snapshot'][f] for f in ('g','q','d')} and (not obs['details']['opinion'].strip() or not obs['details']['evidence_refs']):
                    fail('EVIDENCE_REQUIRED', '盘点差异须填写处理意见和证据引用', 422)
                c.execute(insert(observations).values(id=uid(), count_id=identity, line_id=item['id'], round=row['version'],
                    kind=action, observed=obs['observed'], details=obs['details'], actor_id=actor['id'], created_at=now()))
                c.execute(update(lines).where(lines.c.id==item['id']).values(observed=obs['observed'], details=obs['details']))
            return change(c, actor, row, action, body, status='initial_counted' if action=='initial' else 'recounted')
        if action == 'submit':
            if status not in ('initial_counted','recounted'): fail('INVALID_STATE','请完成初盘或复盘')
            differences = any(any(x['observed'][f]!=x['snapshot'][f] for f in ('g','q','d')) for x in items)
            if differences and status!='recounted': fail('RECOUNT_REQUIRED', '有差异时必须先复盘')
            return change(c, actor, row, action, body, status='awaiting_review', submitter_id=actor['id'])
        if action in ('approve','reject'):
            if status!='awaiting_review': fail('INVALID_STATE','仅待审批盘点可以审核')
            counters = set(c.execute(select(observations.c.actor_id).where(observations.c.count_id==identity)).scalars())
            if actor['id'] in counters | {row['creator_id'],row['submitter_id']}:
                fail('SELF_APPROVAL','审批人须与申请人、盘点人和提交人不同',403)
            if action=='reject':
                return change(c, actor, row, action, body, status='conflict', reviewer_id=actor['id'])
            stocks=[]; conflicts=[]
            from .operations import protect_pending_quality
            for item in items:
                stock=inv.load_balance(c, actor, item['warehouse_id'],item['sku_id'])
                lock=c.execute(select(locks.c.id).where(locks.c.count_id==identity,locks.c.warehouse_id==item['warehouse_id'],locks.c.sku_id==item['sku_id'])).first()
                if not lock or stock['version']!=item['snapshot_version']:
                    fail('COUNT_SNAPSHOT_CONFLICT','盘点锁或快照版本异常，请取消后重新盘点')
                proposed={**stock, **item['observed']}
                from .domain import BusinessError
                try:
                    inv.validate_balance(proposed); protect_pending_quality(c,actor,proposed)
                except BusinessError as exc:
                    conflicts.append({'line_id':item['id'],'code':exc.code})
                stocks.append(stock)
            if conflicts:
                from . import exception_tasks as tasks
                for conflict in conflicts:
                    item=next(x for x in items if x['id']==conflict['line_id'])
                    tasks.upsert(c,actor,dict(object_type='stock_count',object_id=identity,warehouse_id=item['warehouse_id'],sku_id=item['sku_id'],
                        rule='count_coverage',rule_version='m3-v1',instance=item['id'],type='count_conflict',priority='critical',
                        impact_quantity=sum(abs(item['observed'][f]-item['snapshot'][f]) for f in ('g','q','d')),
                        due_at=None,details=item['details'],requires_review=True),automatic=True)
                return change(c,actor,row,'coverage_conflict',{'conflicts':conflicts},status='conflict',reviewer_id=actor['id'])
            # The lock is released only in the same transaction as ALL postings.
            # Tenant serialization prevents a competing writer from entering here.
            c.execute(delete(locks).where(locks.c.count_id==identity,locks.c.tenant_id==actor['tenant_id']))
            for item, stock in zip(items,stocks):
                delta={f:item['observed'][f]-item['snapshot'][f] for f in ('g','q','d')}
                if any(delta.values()):
                    doc=inv.movement(c,actor,stock,delta,'stock_count',identity,item['details']['opinion'],stock['version'],business_reference=row['code'])
                    c.execute(update(lines).where(lines.c.id==item['id']).values(document_id=doc))
            return change(c,actor,row,action,body,status='posted',reviewer_id=actor['id'])
        fail('INVALID_ACTION','不支持的盘点操作',422)
    return command(engine,actor,'count.'+action+':'+identity,body,key,apply,lambda c:get(c,actor,identity))


def listing(engine, actor, offset=0, limit=50):
    require_role(actor,'admin','warehouse')
    with engine.connect() as c:
        read_actor(c,actor)
        query=select(counts).where(counts.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':
            denied=select(lines.c.count_id).where(~lines.c.warehouse_id.in_(actor['warehouse_ids']))
            query=query.where(~counts.c.id.in_(denied))
        rows=c.execute(query.order_by(counts.c.created_at.desc(),counts.c.id).offset(offset).limit(limit+1)).mappings().all()
        return {'items':[view(c,actor,x['id']) for x in rows[:limit]],'has_more':len(rows)>limit,'offset':offset,'limit':limit}
