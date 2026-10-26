"""Fresh SQLite release batch: 600 runs, 500 independent scenario groups."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.control import Control
from simulator.batch import verify_batch
from simulator.m3_validation import batch
from simulator.common import code_version,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);args=p.parse_args()
    control=Control.initialize(args.root);reports=[]
    for index,stage in enumerate(('M2','M3','M4','M5','M6'),1):
        count=200 if stage=='M6' else 100;seed=2600000000+index*1000000
        report=verify_batch(control,count=count,seed_start=seed) if stage=='M2' else batch(control,count,seed_start=seed,stage=stage)
        compact={k:v for k,v in report.items() if k not in ('cases','paired_experiments')};compact['stage']=stage;reports.append(compact)
        print(json.dumps(compact,ensure_ascii=False),flush=True)
    result=dict(code_version=code_version(),backend='sqlite',independent_groups=sum(r.get('independent_scenario_groups',r['passed']+r['failed']) for r in reports),
        runs=sum(r['passed']+r['failed'] for r in reports),passed=sum(r['passed'] for r in reports),failed=sum(r['failed'] for r in reports),stages=reports)
    write_json(control.root/'archives/release-summary.json',result)
    raise SystemExit(bool(result['failed']))


if __name__=='__main__':main()
