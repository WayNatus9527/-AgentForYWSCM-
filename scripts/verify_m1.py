"""M1 independent datasets: python scripts/verify_m1.py --count 100 --output-dir ..."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import platform
import secrets
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validation.m1_scenarios import run_scenario


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count',type=int,default=10)
    parser.add_argument('--seed-start',type=int)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args()
    if not 1<=args.count<=1000 or not 1<=args.workers<=8:parser.error('count 1..1000; workers 1..8')
    start=args.seed_start if args.seed_start is not None else secrets.randbelow(1_000_000_000)
    root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'stage':'M1','generator_version':'m1-3','purpose':'independent_validation',
            'seeds':list(range(start,start+args.count)),'database':'SQLite, one file and engine per dataset',
            'workers':args.workers,'machine':{'platform':platform.platform(),'python':platform.python_version(),
                                           'processor':platform.machine()},'cases':[],
            'scope':'M1 quantity/idempotency/commitment checks; not full M2 simulator isolation acceptance'}
    started=time.monotonic()
    with ThreadPoolExecutor(args.workers) as pool:
        futures={pool.submit(run_scenario,s,root/('seed-'+str(s))):s for s in report['seeds']}
        for future in as_completed(futures):
            seed=futures[future]
            try:report['cases'].append(future.result())
            except Exception as exc:report['cases'].append({'seed':seed,'status':'failed','error':str(exc)})
    report['cases'].sort(key=lambda r:r['seed'])
    report['elapsed_seconds']=round(time.monotonic()-started,3)
    report['passed']=sum(r['status']=='passed' for r in report['cases'])
    report['failed']=args.count-report['passed']
    report['checks']=sum(r.get('checks',0) for r in report['cases'])
    (root/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:report[k] for k in ('passed','failed','checks','elapsed_seconds')},ensure_ascii=False))
    return 1 if report['failed'] else 0


if __name__=='__main__':raise SystemExit(main())
