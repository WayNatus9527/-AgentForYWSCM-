"""Idempotent anomaly instances, auditable handling and human review."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U
from sqlalchemy import select, insert, update
from . import db
from .domain import require_role, uid, now
from .clock import business_now, parse_timestamp
from .m3_common import command, fail, scope, version, evidence, read_actor

tasks = Table('exception_tasks', db.metadata, C('id',S,primary_key=True), C('tenant_id',S,nullable=False),
    C('warehouse_id',S,nullable=False), C('sku_id',S,nullable=False), C('object_type',S,nullable=False), C('object_id',S,nullable=False),
    C('rule',S,nullable=False), C('rule_version',S,nullable=False), C('instance',S,nullable=False), C('type',S,nullable=False),
    C('priority',S,nullable=False), C('impact_quantity',I,nullable=False), C('due_at',S), C('details',JSON,nullable=False),
    C('requires_review',I,nullable=False), C('status',S,nullable=False), C('previous_status',S), C('version',I,nullable=False),
    C('owner_id',S), C('creator_id',S,nullable=False), C('resolver_id',S), C('reviewer_id',S),
    C('result',S), C('responsibility_confirmed',I,nullable=False), C('created_at',S,nullable=False), C('updated_at',S,nullable=False),
    U('tenant_id','object_type','object_id','rule','instance'))
history = Table('exception_task_history',db.metadata,C('id',S,primary_key=True),C('task_id',S,nullable=False),
    C('version',I,nullable=False),C('action',S,nullable=False),C('actor_id',S,nullable=False),
    C('payload',JSON,nullable=False),C('created_at',S,nullable=False),U('task_id','version'))
reminders = Table('exception_reminders',db.metadata,C('id',S,primary_key=True),C('task_id',S,nullable=False),
    C('due_at',S,nullable=False),C('created_at',S,nullable=False),U('task_id','due_at'))
TABLE_NAMES=tuple(x.name for x in (tasks,history,reminders))


def linked(c,actor,body):
    scope(c,actor,body['warehouse_id'],body['sku_id'])
    from . import counting, operations, quality_control
    tables={'stock_count':counting.counts,'return':operations.returns,'inspection':quality_control.sessions,'stock_document':db.documents}
    table=tables.get(body['object_type'])
    if table is None: fail('INVALID_LINK','不支持此业务对象类型',422)
    row=c.execute(select(table).where(table.c.id==body['object_id'],table.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND','关联业务对象不存在或未授权',404)
    if body['object_type']=='stock_count':
        _,items=counting.get(c,actor,row['id'])
        if not any(x['warehouse_id']==body['warehouse_id'] and x['sku_id']==body['sku_id'] for x in items):
            fail('INVALID_LINK','任务范围不属于该盘点单',422)
    elif row['warehouse_id']!=body['warehouse_id'] or row['sku_id']!=body['sku_id']:
        fail('INVALID_LINK','任务范围与原业务对象不一致',422)


def get(c,actor,identity):
    row=c.execute(select(tasks).where(tasks.c.id==identity,tasks.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row: fail('NOT_FOUND','异常任务不存在或未授权',404)
    scope(c,actor,row['warehouse_id'],row['sku_id'])
    return dict(row)


def view(c,actor,identity):
    row=get(c,actor,identity)
    return {**row,'overdue':bool(row['due_at'] and row['status']!='closed' and parse_timestamp(row['due_at'])<parse_timestamp(business_now(c.engine))),
        'history':[dict(x) for x in c.execute(select(history).where(history.c.task_id==identity).order_by(history.c.version)).mappings()],
        'reminders':[dict(x) for x in c.execute(select(reminders).where(reminders.c.task_id==identity)).mappings()]}


def change(c,actor,row,action,payload,**changes):
    changes.update(version=row['version']+1,updated_at=now())
    c.execute(update(tasks).where(tasks.c.id==row['id']).values(**changes));row.update(changes)
    c.execute(insert(history).values(id=uid(),task_id=row['id'],version=row['version'],action=action,
        actor_id=actor['id'],payload=payload,created_at=now()))
    lifecycle={'unassigned':'queued','in_progress':'started','paused':'paused','awaiting_review':'queued','closed':'completed'}[row['status']]
    if action in ('resume','reopen'):lifecycle='resumed'
    evidence(c,actor,'exception_task',row,action,lifecycle=lifecycle,quantity=row['impact_quantity'],payload={'version':row['version'],'status':row['status']})
    return view(c,actor,row['id'])


def upsert(c,actor,body,automatic=False):
    linked(c,actor,body)
    if not automatic and body['rule'].startswith('count_'):
        fail('RESERVED_RULE','此规则由盘点系统维护',422)
    old=c.execute(select(tasks).where(tasks.c.tenant_id==actor['tenant_id'],tasks.c.object_type==body['object_type'],
        tasks.c.object_id==body['object_id'],tasks.c.rule==body['rule'],tasks.c.instance==body['instance'])).mappings().first()
    required=int(body['type']=='count_conflict' or body.get('requires_review',False) or body['priority'] in ('high','critical') or body['details']['responsibility_type']=='person')
    if old:
        if old['warehouse_id']!=body['warehouse_id'] or old['sku_id']!=body['sku_id'] or old['type']!=body['type']:
            fail('TASK_IDENTITY_CONFLICT','异常实例已关联其他范围或类型')
        last=c.execute(select(history.c.payload).where(history.c.task_id==old['id'],history.c.action.in_(('created','metrics_updated'))).order_by(history.c.version.desc()).limit(1)).scalar_one()
        fields=('rule_version','impact_quantity','due_at','details','priority','requires_review')
        if all(last.get(k)==body.get(k) for k in fields): return view(c,actor,old['id'])
        changes={k:body[k] for k in ('rule_version','impact_quantity','due_at') if old[k]!=body[k]}
        rank={'low':0,'normal':1,'high':2,'critical':3}
        if rank[body['priority']]>rank[old['priority']]:changes['priority']=body['priority']
        changes['requires_review']=max(required,old['requires_review'])
        if old['status'] not in ('closed','awaiting_review'):
            changes.update(details=body['details'],responsibility_confirmed=0)
        # A later metric observation cannot rewrite already-reviewed evidence.
        # It is retained in history; reopening remains an explicit human action.
        return change(c,actor,dict(old),'metrics_updated',body,**changes)
    row={k:body[k] for k in ('warehouse_id','sku_id','object_type','object_id','rule','rule_version','instance','type','priority','impact_quantity','due_at','details')}
    row.update(id=uid(),tenant_id=actor['tenant_id'],status='unassigned',version=0,requires_review=required,
        creator_id=actor['id'],responsibility_confirmed=0,created_at=now(),updated_at=now())
    c.execute(insert(tasks).values(**row))
    return change(c,actor,row,'created',body)


def create(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    return command(engine,actor,'exception.upsert',body,key,lambda c:upsert(c,actor,body),lambda c:linked(c,actor,body))


def validate_owner(c,actor,row,identity):
    from .accounts import active
    user=c.execute(select(db.users).where(db.users.c.id==identity,db.users.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not user or not active(c,identity) or user['role'] not in ('admin','warehouse') or (user['role']!='admin' and row['warehouse_id'] not in user['warehouse_ids']):
        fail('INVALID_OWNER','负责人须是当前商户中有该仓库权限的有效管理员或仓管',422)


def act(engine,actor,identity,action,body,key):
    require_role(actor,'admin' if action in ('approve','reject','assign') else 'warehouse','admin')
    def apply(c):
        row=get(c,actor,identity);version(row,body);status=row['status']
        if action=='assign':
            if status in ('closed','awaiting_review'): fail('INVALID_STATE','请先重开或驳回任务')
            validate_owner(c,actor,row,body['owner_id'])
            return change(c,actor,row,action,body,owner_id=body['owner_id'],status='in_progress',previous_status=None)
        if action in ('pause','resume','resolve') and actor['id']!=row['owner_id']:
            fail('NOT_OWNER','仅当前负责人可以处理任务',403)
        if action=='pause':
            if status!='in_progress': fail('INVALID_STATE','仅处理中任务可暂停')
            return change(c,actor,row,action,body,status='paused',previous_status=status)
        if action=='resume':
            if status!='paused': fail('INVALID_STATE','仅暂停任务可恢复')
            return change(c,actor,row,action,body,status='in_progress',previous_status=None)
        if action=='resolve':
            if status!='in_progress': fail('INVALID_STATE','请先分派或恢复任务')
            # All closes require a concrete treatment result. Person attribution
            # is a proposal until a different human reviewer explicitly confirms it.
            details=body['details']; required=bool(row['requires_review'] or details['responsibility_type']=='person')
            return change(c,actor,row,action,body,status='awaiting_review' if required else 'closed',
                result=body['result'],resolver_id=actor['id'],details=details,requires_review=int(required),responsibility_confirmed=0)
        if action in ('approve','reject'):
            if status!='awaiting_review': fail('INVALID_STATE','仅待复核任务可以审核')
            if actor['id'] in (row['owner_id'],row['resolver_id']): fail('SELF_APPROVAL','任务复核人须与处理人不同',403)
            if action=='approve':
                if row['details']['responsibility_type']=='person' and not body.get('confirm_responsibility'):
                    fail('RESPONSIBILITY_REVIEW_REQUIRED','人员归责需人工明确复核确认',422)
                return change(c,actor,row,action,body,status='closed',reviewer_id=actor['id'],
                    responsibility_confirmed=int(row['details']['responsibility_type']=='person'))
            return change(c,actor,row,action,body,status='in_progress',reviewer_id=actor['id'],responsibility_confirmed=0)
        if action=='reopen':
            if status!='closed': fail('INVALID_STATE','仅已关闭任务可重开')
            return change(c,actor,row,action,body,status='in_progress' if row['owner_id'] else 'unassigned',responsibility_confirmed=0)
        fail('INVALID_ACTION','不支持的任务操作',422)
    return command(engine,actor,'exception.'+action+':'+identity,body,key,apply,lambda c:get(c,actor,identity))


def listing(engine,actor,offset=0,limit=50):
    require_role(actor,'admin','warehouse')
    with engine.connect() as c:
        read_actor(c,actor)
        query=select(tasks.c.id).where(tasks.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(tasks.c.warehouse_id.in_(actor['warehouse_ids']))
        rows=c.execute(query.order_by(tasks.c.updated_at.desc(),tasks.c.id).offset(offset).limit(limit+1)).scalars().all()
        return {'items':[view(c,actor,x) for x in rows[:limit]],'has_more':len(rows)>limit,'offset':offset,'limit':limit}


def remind(engine,actor,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        query=select(tasks).where(tasks.c.tenant_id==actor['tenant_id'],tasks.c.status!='closed',tasks.c.due_at.is_not(None))
        if actor['role']!='admin':query=query.where(tasks.c.warehouse_id.in_(actor['warehouse_ids']))
        created=0
        for row in c.execute(query).mappings().all():
            if parse_timestamp(row['due_at'])>=parse_timestamp(business_now(engine)):continue
            if c.execute(select(reminders.c.id).where(reminders.c.task_id==row['id'],reminders.c.due_at==row['due_at'])).first():continue
            c.execute(insert(reminders).values(id=uid(),task_id=row['id'],due_at=row['due_at'],created_at=business_now(engine)))
            evidence(c,actor,'exception_task',dict(row),'overdue_reminder',payload={'due_at':row['due_at']})
            created+=1
        return {'reminders_created':created,'business_time':business_now(engine)}
    return command(engine,actor,'exception.remind',{},key,apply,lambda c:None)
