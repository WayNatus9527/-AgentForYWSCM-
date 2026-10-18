import argparse
from pathlib import Path
from .common import canonical, read_json
from .config import profile, Scenario, SCENARIOS
from .control import Control


def main():
    parser=argparse.ArgumentParser(description='独立模拟运行管理；仅管理已登记的模拟空间')
    parser.add_argument('--root',required=True,help='绝对路径；与业务数据库目录分开')
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init'); init.add_argument('--pg-url-file')
    create=commands.add_parser('create'); create.add_argument('--scenario',choices=SCENARIOS,default='normal')
    create.add_argument('--purpose',choices=('development','regression','validation'),default='development')
    create.add_argument('--seed',type=int,default=20261017); create.add_argument('--config')
    create.add_argument('--backend',choices=('sqlite','postgresql'),default='sqlite'); create.add_argument('--run-id')
    commands.add_parser('list')
    for name in ('run','report','replay','promote','reset','destroy'):
        sub=commands.add_parser(name); sub.add_argument('run_id')
        if name=='run': sub.add_argument('--until',help='带时区的业务时间；逐事件推进')
        if name in ('reset','destroy'): sub.add_argument('--fingerprint',required=True)
    batch=commands.add_parser('verify'); batch.add_argument('--count',type=int,default=100)
    batch.add_argument('--workers',type=int,default=2); batch.add_argument('--seed-start',type=int)
    batch.add_argument('--backend',choices=('sqlite','postgresql'),default='sqlite')
    web=commands.add_parser('web'); web.add_argument('--port',type=int,default=8020)
    workbench=commands.add_parser('workbench'); workbench.add_argument('run_id'); workbench.add_argument('--port',type=int,default=8021)
    args=parser.parse_args()
    if args.command=='init':
        control=Control.initialize(args.root,args.pg_url_file); output={'root':str(control.root),'initialized':True}
    else:
        control=Control(args.root)
        if args.command=='create':
            config=Scenario.model_validate(read_json(args.config)) if args.config else profile(args.scenario,purpose=args.purpose,seed=args.seed)
            output=control.create(config,run_id=args.run_id,backend=args.backend)
        elif args.command=='list': output=control.list()
        elif args.command=='report': output=control.report(args.run_id)
        elif args.command in ('reset','destroy'): output=getattr(control,args.command)(args.run_id,args.fingerprint)
        elif args.command=='promote': output={'regression_file':control.promote(args.run_id)}
        elif args.command=='run':
            from .runner import run
            output=run(control,args.run_id,until=args.until)
        elif args.command=='replay':
            from .runner import replay
            output=replay(control,args.run_id)
        elif args.command=='verify':
            from .batch import verify_batch
            output=verify_batch(control,count=args.count,workers=args.workers,seed_start=args.seed_start,backend=args.backend)
        elif args.command=='web':
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(control),host='127.0.0.1',port=args.port)
            return
        elif args.command=='workbench':
            from .isolation import Worker
            with control.lock(args.run_id) as fd:
                path,manifest=control.verify(args.run_id)
                with Worker(path,manifest,fd) as worker:
                    worker.call('initialize',masters=read_json(path/'inputs.json')['masters'])
                control.audit(args.run_id,'workbench_opened')
                print(canonical({'url':f'http://127.0.0.1:{args.port}/','run_id':args.run_id,
                      'login_file':str(path/'business/login.json'),'username':'tenant-0-admin'}),flush=True)
                with Worker(path,manifest,fd,serve_port=args.port) as worker:
                    try: worker.process.wait()
                    except KeyboardInterrupt: pass
                control.audit(args.run_id,'workbench_closed')
            return
    print(canonical(output))


if __name__=='__main__': main()
