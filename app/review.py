"""Durable, scoped process reviews. Reports and their evidence never change."""
import logging,time
from sqlalchemy import select,insert,update,func,or_,and_
from . import db,events,inventory,workflows,m6_schema as s,m6_models as m,review_sources,review_metrics
from .review_queue_schema import history as job_history
from .accounts import assert_current
from .domain import uid,now,require_role,BusinessError,get_warehouse,get_sku
from .clock import business_now,parse_timestamp
from .m5_common import scope,supplier,READ,WRITE,fail,validate
from .m4_common import connection

LEASE_SECONDS=300
RETRY_DELAYS=(5,30,120)
logger=logging.getLogger(__name__)

def job_event(c,job,event,*,actor_id=None,error=None,reason=None):
    c.execute(insert(job_history).values(id=uid(),tenant_id=job['tenant_id'],job_id=job['id'],actor_id=actor_id,
        event=event,attempt=job['attempts'],error=error,reason=reason,recorded_at=time.time()))

def creator(c,job):
    row=c.execute(select(db.users).where(db.users.c.id==job['creator_id'],db.users.c.tenant_id==job['tenant_id'])).mappings().first()
    if not row:fail('UNAUTHENTICATED','创建人已失效',401)
    actor=dict(row);authorize_request(c,actor,job['request']);return actor

def read_scope(c,actor,warehouse,sku=None):
    assert_current(c,actor);get_warehouse(c,actor,warehouse)
    if sku:get_sku(c,actor,sku)


def command(engine,actor,operation,body,key,apply,authorize):
    if not isinstance(key,str) or not 8<=len(key)<=160:fail('INVALID_KEY','请求编号须为8至160个字符',422)
    return workflows.command(engine,actor,'m6.'+operation,body,'m6:'+inventory.fingerprint([operation,key]),apply,authorize=authorize)


def authorize_request(c,actor,body):
    require_role(actor,*READ);read_scope(c,actor,body['warehouse_id'],body.get('sku_id'))
    if body.get('supplier_id'):supplier(c,actor,body['supplier_id'],body.get('sku_id'))
    if body.get('connection_id'):
        account=connection(c,actor,body['connection_id'])
        if body['warehouse_id'] not in account['warehouse_ids']:fail('NOT_FOUND','连接未授权此仓库',404)


