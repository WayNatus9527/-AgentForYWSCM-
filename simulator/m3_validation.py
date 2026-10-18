"""Independent M3 datasets, one OS-restricted worker and database per run."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import random,secrets,time
from .config import profile
from .common import read_json,write_json,digest
from .isolation import Worker
from .m3_data import expected


def batch(control,count=100,backend='sqlite',purpose='validation',seed_start=None):
    seed_start=seed_start if seed_start is not None else secrets.randbelow(2**48)
    if not 1<=count<=1000:raise ValueError('Expected 1..1000 cases')
    directory=control.root/'archives'/('m3-'+secrets.token_hex(8));directory.mkdir(mode=0o700)
    cases=[]
    for i in range(count):
        seed=seed_start+i;rng=random.Random(seed)
        config=profile('normal',seed=seed,purpose=purpose,sku_count=rng.randint(1,4),warehouse_count=rng.randint(2,4),order_count=5)
        item=control.create(config,backend=backend,m3=True)
        cases.append(dict(run_id=item['run_id'],seed=seed,config=config.model_dump()))
    write_json(directory/'manifest.json',dict(purpose=purpose,stage='M3',backend=backend,cases=cases),immutable=True)
    def execute(item):
        try:return run(control,item['run_id'])
        except Exception as exc:
            return dict(run_id=item['run_id'],seed=item['seed'],stage='M3',status='failed',checks=0,error=str(exc))
    start=time.monotonic()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(execute,cases))
    report=dict(stage='M3',purpose=purpose,backend=backend,workers=2,passed=sum(r['status']=='passed' for r in results),failed=sum(r['status']!='passed' for r in results),
        checks=sum(r['checks'] for r in results),elapsed_seconds=round(time.monotonic()-start,3),
        coverage={k:dict(Counter(str(read_json(control.root/'runs'/x['run_id']/'inputs.json')['m3'][k]) for x in cases)) for k in ('conflict','count_delta','quality_good','quality_bad','sale','return_good')},cases=results)
    write_json(directory/'report.json',report)
    return report|{'report_path':str(directory/'report.json')}


def run(control,identity):
    start=time.monotonic()
    with control.lock(identity) as lock:
        path,manifest=control.verify(identity);dataset=read_json(path/'inputs.json');worker=None
        old=control.report(identity)
        if old.get('status')=='passed':return old
        if old.get('stage')=='M3' and old.get('status')=='failed':
            raise ValueError('M3 acceptance is retained after failure; reset into a fresh run to rerun')
        report=dict(run_id=identity,seed=manifest['seed'],backend=manifest['backend'],purpose=manifest['purpose'],stage='M3',status='failed',checks=0)
        try:
            control.update(identity,'running');worker=Worker(path,manifest,lock_fd=lock).start()
            assert worker.call('initialize',masters=dataset['masters']).get('initialized')
            found=worker.call('m3',masters=dataset['masters'],case=dataset['m3'])
            write_json(path/'reports/m3-trace.json',found)
            if 'snapshot' not in found:raise RuntimeError('M3 worker failed; retained worker.log')
            target=expected(dataset['masters'],dataset['m3'])
            checks=[*found['trace'],dict(name='independent-final-quantities',passed=target==found['snapshot']['balances']),
                dict(name='permissions',passed=all(found['snapshot']['permission_checks'].values()))]
            probe=worker.call('probe',path=str(path/'private/truth.json'))
            checks.append(dict(name='private-truth-inaccessible',passed=probe=={'read':'denied','write':'denied'}))
            report.update(status='passed' if all(x['passed'] for x in checks) else 'failed',checks=len(checks),failures=[x for x in checks if not x['passed']],
                expected_sha256=digest(target),actual_sha256=digest(found['snapshot']['balances']),input_sha256=manifest['input_sha256'])
        except Exception as exc:report['error']=str(exc)
        finally:
            if worker:worker.close()
        report['elapsed_seconds']=round(time.monotonic()-start,3)
        write_json(path/'reports/report.json',report);control.update(identity,report['status'])
        if report['status']=='failed':
            # Retain the full public case plus private oracle and registered
            # run identity; no failed seed disappears from the denominator.
            write_json(control.root/'archives'/('m3-regression-'+str(manifest['seed'])+'.json'),dict(config=manifest['config'],inputs=dataset,report=report))
        return report
