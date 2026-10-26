"""M4 independent oracle and real isolated worker/restart acceptance."""
from pathlib import Path
import pytest
from simulator import m4_data
from simulator.common import read_json
from simulator.runner import run, replay
from test_m2_simulator import control, backend, small, isolated


def test_m4_hand_calculated_gold_and_independent_generation():
    masters = [dict(tenant_id='A', warehouses=[dict(id='W')], skus=[dict(id='S', opening=100)])]
    case = dict(shipped=3, cancelled=4, remaining=2, quantity=9, finish=False, grant=True, restore=True)
    row = m4_data.expected(masters, case)[0]
    assert (row['g'], row['r'], row['online']) == (97, 2, 11)
    row = m4_data.expected(masters, {**case, 'finish': True, 'restore': False})[0]
    assert (row['g'], row['r'], row['online']) == (95, 0, 7)
    assert m4_data.generate(1) == m4_data.generate(1) != m4_data.generate(2)
    assert 'from app' not in Path(m4_data.__file__).read_text()


@isolated
@pytest.mark.parametrize('fault', ['timeout_after', 'worker_crash'])
def test_m4_isolated_run_restart_and_reset(control, fault):
    seed = next(n for n in range(920001, 920100) if m4_data.generate(n)['fault'] == fault)
    item = control.create(small(seed=seed, purpose='validation'), backend=backend(control), m4=True)
    path, manifest = control.verify(item['run_id']); assert manifest['acceptance_stage'] == 'M4'
    result = run(control, item['run_id']); assert result['status'] == 'passed', result
    assert run(control, item['run_id']) == result
    before = (path/'reports/report.json').read_bytes()
    with pytest.raises(ValueError, match='fresh reset'): replay(control, item['run_id'])
    successor = control.reset(item['run_id'], item['fingerprint'])
    assert control.verify(successor['run_id'])[1]['acceptance_stage'] == 'M4'
    assert run(control, successor['run_id'])['status'] == 'passed'
    control.destroy(successor['run_id'], successor['fingerprint'])
    assert (path/'reports/report.json').read_bytes() == before