def get(c,actor,identity):
    require_role(actor,*READ);assert_current(c,actor)
    row=c.execute(select(s.reports).where(s.reports.c.id==identity,s.reports.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:fail('NOT_FOUND','报告不存在或未授权',404)
    read_scope(c,actor,row['warehouse_id'],row['sku_id'])
    for account in row['scope']['connections']:
        current=connection(c,actor,account['id'])
        if not set(account['warehouses'])<=set(current['warehouse_ids']):fail('NOT_FOUND','报告证据授权已变化',404)
        for warehouse in account['warehouses']:get_warehouse(c,actor,warehouse)
    return dict(row)


def get_job(c,actor,identity):
    require_role(actor,*READ);assert_current(c,actor)
    row=c.execute(select(s.jobs).where(s.jobs.c.id==identity,s.jobs.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row or actor['role']!='admin' and row['creator_id']!=actor['id']:fail('NOT_FOUND','分析任务不存在或未授权',404)
    authorize_request(c,actor,row['request'])
    if row['report_id']:get(c,actor,row['report_id'])
    result={k:v for k,v in dict(row).items() if k not in ('lease_token','lease_until')}
    result['awaiting_recovery']=row['status']=='running' and (row['lease_until'] or 0)<=time.time()
    return result


def create(engine,actor,body,key):
    body=validate(m.Analyze,body)
    if parse_timestamp(body['end_at'])>parse_timestamp(business_now(engine)):fail('FUTURE_ANALYSIS','分析截止时间不能晚于当前业务时间',422)
    def apply(c):
        running=c.execute(select(s.jobs.c.id).where(s.jobs.c.tenant_id==actor['tenant_id'],s.jobs.c.creator_id==actor['id'],s.jobs.c.status.in_(('queued','running')))).all()
        if len(running)>=3:fail('ANALYSIS_BUSY','已有3个待处理分析，请等待完成',429)
        row=dict(id=uid(),tenant_id=actor['tenant_id'],creator_id=actor['id'],warehouse_id=body['warehouse_id'],sku_id=body['sku_id'],
            request=body,status='queued',attempts=0,attempt_limit=3,created_at=now())
        c.execute(insert(s.jobs).values(**row));job_event(c,row,'queued',actor_id=actor['id']);return get_job(c,actor,row['id'])
    return command(engine,actor,'analysis.create',body,key,apply,lambda c:authorize_request(c,actor,body))


def process(engine,identity):
    """Claim with a real-time lease; publish all evidence and completion atomically.

    A crash before commit leaves a reclaimable lease. A crash after commit has
    exactly one report. No model or external calls take place in this transaction.
    """
    with engine.connect() as c:tenant=c.scalar(select(s.jobs.c.tenant_id).where(s.jobs.c.id==identity))
    if not tenant:return None
    token=uid()
    with db.write(engine,tenant) as c:
        job=dict(c.execute(select(s.jobs).where(s.jobs.c.id==identity)).mappings().one())
        if job['status']=='completed':return job['report_id']
        if job['status']=='failed' or job['status']=='running' and (job['lease_until'] or 0)>time.time():return None
        if job['status']=='queued' and (job['next_attempt_at'] or 0)>time.time():return None
        if job['attempts']>=job['attempt_limit']:
            c.execute(update(s.jobs).where(s.jobs.c.id==identity).values(status='failed',error='RETRY_EXHAUSTED',finished_at=time.time(),lease_token=None,lease_until=None,next_attempt_at=None))
            job_event(c,job,'failed',error='RETRY_EXHAUSTED');return None
        if job['status']=='running':job_event(c,job,'lease_expired',error='WORKER_INTERRUPTED')
        job['attempts']+=1
        c.execute(update(s.jobs).where(s.jobs.c.id==identity).values(status='running',lease_token=token,lease_until=time.time()+LEASE_SECONDS,
            attempts=job['attempts'],error=None,next_attempt_at=None,started_at=time.time(),finished_at=None))
        job_event(c,job,'started')
    try:
        with db.write(engine,tenant) as c:
            job=dict(c.execute(select(s.jobs).where(s.jobs.c.id==identity)).mappings().one())
            if job['lease_token']!=token:return None
            actor=creator(c,job);body=job['request']
            data=review_sources.capture(c,actor,body);raw=review_sources.spans(data,body)
            if body.get('connection_id'):
                refs={(x['source_type'],x['source_id']) for x in data['links']}
                raw=[r for r in raw if r['connection_id']==body['connection_id'] or any((x['type'],x['id']) in refs for x in r['sources'])]
            result=review_metrics.analyze(raw,data,body);report_id=uid()
            result['summary']['data_quality_tasks']=clock_tasks(c,actor,data,result['samples'])
            report=dict(id=report_id,tenant_id=tenant,job_id=identity,creator_id=actor['id'],warehouse_id=body['warehouse_id'],sku_id=body['sku_id'],
                as_of=body['end_at'],recorded_cutoff=business_now(engine),rule_version=review_metrics.RULE,request=body,scope=data['scope'],summary=result['summary'],
                source_hash=inventory.fingerprint(dict(inputs=data,request=body,rule=review_metrics.RULE)),created_at=now())
            c.execute(insert(s.reports).values(**report))
            inputs=[dict(object_id=x['id'],table=table,record=x,sku_id=x.get('sku_id')) for table,values in data.items()
                if table not in ('events','event_hashes') and isinstance(values,list) for x in values]
            for category,items in [('sample',result['samples']),('candidate',result['candidates']),('impact',result['impacts']),('event',data['events']),('input',inputs)]:
                packed=[]
                for index,item in enumerate(items):
                    row_id=uid();payload=dict(item)
                    if category=='candidate':payload['candidate_key']=item['object_id']
                    packed.append(dict(id=row_id,tenant_id=tenant,report_id=report_id,warehouse_id=body['warehouse_id'],category=category,
                        object_id=item['object_id'],sku_id=item.get('sku_id'),supplier_id=item.get('supplier_id'),connection_id=item.get('connection_id'),
                        metric=item.get('metric'),ordinal=index,payload=payload))
                if packed:c.execute(insert(s.rows),packed)
            c.execute(update(s.jobs).where(s.jobs.c.id==identity,s.jobs.c.lease_token==token).values(status='completed',report_id=report_id,
                lease_until=None,lease_token=None,next_attempt_at=None,error=None,finished_at=time.time()))
            job_event(c,job,'completed')
            return report_id
    except BusinessError as exc:
        finish_failure(engine,tenant,identity,token,exc.code,retryable=False)
        return None
    except Exception:
        finish_failure(engine,tenant,identity,token,'ANALYSIS_TEMPORARY_ERROR',retryable=True)
        raise


def finish_failure(engine,tenant,identity,token,error,*,retryable):
    with db.write(engine,tenant) as c:
        job=dict(c.execute(select(s.jobs).where(s.jobs.c.id==identity)).mappings().one())
        if job['lease_token']!=token:return
        retry=retryable and job['attempts']<job['attempt_limit']
        delay=RETRY_DELAYS[min(job['attempts']-1,len(RETRY_DELAYS)-1)]
        c.execute(update(s.jobs).where(s.jobs.c.id==identity).values(status='queued' if retry else 'failed',error=error,
            lease_token=None,lease_until=None,next_attempt_at=time.time()+delay if retry else None,finished_at=None if retry else time.time()))
        job_event(c,job,'retry_scheduled' if retry else 'failed',error=error)


def dispatch(engine,identity):
    try:return process(engine,identity)
    except Exception as exc:
        # SQL errors can embed connection details or user inputs. Log the class
        # and job reference, never raw exception text/tracebacks or parameters.
        logger.warning('analysis_job_error job_id=%s type=%s',identity,type(exc).__name__)
        return None


def clock_tasks(c,actor,data,samples):
    from .exception_tasks import upsert
    from .m3_models import Details
    ids=[];negative={identity for sample in samples if set(sample['flags'])=={'negative_duration'} for identity in sample['event_ids']}
    for e in data['events']:
        if not (e['quality_flags'] or negative.intersection(e['evidence_ids'])) or not e['warehouse_id'] or not e['sku_id'] or e['event_type']=='inventory.posted':continue
        if get_warehouse(c,actor,e['warehouse_id'])['authority']!='local':continue
        task=upsert(c,actor,dict(object_type='business_event',object_id=e['id'],warehouse_id=e['warehouse_id'],sku_id=e['sku_id'],
            rule='process_clock',rule_version=review_metrics.RULE,instance=e['effective_id'],type='data_quality',priority='normal',
            impact_quantity=e['quantity'] or 0,due_at=None,requires_review=False,
            details=Details(reason_code='data_delay',opinion='来源时间顺序异常，请核实凭证并追加订正；不能把登记时间当作实际发生时间',
                evidence_refs=e['evidence_ids']).model_dump()),automatic=True)
        ids.append(task['id'])
    return ids


def pending(engine,limit=20):
    instant=time.time()
    eligible=or_(and_(s.jobs.c.status=='queued',or_(s.jobs.c.next_attempt_at.is_(None),s.jobs.c.next_attempt_at<=instant)),
        and_(s.jobs.c.status=='running',or_(s.jobs.c.lease_until.is_(None),s.jobs.c.lease_until<=instant)))
    with engine.connect() as c:
        ranked=select(s.jobs.c.id,s.jobs.c.created_at,func.row_number().over(partition_by=s.jobs.c.tenant_id,order_by=(s.jobs.c.created_at,s.jobs.c.id)).label('position')).where(eligible).subquery()
        return list(c.scalars(select(ranked.c.id).order_by(ranked.c.position,ranked.c.created_at,ranked.c.id).limit(limit)))


def recover(engine,limit=20,stop_event=None):
    result=[]
    for identity in pending(engine,limit):
        if stop_event is not None and stop_event.is_set():break
        result.append(dispatch(engine,identity))
    return result


def retry(engine,actor,identity,body,key):
    body=validate(m.Retry,body)
    def apply(c):
        job=get_job(c,actor,identity)
        if job['status']!='failed' or job['attempts']!=body['expected_attempts']:fail('JOB_VERSION_CONFLICT','任务状态已变化，请刷新后重试')
        creator(c,job)
        count=c.scalar(select(func.count()).select_from(s.jobs).where(s.jobs.c.tenant_id==actor['tenant_id'],s.jobs.c.creator_id==job['creator_id'],s.jobs.c.status.in_(('queued','running'))))
        if count>=3:fail('ANALYSIS_BUSY','创建人已有3个待处理分析，请等待完成',429)
        c.execute(update(s.jobs).where(s.jobs.c.id==identity).values(status='queued',attempt_limit=job['attempts']+3,next_attempt_at=None,
            lease_token=None,lease_until=None,started_at=None,finished_at=None,error=None))
        job_event(c,job,'retry_requested',actor_id=actor['id'],reason=body['reason'])
        return get_job(c,actor,identity)
    return command(engine,actor,'analysis.retry:'+identity,body,key,apply,lambda c:get_job(c,actor,identity))


def jobs(engine,actor,offset=0,limit=20):
    require_role(actor,*READ)
    with engine.connect() as c:
        assert_current(c,actor);items=[];seen=0
        query=select(s.jobs.c.id).where(s.jobs.c.tenant_id==actor['tenant_id']).order_by(s.jobs.c.created_at.desc(),s.jobs.c.id)
        if actor['role']!='admin':query=query.where(s.jobs.c.creator_id==actor['id'],s.jobs.c.warehouse_id.in_(actor['warehouse_ids']))
        for identity in c.scalars(query):
            try:value=get_job(c,actor,identity)
            except BusinessError as exc:
                if exc.code in ('NOT_FOUND','CONNECTION_DISABLED','WAREHOUSE_FORBIDDEN','SUPPLIER_UNAVAILABLE','SUPPLIER_SKU_UNAVAILABLE'):continue
                raise
            if seen>=offset:items.append(value)
            seen+=1
            if len(items)>limit:break
    return dict(items=items[:limit],offset=offset,limit=limit,has_more=len(items)>limit)


def job_timeline(c,actor,identity,offset=0,limit=20):
    get_job(c,actor,identity)
    values=[dict(x) for x in c.execute(select(job_history).where(job_history.c.job_id==identity,job_history.c.tenant_id==actor['tenant_id'])
        .order_by(job_history.c.recorded_at,job_history.c.id).offset(offset).limit(limit+1)).mappings()]
    return dict(items=values[:limit],offset=offset,limit=limit,has_more=len(values)>limit)


def listing(engine,actor,offset=0,limit=20):
    require_role(actor,*READ)
    with engine.connect() as c:
        assert_current(c,actor);out=[];seen=0
        q=select(s.reports.c.id).where(s.reports.c.tenant_id==actor['tenant_id']).order_by(s.reports.c.created_at.desc(),s.reports.c.id)
        if actor['role']!='admin':q=q.where(s.reports.c.warehouse_id.in_(actor['warehouse_ids']))
        for identity in c.scalars(q):
            try:row=get(c,actor,identity)
            except BusinessError as exc:
                if exc.code in ('NOT_FOUND','CONNECTION_DISABLED','WAREHOUSE_FORBIDDEN'):continue
                raise
            if seen>=offset:out.append(row)
            seen+=1
            if len(out)>limit:break
    return dict(items=out[:limit],offset=offset,limit=limit,has_more=len(out)>limit)


def page(c,actor,identity,category,offset=0,limit=20,object_id=None,metric=None):
    get(c,actor,identity)
    if category not in ('sample','candidate','impact','event','input'):fail('INVALID_INPUT','不支持的明细类型',422)
    query=select(s.rows).where(s.rows.c.report_id==identity,s.rows.c.tenant_id==actor['tenant_id'],s.rows.c.category==category)
    if object_id:query=query.where(s.rows.c.object_id==object_id)
    if metric:query=query.where(s.rows.c.metric==metric)
    rows=[dict(x) for x in c.execute(query.order_by(s.rows.c.ordinal).offset(offset).limit(limit+1)).mappings()]
    return dict(items=rows[:limit],offset=offset,limit=limit,has_more=len(rows)>limit)


def row(c,actor,identity):
    value=c.execute(select(s.rows).where(s.rows.c.id==identity,s.rows.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not value:fail('NOT_FOUND','报告证据不存在或未授权',404)
    get(c,actor,value['report_id']);return dict(value)


def improve(engine,actor,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Improvement,body)
    def apply(c):
        value=row(c,actor,identity);p=value['payload']
        if value['category'] not in ('candidate','sample') or not value['sku_id']:fail('INVALID_INPUT','请选择带商品范围的候选或异常样本',422)
        old=c.execute(select(s.improvements).where(s.improvements.c.report_id==value['report_id'],s.improvements.c.row_id==identity)).mappings().first()
        if old:return dict(old)|{'business_replayed':True}
        from .exception_tasks import upsert
        from .m3_models import Details
        task=upsert(c,actor,dict(object_type='analysis_row',object_id=identity,warehouse_id=value['warehouse_id'],sku_id=value['sku_id'],
            rule='process_improvement',rule_version=review_metrics.RULE,instance=identity,type='process_review',priority='normal',
            impact_quantity=p.get('impact_quantity',p.get('quantity',0)),due_at=None,requires_review=False,
            details=Details(reason_code='system',opinion=body['reason'],evidence_refs=[value['report_id'],identity]).model_dump()),automatic=True)
        result=dict(id=uid(),tenant_id=actor['tenant_id'],report_id=value['report_id'],row_id=identity,task_id=task['id'],actor_id=actor['id'],created_at=now())
        c.execute(insert(s.improvements).values(**result));return result
    return command(engine,actor,'improve:'+identity,body,key,apply,lambda c:row(c,actor,identity))


def event_scope(c,actor,identity):
    assert_current(c,actor)
    value=c.execute(select(events.business_events).where(events.business_events.c.id==identity,events.business_events.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not value:fail('NOT_FOUND','事件不存在或未授权',404)
    if value['object_type']=='sales_order':
        from .fulfillment import get as order
        order(c,actor,value['object_id'])
    elif value['warehouse_id']:scope(c,actor,value['warehouse_id'],value['sku_id'])
    else:fail('NOT_FOUND','事件缺少可授权业务范围',404)
    return dict(value)


def correct(engine,actor,identity,body,key):
    require_role(actor,*WRITE);body=validate(m.Correct,body)
    def apply(c):
        value=event_scope(c,actor,identity)
        if value['event_type']=='inventory.posted':fail('INVALID_INPUT','记账登记事件不能修改；请订正对应业务事件',422)
        if identity!=body['expected_event_id'] or c.scalar(select(events.business_events.c.id).where(events.business_events.c.correction_of==identity)):
            fail('EVENT_VERSION_CONFLICT','事件已有订正，请重新读取最新证据')
        copied={k:value[k] for k in ('event_type','object_type','object_id','object_line_id','process_id','batch_id','task_id','warehouse_id','sku_id','supplier_id','quantity','unit','resource_group','dependencies','evidence_refs')}
        new_id=events.record_event(c,actor,**copied,lifecycle='corrected',correction_of=identity,occurred_at=body['occurred_at'],planned_at=body['planned_at'],
            time_quality=body['time_quality'],source_system='review_correction',reason_code='time_correction',rule_version=review_metrics.RULE,payload=dict(reason=body['reason']))
        return dict(event_id=new_id,correction_of=identity,old_reports_unchanged=True)
    return command(engine,actor,'correct:'+identity,body,key,apply,lambda c:event_scope(c,actor,identity))


def compare(c,actor,before,after):
    left=get(c,actor,before);right=get(c,actor,after)
    filters=('warehouse_id','sku_id','supplier_id','connection_id','calendar','waiting_hours','registration_hours','min_samples')
    mismatches=[k for k in filters if left['request'][k]!=right['request'][k]]
    if left['rule_version']!=right['rule_version']:mismatches.append('rule_version')
    a,b=left['summary']['metrics'],right['summary']['metrics'];items=[]
    for metric in sorted(set(a)|set(b)):
        one,two=a.get(metric,{}),b.get(metric,{})
        delta={k:two[k]-one[k] if one.get(k) is not None and two.get(k) is not None else None for k in ('median_hours','p95_hours','open_quantity','completed_quantity','coverage')}
        items.append(dict(metric=metric,before=one,after=two,delta=delta))
    return dict(before=before,after=after,comparable=not mismatches,mismatches=mismatches,items=items,
        interpretation='两个观察期间的描述性比较；业务组合或时间窗口变化仍可能影响结果，不代表改善措施的因果效果')
