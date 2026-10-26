"""Run process-isolated M2 acceptance and retain every generated input/report."""
import argparse
from pathlib import Path
import sys
import os
import secrets
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.common import canonical
from simulator.control import Control
from simulator.config import profile
from simulator.runner import run, replay


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--mode',choices=('smoke','batch','pytest','postgres-suite','regression'),default='smoke')
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--m3-count',type=int,default=0)
    parser.add_argument('--m4-count',type=int,default=0)
    parser.add_argument('--m4-seed-start',type=int)
    parser.add_argument('--m5-count',type=int,default=0)
    parser.add_argument('--m5-seed-start',type=int)
    parser.add_argument('--m6-count',type=int,default=0)
    parser.add_argument('--m6-seed-start',type=int)
    parser.add_argument('--backend',choices=('sqlite','postgresql'),default='sqlite')
    parser.add_argument('--pg-url-file')
    parser.add_argument('--test-path',default='tests/test_m2_simulator.py')
    args=parser.parse_args()
    if args.mode=='postgres-suite':
        from pgserver import get_server
        from sqlalchemy.engine import make_url
        from psycopg import sql
        from simulator.postgres import connection
        from simulator.batch import verify_batch
        from simulator.common import write_json
        import pytest
        root=Path(args.root); root.parent.mkdir(parents=True,exist_ok=True)
        pgdata=root.parent/('postgres-'+secrets.token_hex(6))
        with get_server(pgdata,cleanup_mode='stop') as server:
            url=make_url(server.get_uri()).set(drivername='postgresql+psycopg')
            password=secrets.token_urlsafe(32)
            with connection(url.render_as_string(hide_password=False)) as c:
                c.execute("SET password_encryption='scram-sha-256'")
                c.execute(sql.SQL('ALTER ROLE postgres PASSWORD {}').format(sql.Literal(password)))
                (pgdata/'pg_hba.conf').write_text('local all all scram-sha-256\nhost all all 127.0.0.1/32 scram-sha-256\nhost all all ::1/128 scram-sha-256\n')
                c.execute('SELECT pg_reload_conf()')
            urlfile=pgdata/'test-url'
            fd=os.open(urlfile,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:f.write(url.set(password=password).render_as_string(hide_password=False))
            os.environ['M2_PG_URL_FILE']=str(urlfile)
            os.environ['TEST_DATABASE_URL_FILE']=str(urlfile)
            result=pytest.main(['-q',args.test_path,'--tb=short'])
            if result: raise SystemExit(result)
            control=Control.initialize(str(root),str(urlfile))
            if args.count:
                report=verify_batch(control,count=args.count,backend='postgresql')
                print(canonical(report))
                if report['failed']:raise SystemExit(1)
            if args.m3_count:
                from simulator.m3_validation import batch as m3_batch
                m3_report=m3_batch(control,args.m3_count,backend='postgresql')
                print(canonical(m3_report))
                if m3_report['failed']:raise SystemExit(1)
            if args.m4_count:
                from simulator.m3_validation import batch as workflow_batch
                m4_report=workflow_batch(control,args.m4_count,backend='postgresql',stage='M4',seed_start=args.m4_seed_start)
                print(canonical(m4_report))
                if m4_report['failed']:raise SystemExit(1)
            if args.m5_count:
                from simulator.m3_validation import batch as workflow_batch
                report=workflow_batch(control,args.m5_count,backend='postgresql',stage='M5',seed_start=args.m5_seed_start)
                print(canonical(report))
                if report['failed']:raise SystemExit(1)
            if args.m6_count:
                from simulator.m3_validation import batch as workflow_batch
                report=workflow_batch(control,args.m6_count,backend='postgresql',stage='M6',seed_start=args.m6_seed_start)
                print(canonical(report))
                if report['failed']:raise SystemExit(1)
        return
    if args.mode=='pytest':
        import pytest
        raise SystemExit(pytest.main(['-q',args.test_path,'--tb=short']))
    control=Control.initialize(args.root,args.pg_url_file)
    if args.mode=='regression':
        from simulator.config import Scenario
        from simulator.common import read_json,write_json
        results=[]
        for file in sorted((Path(__file__).resolve().parents[1]/'simulator/scenarios').glob('*.json')):
            config=Scenario.model_validate(read_json(file))
            if config.purpose!='regression': continue
            item=control.create(config,backend=args.backend)
            try: report=run(control,item['run_id'])
            except Exception: report=control.report(item['run_id'])
            results.append(report)
        summary=dict(purpose='regression',passed=sum(r['status']=='passed' for r in results),cases=results)
        write_json(control.root/'archives/fixed-regression-summary.json',summary)
        print(canonical(summary)); raise SystemExit(0 if summary['passed']==len(results) else 1)
    if args.mode=='batch':
        from simulator.batch import verify_batch
        output=verify_batch(control,count=args.count,backend=args.backend)
        print(canonical(output)); raise SystemExit(0 if output['failed']==0 else 1)
    item=control.create(profile('mixed',sku_count=2,order_count=4),backend=args.backend)
    try:
        output=run(control,item['run_id'])
        print(canonical(output))
        if output['status']=='passed': print(canonical({'replay':replay(control,item['run_id'])}))
    except Exception:
        print((control.root/'runs'/item['run_id']/'worker.log').read_text()[-8000:])
        raise
    raise SystemExit(0 if output['status']=='passed' else 1)


if __name__=='__main__': main()
