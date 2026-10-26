"""M5 authenticated procurement, transport and deterministic planning routes."""
from typing import Literal
from fastapi import Request
from . import supply, planning, m5_models as m, m5_schema as s
from .phase2 import Key
from .m4_api import Offset, Limit

def install(app,engine,actor_for):
    @app.get('/api/v2/supply-orders/{kind}')
    def orders(kind:Literal['purchase','transfer'],request:Request,offset:Offset=0,limit:Limit=20):
        actor,_=actor_for(request);return supply.listing(engine,actor,kind,offset,limit)

    @app.post('/api/v2/purchase-plans')
    def purchase(body:m.Purchase,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.create(engine,actor,'purchase',body.model_dump(),idempotency_key)

    @app.post('/api/v2/transfer-plans')
    def transfer(body:m.Transfer,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.create(engine,actor,'transfer',body.model_dump(),idempotency_key)

    @app.post('/api/v2/purchase-plans/{identity}/promise')
    def promise(identity:str,body:m.Promise,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.update_promise(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.post('/api/v2/purchase-plans/scan-delays')
    def scan_delays(request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.scan_delays(engine,actor,idempotency_key)

    @app.post('/api/v2/supply-orders/{kind}/{identity}/batches')
    def batch(kind:Literal['purchase','transfer'],identity:str,body:m.Batch,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.make_batch(engine,actor,kind,identity,body.model_dump(),idempotency_key)

    @app.post('/api/v2/supply-orders/{kind}/{identity}/close')
    def close(kind:Literal['purchase','transfer'],identity:str,body:m.Action,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.close(engine,actor,kind,identity,body.model_dump(),idempotency_key)

    def action(name,model):
        def execute(identity:str,body,request:Request,idempotency_key:Key):
            actor,_=actor_for(request,True);return supply.batch_action(engine,actor,identity,name,body.model_dump(),idempotency_key)
        execute.__annotations__['body']=model;execute.__name__='supply_batch_'+name
        app.post('/api/v2/supply-batches/{identity}/'+name)(execute)
    for name,model in [('ship',m.BatchShip),('receive',m.BatchReceive),('cancel',m.BatchCancel)]:action(name,model)

    @app.post('/api/v2/supply-batches/{identity}/discrepancy')
    def discrepancy(identity:str,body:m.Claim,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.discrepancy(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.post('/api/v2/supply-discrepancies/{identity}/review')
    def review(identity:str,body:m.Review,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.review(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.post('/api/v2/supply-receipts/{identity}/inspect')
    def inspect(identity:str,body:m.Inspect,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return supply.inspect(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.get('/api/v2/planning/{kind}')
    def listing(kind:Literal['policies','plans','demand'],request:Request,offset:Offset=0,limit:Limit=20):
        actor,_=actor_for(request);return planning.listing(engine,actor,getattr(s,kind),offset,limit)

    @app.post('/api/v2/planning/demand')
    def demand(body:m.Demand,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return planning.demand_day(engine,actor,body.model_dump(),idempotency_key)

    @app.post('/api/v2/planning/policies')
    def policy(body:m.Policy,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return planning.policy(engine,actor,body.model_dump(),idempotency_key)

    @app.post('/api/v2/planning/policies/{identity}/calculate')
    def calculate(identity:str,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return planning.plan(engine,actor,identity,idempotency_key)

    @app.post('/api/v2/planning/plans/{identity}/confirm')
    def confirm(identity:str,body:m.Confirm,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return planning.confirm(engine,actor,identity,body.model_dump(),idempotency_key)
