"""M3 routes share authentication, CSRF, scope and transactional services."""
from typing import Annotated, Literal
from fastapi import Request, Query
from pydantic import Field
from sqlalchemy import select
from . import counting, exception_tasks as tasks, quality_control as qc, db
from .m3_models import (CountCreate, Action, CountObservation, ExceptionCreate, Assign, Resolution,
    TaskReview, InspectionStart, InspectionComplete, Restock, Scrap, SupplierReturn, DispositionReview)
from .phase2 import Key
from .domain import require_role
from .accounts import active
from .m3_common import read_actor

Offset=Annotated[int,Query(ge=0)]
Limit=Annotated[int,Query(ge=1,le=100)]


def install(app,engine,actor_for):
    @app.get('/api/v2/counts')
    def counts(request:Request,offset:Offset=0,limit:Limit=50):
        actor,_=actor_for(request);return counting.listing(engine,actor,offset,limit)
    @app.get('/api/v2/counts/{identity}')
    def count(identity:str,request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:
            read_actor(c,actor);return counting.view(c,actor,identity)
    @app.post('/api/v2/counts')
    def create_count(body:CountCreate,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return counting.create(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v2/counts/{identity}/observations/{action}')
    def observations(identity:str,action:Literal['initial','recount'],body:CountObservation,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return counting.act(engine,actor,identity,action,body.model_dump(),idempotency_key)
    @app.post('/api/v2/counts/{identity}/{action}')
    def count_action(identity:str,action:Literal['confirm','start','submit','approve','reject','cancel'],body:Action,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return counting.act(engine,actor,identity,action,body.model_dump(),idempotency_key)
    @app.get('/api/v2/count-locks')
    def locks(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:
            read_actor(c,actor);query=select(counting.locks).where(counting.locks.c.tenant_id==actor['tenant_id'])
            if actor['role']!='admin':query=query.where(counting.locks.c.warehouse_id.in_(actor['warehouse_ids']))
            return [dict(x) for x in c.execute(query.order_by(counting.locks.c.created_at,counting.locks.c.id)).mappings()]
    @app.get('/api/v2/exceptions')
    def exceptions(request:Request,offset:Offset=0,limit:Limit=50):
        actor,_=actor_for(request);return tasks.listing(engine,actor,offset,limit)
    @app.post('/api/v2/exceptions')
    def new_exception(body:ExceptionCreate,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.create(engine,actor,body.model_dump(),idempotency_key)
    @app.get('/api/v2/exception-owners')
    def owners(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:
            read_actor(c,actor)
            rows=c.execute(select(db.users).where(db.users.c.tenant_id==actor['tenant_id'],db.users.c.role.in_(('admin','warehouse')))).mappings()
            return [{k:x[k] for k in ('id','name','role','warehouse_ids')} for x in rows if active(c,x['id']) and (actor['role']=='admin' or x['id']==actor['id'])]
    @app.post('/api/v2/exceptions/remind')
    def remind(request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.remind(engine,actor,idempotency_key)
    @app.post('/api/v2/exceptions/{identity}/assign')
    def assign(identity:str,body:Assign,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.act(engine,actor,identity,'assign',body.model_dump(),idempotency_key)
    @app.post('/api/v2/exceptions/{identity}/resolve')
    def resolve(identity:str,body:Resolution,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.act(engine,actor,identity,'resolve',body.model_dump(),idempotency_key)
    @app.post('/api/v2/exceptions/{identity}/review/{action}')
    def task_review(identity:str,action:Literal['approve','reject'],body:TaskReview,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.act(engine,actor,identity,action,body.model_dump(),idempotency_key)
    @app.post('/api/v2/exceptions/{identity}/{action}')
    def task_action(identity:str,action:Literal['pause','resume','reopen'],body:Action,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return tasks.act(engine,actor,identity,action,body.model_dump(),idempotency_key)
    @app.get('/api/v2/inspections')
    def inspections(request:Request,offset:Offset=0,limit:Limit=50):
        actor,_=actor_for(request);return qc.listing(engine,actor,offset=offset,limit=limit)
    @app.post('/api/v2/inspections')
    def start_inspection(body:InspectionStart,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.start(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v2/inspections/{identity}/complete')
    def complete(identity:str,body:InspectionComplete,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.act(engine,actor,identity,'complete',body.model_dump(),idempotency_key)
    @app.post('/api/v2/inspections/{identity}/restock')
    def restock(identity:str,body:Restock,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.act(engine,actor,identity,'restock',body.model_dump(),idempotency_key)
    @app.post('/api/v2/inspections/{identity}/cancel')
    def cancel_inspection(identity:str,body:Action,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.act(engine,actor,identity,'cancel',body.model_dump(),idempotency_key)
    @app.get('/api/v2/quality-dispositions')
    def dispositions(request:Request,offset:Offset=0,limit:Limit=50):
        actor,_=actor_for(request);return qc.listing(engine,actor,qc.dispositions,offset,limit)
    @app.post('/api/v2/quality-dispositions')
    def disposition(body:Annotated[Scrap|SupplierReturn,Field(discriminator='kind')],request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.request_disposition(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v2/quality-dispositions/{identity}/review')
    def review_disposition(identity:str,body:DispositionReview,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return qc.review(engine,actor,identity,body.model_dump(),idempotency_key)
