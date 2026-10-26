"""M5 independent arithmetic and real commit-before-crash recovery."""
from pathlib import Path
import pytest
from simulator import m5_data
from simulator.runner import run,replay
from test_m2_simulator import control,backend,small,isolated


def test_m5_hand_calculated_gold_and_independent_generation():
    masters=[dict(tenant_id='A',warehouses=[dict(id='W1'),dict(id='W2')],skus=[dict(id='S',opening=100)])]
    case=m5_data.generate(1)|dict(good=10,quality_good=4,quality_bad=2,restocked=3,bad=1,remaining=20,
        transfer_sent=10,transfer_received=6,transfer_bad=1,transfer_lost=2,open_order=10,reserve_order=True,
        daily=[60,90,150],lead=2,coverage=3,safety=5,minimum=10,pack=6,includes_open_orders=True,
        demand_quality='known',supply_due='timely',close_remaining=False)
    rows=m5_data.expected(masters,case)
    assert [(r['g'],r['q'],r['d'],r['r']) for r in rows]==[(103,1,3,10),(5,0,1,0)]
    # Demand 500, safety 5, physical 103, timely supply 20 -> 382, round to 384.
    assert m5_data.expected_plans(masters,case)=={'A':384}
    assert m5_data.expected_plans(masters,case|{'demand_quality':'missing'})=={'A':None}
    assert m5_data.generate(1)==m5_data.generate(1)!=m5_data.generate(2)
    assert 'from app' not in Path(m5_data.__file__).read_text()


@isolated
@pytest.mark.parametrize('fault',['none','restart_after_confirm'])
def test_m5_isolated_run_restart_and_reset(control,fault):
    seed=next(n for n in range(930001,930100) if m5_data.generate(n)['fault']==fault)
    item=control.create(small(seed=seed,purpose='validation'),backend=backend(control),m5=True)
    path,manifest=control.verify(item['run_id']);assert manifest['acceptance_stage']=='M5'
    result=run(control,item['run_id']);assert result['status']=='passed',result
    assert run(control,item['run_id'])==result
    before=(path/'reports/report.json').read_bytes()
    with pytest.raises(ValueError,match='fresh reset'):replay(control,item['run_id'])
    successor=control.reset(item['run_id'],item['fingerprint'])
    assert control.verify(successor['run_id'])[1]['acceptance_stage']=='M5'
    assert run(control,successor['run_id'])['status']=='passed'
    control.destroy(successor['run_id'],successor['fingerprint'])
    assert (path/'reports/report.json').read_bytes()==before
