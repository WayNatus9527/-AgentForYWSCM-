"""Register all independent seeds before executing; never omit failed cases."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import random
import secrets
import time
from .common import write_json
from .config import profile, SCENARIOS, FAULTS
from .runner import run


def verify_batch(control, *, count=100, workers=2, backend='sqlite', seed_start=None, purpose='validation'):
    if count<1 or count>1000 or workers<1 or workers>8: raise ValueError('Count 1..1000 and workers 1..8 required')
    if purpose not in ('validation','regression'): raise ValueError('Batch reports must be regression or validation')
    seed_start=seed_start if seed_start is not None else secrets.randbelow(2**48)
    batch_id='batch-'+secrets.token_hex(8)
    directory=control.root/'archives'/batch_id; directory.mkdir(mode=0o700)
    cases=[]
    for index in range(count):
        seed=seed_start+index; rng=random.Random(seed ^ 0xA8E3)
        config=profile(SCENARIOS[index%len(SCENARIOS)],purpose=purpose,seed=seed,
                       sku_count=rng.randint(1,4),warehouse_count=rng.randint(2,4),order_count=rng.randint(5,12),
                       supplier_lead_days=rng.randint(1,5),sales_distribution=('uniform','peaked','long_tail')[index%3],
                       return_rate=rng.choice([0.0,0.2,0.8,1.0]),inspection_rate=rng.choice([0.0,0.25,1.0]),
                       quality_units_per_hour=rng.choice([1,10,100]),shipping_units_per_hour=rng.choice([1,50,200]))
        registered=control.create(config,backend=backend)
        cases.append(dict(run_id=registered['run_id'],config=config.model_dump()))
    write_json(directory/'batch-manifest.json',dict(batch_id=batch_id,purpose=purpose,backend=backend,
               policy='all registered cases must be reported, including failures',cases=cases),immutable=True)
    start=time.monotonic()
    def execute(case):
        try: report=run(control,case['run_id'])
        except Exception: report=control.report(case['run_id'])
        if report['status']=='failed':
            try: control.promote(case['run_id'])
            except FileExistsError: pass
        return dict(run_id=case['run_id'],seed=case['config']['seed'],scenario=case['config']['scenario'],
                    status=report['status'],checks=report.get('business',{}).get('checks',0),
                    input_sha256=report.get('input_sha256'),error=report.get('error'),
                    failures=report.get('failures',[]))
    with ThreadPoolExecutor(max_workers=workers) as pool: results=list(pool.map(execute,cases))
    report=dict(batch_id=batch_id,purpose=purpose,backend=backend,workers=workers,
                elapsed_seconds=round(time.monotonic()-start,3),passed=sum(r['status']=='passed' for r in results),
                failed=sum(r['status']!='passed' for r in results),checks=sum(r['checks'] for r in results),
                scenarios=dict(Counter(x['config']['scenario'] for x in cases)),
                coverage={key:dict(Counter(str(x['config'][key]) for x in cases)) for key in
                          ('sku_count','warehouse_count','return_rate','inspection_rate','sales_distribution','quality_units_per_hour','shipping_units_per_hour')},
                fault_coverage=dict(Counter(f for x in cases for f in x['config']['faults'])),cases=results)
    write_json(directory/'report.json',report)
    return report | {'report_path':str(directory/'report.json')}
