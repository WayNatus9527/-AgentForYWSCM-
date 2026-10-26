from pathlib import Path
import pytest
from simulator import m6_data
from simulator.runner import run,replay
from test_m2_simulator import control,backend,small,isolated

def test_independent_gold_and_paired_intervention():
    case=m6_data.generate(4)|dict(kind='mixed',quality_queue_hours=48,picking_queue_hours=36,transit_hours=120,arrival_offsets=[0],quantities=[10],registration_hours=0,linked=True,actual_quality='reported',stock_available=False)
    expected=m6_data.diagnosis(case)
    assert expected['constraints']==['pick_wait','quality_wait'] and expected['constraint_quantity']==10
    assert expected['order_ship_hours']==206.5
    improved=m6_data.diagnosis(case|{'quality_queue_hours':8})
    assert improved['constraints']==['pick_wait'] and expected['order_ship_hours']-improved['order_ship_hours']==40
    assert 'from app' not in Path(m6_data.__file__).read_text()
    one,two=m6_data.generate(6),m6_data.generate(7)
    changes={k for k in one if one[k]!=two[k]}
    assert changes<=set(('seed','variant','quality_queue_hours'))

@isolated
@pytest.mark.parametrize('fault',['none','after_report_commit'])
def test_m6_isolated_diagnostics_and_report_crash(control,fault):
    seed=next(n for n in range(960000,960100) if m6_data.generate(n)['fault']==fault)
    item=control.create(small(seed=seed,purpose='validation'),backend=backend(control),m6=True)
    result=run(control,item['run_id']);assert result['status']=='passed',result
    assert run(control,item['run_id'])==result
    with pytest.raises(ValueError,match='fresh reset'):replay(control,item['run_id'])
    successor=control.reset(item['run_id'],item['fingerprint'])
    assert control.verify(successor['run_id'])[1]['acceptance_stage']=='M6'
    assert run(control,successor['run_id'])['status']=='passed'
    control.destroy(successor['run_id'],successor['fingerprint'])


@isolated
def test_real_process_death_before_publish_then_continuous_recovery(control):
    import time
    from simulator.isolation import Worker,WorkerLost
    from simulator.common import read_json
    item=control.create(small(seed=961010,purpose='validation'),backend=backend(control),m6=True)
    path,manifest=control.verify(item['run_id']);master=read_json(path/'inputs.json')['masters'][0]
    with control.lock(item['run_id']) as lock:
        worker=Worker(path,manifest,lock_fd=lock).start()
        try:
            worker.call('initialize',masters=read_json(path/'inputs.json')['masters'])
            ids=worker.call('review_queue_fixture',master=master)
            with pytest.raises(WorkerLost):worker.call('review_queue_crash',identity=ids['queued_job'])
            assert worker.process.wait(timeout=5)==97
        finally:worker.close()
        time.sleep(.2)
        with Worker(path,manifest,lock_fd=lock) as recovered:
            result=recovered.call('review_queue_recover',master=master,identities=ids)
            assert (result['reports'],result['queued_attempts'],result['retried_attempts'],result['stopped'])==(2,2,4,True)
            assert sum(x['event']=='lease_expired' for x in result['history'])==1
            assert sum(x['event']=='retry_requested' for x in result['history'])==1
            assert recovered.call('probe',path=str(path/'private/truth.json'))=={'read':'denied','write':'denied'}
