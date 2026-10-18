"""Separate local simulation administrator UI; no business login or DB selector."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import secrets
import time
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from .common import read_json, write_json
from .config import profile, SCENARIOS
from .runner import run, replay


class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token: str=Field(min_length=1,max_length=256)


class Create(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scenario: str='normal'
    purpose: str='development'
    seed: int=Field(ge=0,le=2**63-1,strict=True)
    backend: str='sqlite'


class Action(BaseModel):
    model_config=ConfigDict(extra='forbid')
    fingerprint: str|None=None
    until: str|None=None


def create_app(control):
    credential=control.root/'admin-token.json'
    if not credential.exists(): write_json(credential,{'token':secrets.token_urlsafe(32)})
    token=read_json(credential)['token']
    with control.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY,csrf TEXT,expires REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS admin_login_attempts(address TEXT,at REAL)')
    pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='simulation-run')
    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)
    app=FastAPI(title='跨境电商供应链管理Agent助手 · 模拟管理',version='0.6.0',lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def guard(request,call_next):
        if request.method in ('POST','DELETE','PUT'):
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):
                return JSONResponse({'error':'请求来源不允许'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({'error':str(exc)},status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation(request,exc):
        return JSONResponse({'detail':[{k:e[k] for k in ('loc','msg','type')} for e in exc.errors()]},status_code=422)

    def auth(request,write=False):
        key=hashlib.sha256(request.cookies.get('simulation_admin','').encode()).hexdigest()
        with control.connect() as c:
            session=c.execute('SELECT csrf FROM admin_sessions WHERE token_hash=? AND expires>?',(key,time.time())).fetchone()
        if not session: raise HTTPException(401,'请使用独立的模拟管理凭据登录')
        if write and not secrets.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
            raise HTTPException(403,'操作凭据失效')
        return session['csrf']

    @app.post('/api/session')
    def login(body:Login,request:Request,response:Response):
        address=request.client.host if request.client else 'unknown'
        with control.connect() as c:
            c.execute('DELETE FROM admin_login_attempts WHERE at<?',(time.time()-60,))
            count=c.execute('SELECT COUNT(*) FROM admin_login_attempts WHERE address=?',(address,)).fetchone()[0]
            if count>=10: raise HTTPException(429,'登录尝试过于频繁')
            c.execute('INSERT INTO admin_login_attempts VALUES(?,?)',(address,time.time()))
        if not secrets.compare_digest(body.token,token): raise HTTPException(401,'模拟管理凭据不正确')
        session=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
        with control.connect() as c:
            c.execute('DELETE FROM admin_sessions WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO admin_sessions VALUES(?,?,?)',(hashlib.sha256(session.encode()).hexdigest(),csrf,time.time()+8*3600))
        response.set_cookie('simulation_admin',session,httponly=True,samesite='strict',max_age=8*3600)
        return {'csrf':csrf}

    @app.get('/api/session')
    def session(request:Request): return {'csrf':auth(request)}

    @app.delete('/api/session')
    def logout(request:Request,response:Response):
        auth(request,True)
        key=hashlib.sha256(request.cookies.get('simulation_admin','').encode()).hexdigest()
        with control.connect() as c: c.execute('DELETE FROM admin_sessions WHERE token_hash=?',(key,))
        response.delete_cookie('simulation_admin'); return {'ok':True}

    @app.get('/api/runs')
    def list_runs(request:Request):
        auth(request)
        items=[]
        for row in control.list():
            row={k:v for k,v in row.items() if k!='manifest_hash'}
            if row['status']!='destroyed':
                try:
                    _,manifest=control.verify(row['run_id'],check_code=False)
                    row['fingerprint']=manifest['database_fingerprint']
                except ValueError: row['status']='invalid'
            items.append(row)
        return {'items':items,'postgresql_available':control.identity['postgres']}

    @app.post('/api/runs')
    def create_run(body:Create,request:Request):
        auth(request,True)
        if body.scenario not in SCENARIOS: raise ValueError('未知场景')
        return control.create(profile(body.scenario,purpose=body.purpose,seed=body.seed),backend=body.backend)

    @app.get('/api/runs/{run_id}/report')
    def report(run_id:str,request:Request):
        auth(request); return control.report(run_id)

    @app.get('/api/runs/{run_id}/export')
    def export(run_id:str,request:Request):
        auth(request)
        return JSONResponse(control.report(run_id),headers={'Content-Disposition':f'attachment; filename="{run_id}-report.json"'})

    @app.post('/api/runs/{run_id}/{action}')
    def action(run_id:str,action:str,body:Action,request:Request):
        auth(request,True)
        control.verify(run_id,body.fingerprint,check_code=action in ('run','replay'))
        if action in ('reset','destroy'):
            if not body.fingerprint: raise ValueError('清理或重置须校验目标运行指纹')
            return getattr(control,action)(run_id,body.fingerprint)
        if action not in ('run','replay'): raise HTTPException(404)
        # The actual lifecycle lock also rejects CLI/other-process competition.
        with control.lock(run_id): pass
        if action=='run': future=pool.submit(run,control,run_id,until=body.until)
        else: future=pool.submit(replay,control,run_id)
        def settled(task):
            try: task.result()
            except Exception as exc: control.audit(run_id,'background_error',type(exc).__name__)
        future.add_done_callback(settled)
        return JSONResponse({'accepted':True,'run_id':run_id},status_code=202)

    static=Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.get('/')
    def index(): return FileResponse(static/'index.html')
    return app
