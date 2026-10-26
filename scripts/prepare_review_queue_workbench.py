"""Fresh development workbench with a failed job and a job awaiting startup recovery."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.control import Control
from simulator.m3_validation import batch
from simulator.isolation import Worker
from simulator.common import read_json,write_json


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);args=p.parse_args()
    control=Control.initialize(args.root);result=batch(control,1,purpose='development',seed_start=960020,stage='M6')
    assert result['failed']==0
    identity=result['cases'][0]['run_id']
    with control.lock(identity) as lock:
        path,manifest=control.verify(identity);master=read_json(path/'inputs.json')['masters'][0]
        with Worker(path,manifest,lock_fd=lock) as worker:ids=worker.call('review_queue_fixture',master=master)
    fixture=dict(run_id=identity,**ids,login_file=str(path/'business/login.json'))
    write_json(control.root/'queue-browser-fixture.json',fixture);print(json.dumps(fixture,ensure_ascii=False))


if __name__=='__main__':main()
