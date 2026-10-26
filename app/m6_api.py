"""Bounded process review, immutable timeline and controlled Agent endpoints."""
from typing import Literal
from fastapi import Request,BackgroundTasks
from . import review,review_agent,m6_models as m
from .phase2 import Key
from .m4_api import Offset,Limit

def install(app,engine,actor_for):
    @app.get('/api/v2/review-agent/status')
    def model_status(request:Request):
        from .domain import require_role
        from .m5_common import READ
        from .config import settings_for
        from .agent_model import status
        actor,_=actor_for(request);require_role(actor,*READ)
        return status(settings=settings_for(engine))

    @app.post('/api/v2/reviews/jobs',status_code=202)
    def create(body:m.Analyze,request:Request,idempotency_key:Key,background_tasks:BackgroundTasks):
        actor,_=actor_for(request,True);job=review.create(engine,actor,body.model_dump(),idempotency_key)
        background_tasks.add_task(review.dispatch,engine,job['id']);return job

    @app.get('/api/v2/reviews/jobs')
    def jobs(request:Request,offset:Offset=0,limit:Limit=20):
        actor,_=actor_for(request);return review.jobs(engine,actor,offset,limit)

    @app.get('/api/v2/reviews/worker-status')
    def worker_status(request:Request):
        from .domain import require_role
        actor,_=actor_for(request);require_role(actor,'admin')
        worker=getattr(app.state,'review_supervisor',None)
        value=worker.status() if worker else dict(enabled=False,alive=False)
        return {k:v for k,v in value.items() if k not in ('reports_observed','scans')}

    @app.post('/api/v2/reviews/jobs/{identity}/retry',status_code=202)
    def retry(identity:str,body:m.Retry,request:Request,idempotency_key:Key,background_tasks:BackgroundTasks):
        actor,_=actor_for(request,True);job=review.retry(engine,actor,identity,body.model_dump(),idempotency_key)
        background_tasks.add_task(review.dispatch,engine,job['id']);return job

    @app.get('/api/v2/reviews/jobs/{identity}/history')
    def history(identity:str,request:Request,offset:Offset=0,limit:Limit=20):
        actor,_=actor_for(request)
        with engine.connect() as c:return review.job_timeline(c,actor,identity,offset,limit)

    @app.get('/api/v2/reviews/jobs/{identity}')
    def job(identity:str,request:Request):
        actor,_=actor_for(request)
        with engine.connect() as c:return review.get_job(c,actor,identity)

    @app.get('/api/v2/reviews/reports')
    def reports(request:Request,offset:Offset=0,limit:Limit=20):
        actor,_=actor_for(request);return review.listing(engine,actor,offset,limit)

    @app.get('/api/v2/reviews/reports/{identity}')
    def report(identity:str,request:Request):
        actor,_=actor_for(request)
        with engine.connect() as c:return review.get(c,actor,identity)

    @app.get('/api/v2/reviews/reports/{identity}/rows/{category}')
    def rows(identity:str,category:Literal['sample','candidate','impact','event','input'],request:Request,offset:Offset=0,limit:Limit=20,object_id:str|None=None,metric:str|None=None):
        actor,_=actor_for(request)
        with engine.connect() as c:return review.page(c,actor,identity,category,offset,limit,object_id,metric)

    @app.post('/api/v2/reviews/rows/{identity}/improve')
    def improve(identity:str,body:m.Improvement,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return review.improve(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.post('/api/v2/reviews/events/{identity}/correct')
    def correct(identity:str,body:m.Correct,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return review.correct(engine,actor,identity,body.model_dump(),idempotency_key)

    @app.get('/api/v2/reviews/compare')
    def compare(before:str,after:str,request:Request):
        actor,_=actor_for(request)
        with engine.connect() as c:return review.compare(c,actor,before,after)

    @app.post('/api/v2/review-agent/tools')
    def tool(body:m.Tool,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return review_agent.execute(engine,actor,body.model_dump(),idempotency_key)

    @app.post('/api/v2/review-agent/ask')
    def ask(body:m.Ask,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return review_agent.ask(engine,actor,body.model_dump(),idempotency_key)
