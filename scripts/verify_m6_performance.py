"""Run real API capacity measurements in a dedicated registered simulation."""
import argparse,json,platform,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.control import Control
from simulator.config import profile
from simulator.common import read_json,write_json
from simulator.isolation import Worker

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--tier',choices=['small','medium','large'],default='medium');args=p.parse_args()
    skus,warehouses,orders={'small':(100,2,1000),'medium':(1000,5,10000),'large':(10000,10,100000)}[args.tier]
    control=Control.initialize(args.root);item=control.create(profile('normal',seed={'small':966600,'medium':966602,'large':966604}[args.tier],purpose='development',sku_count=skus,warehouse_count=warehouses,order_count=orders))
    path,manifest=control.verify(item['run_id']);masters=read_json(path/'inputs.json')['masters'];worker=None
    start=time.monotonic()
    try:
        with control.lock(item['run_id']) as lock:
            worker=Worker(path,manifest,lock_fd=lock).start();worker.call('initialize',masters=masters,timeout=120)
            for offset in range(0,orders,100):
                result=worker.call('m6_capacity_populate',master=masters[0],offset=offset,count=min(100,orders-offset),timeout=120)
                assert result.get('created')==min(100,orders-offset),result
                if (offset+100)%1000==0:print(json.dumps(dict(fixture_orders=offset+100,total=orders)),flush=True)
            result=worker.call('m6_capacity_measure',master=masters[0],orders=orders,timeout=300)
            result.update(tier=args.tier,run_id=item['run_id'],code_version=manifest['code_version'],host=dict(system=platform.platform(),machine=platform.machine()),
                fixture_and_measurement_seconds=time.monotonic()-start,not_measured=['并发多用户','真实外部连接','模型响应时间'])
            write_json(path/'reports/performance.json',result);print(json.dumps(dict(report=str(path/'reports/performance.json'),**result),ensure_ascii=False))
            raise SystemExit(0 if result.get('passed') else 1)
    finally:
        if worker:worker.close()

if __name__=='__main__':main()
