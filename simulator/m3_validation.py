"""Independent M3/M4/M5/M6 datasets, one OS-restricted worker and database per run."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import random,secrets,time
from .config import profile
from .common import read_json,write_json,digest
from .isolation import Worker


def batch(control,count=100,backend='sqlite',purpose='validation',seed_start=None,stage='M3'):
    if stage not in ('M3','M4','M5','M6'): raise ValueError('Unsupported stage')
    field=stage.lower()
    seed_start=seed_start if seed_start is not None else secrets.randbelow(2**48)
    if not 1<=count<=1000:raise ValueError('Expected 1..1000 cases')
    directory=control.root/'archives'/(field+'-'+secrets.token_hex(8));directory.mkdir(mode=0o700)
    cases=[]
    for i in range(count):
        seed=seed_start+i;rng=random.Random(seed)
        config=profile('normal',seed=seed,purpose=purpose,sku_count=rng.randint(1,4),warehouse_count=rng.randint(2,4),order_count=5)
        item=control.create(config,backend=backend,**{field:True})
        cases.append(dict(run_id=item['run_id'],seed=seed,config=config.model_dump()))
    write_json(directory/'manifest.json',dict(purpose=purpose,stage=stage,backend=backend,cases=cases),immutable=True)
    def execute(item):
        try:return run(control,item['run_id'],stage=stage)
        except Exception as exc:
            return dict(run_id=item['run_id'],seed=item['seed'],stage=stage,status='failed',checks=0,error=str(exc))
    start=time.monotonic()
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(execute,cases))
    report=dict(stage=stage,purpose=purpose,backend=backend,workers=2,passed=sum(r['status']=='passed' for r in results),failed=sum(r['status']!='passed' for r in results),
        checks=sum(r['checks'] for r in results),elapsed_seconds=round(time.monotonic()-start,3),
        coverage={k:dict(Counter(str(read_json(control.root/'runs'/x['run_id']/'inputs.json')[field][k]) for x in cases)) for k in (('conflict','count_delta','quality_good','quality_bad','sale','return_good') if stage=='M3' else ('grant','restore','finish','inbox_cancel','fault','snapshot_order','quantity') if stage=='M4' else ('kind','variant','fault','quality_queue_hours','picking_queue_hours','actual_quality') if stage=='M6' else ('supply_due','demand_quality','includes_open_orders','reserve_order','close_remaining','fault','pack','transfer_lost'))},cases=results)
    if stage=='M6':
        from .m6_validation import diagnostics_summary
        report.update(diagnostics_summary(results,cases,control))
    write_json(directory/'report.json',report)
    return report|{'report_path':str(directory/'report.json')}


def run(control,identity,stage='M3'):
    if stage not in ('M3','M4','M5','M6'): raise ValueError('Unsupported stage')
    field=stage.lower()
    from . import m3_data,m4_data,m5_data,m6_data
    oracle={'M3':m3_data.expected,'M4':m4_data.expected,'M5':m5_data.expected,'M6':m6_data.expected}[stage]
    start=time.monotonic()
    with control.lock(identity) as lock:
        path,manifest=control.verify(identity);dataset=read_json(path/'inputs.json');worker=None
        old=control.report(identity)
        if old.get('status')=='passed':return old
        if old.get('stage')==stage and old.get('status')=='failed':
            raise ValueError(stage+' acceptance is retained after failure; reset into a fresh run to rerun')
        report=dict(run_id=identity,seed=manifest['seed'],backend=manifest['backend'],purpose=manifest['purpose'],stage=stage,status='failed',checks=0)
        try:
            control.update(identity,'running');worker=Worker(path,manifest,lock_fd=lock).start()
            assert worker.call('initialize',masters=dataset['masters']).get('initialized')
            found=worker.call(field,masters=dataset['masters'],case=dataset[field])
            if stage=='M4' and dataset[field]['fault']=='worker_crash':
                from .isolation import WorkerLost
                try: worker.call('m4_delivery_crash',tenant_id=dataset['masters'][0]['tenant_id'])
                except WorkerLost: pass
                else: raise AssertionError('M4 crash did not occur')
                assert worker.process.wait(timeout=5)==94
                worker.close(); time.sleep(1.1)
                worker=Worker(path,manifest,lock_fd=lock).start()
                recovered=worker.call('m4_recover',masters=dataset['masters'],case=dataset[field])
                found['trace'].extend(recovered['trace']); found['trace'].append(dict(name='actual-outbox-crash-recovered',passed=True))
                found['snapshot']=recovered['snapshot']
            if stage=='M5' and dataset[field]['fault']=='restart_after_confirm':
                from .isolation import WorkerLost
                try: worker.call('m5_confirmation_crash',tenant_id=dataset['masters'][0]['tenant_id'])
                except WorkerLost: pass
                else: raise AssertionError('M5 crash did not occur')
                assert worker.process.wait(timeout=5)==95
                worker.close(); worker=Worker(path,manifest,lock_fd=lock).start()
                recovered=worker.call('m5_recover',masters=dataset['masters'],case=dataset[field])
                found['trace'].extend(recovered['trace']);found['snapshot']=recovered['snapshot']
            if stage=='M6' and dataset[field]['fault']=='after_report_commit':
                from .isolation import WorkerLost
                try:worker.call('m6_report_crash',tenant_id=dataset['masters'][0]['tenant_id'])
                except WorkerLost:pass
                else:raise AssertionError('M6 crash did not occur')
                assert worker.process.wait(timeout=5)==96
                worker.close();worker=Worker(path,manifest,lock_fd=lock).start()
                recovered=worker.call('m6_recover',masters=dataset['masters'],case=dataset[field])
                found['trace'].extend(recovered['trace']);found.update(snapshot=recovered['snapshot'],diagnoses=recovered['diagnoses'])
            if stage=='M6':
                target_diagnosis=m6_data.diagnosis(dataset[field]);report['diagnoses']=found.get('diagnoses',{});report['expected_diagnosis']=target_diagnosis
                for tenant,diagnosis in found.get('diagnoses',{}).items():found['trace'].append(dict(name=tenant+':independent-diagnosis',passed=diagnosis==target_diagnosis))
                found['trace'].append(dict(name='complete-diagnostic-denominator',passed=len(found.get('diagnoses',{}))==len(dataset['masters'])))
            if stage=='M5':
                expected_plans=m5_data.expected_plans(dataset['masters'],dataset[field])
                found['trace'].append(dict(name='independent-planning-quantities',passed=found['planning']==expected_plans))
            write_json(path/('reports/'+field+'-trace.json'),found)
            if 'snapshot' not in found:raise RuntimeError(stage+' worker failed; retained worker.log')
            target=oracle(dataset['masters'],dataset[field])
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
            write_json(control.root/'archives'/(field+'-regression-'+str(manifest['seed'])+'.json'),dict(config=manifest['config'],inputs=dataset,report=report))
        return report
