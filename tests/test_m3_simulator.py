from pathlib import Path
import pytest
from simulator import m3_data
from simulator.common import read_json,digest
from simulator.runner import run,replay
from test_m2_simulator import control,backend,small,isolated


def test_m3_independent_hand_calculated_gold_and_seed_variation():
    masters=[dict(tenant_id='A',warehouses=[{'id':'A0'},{'id':'A1'}],skus=[{'id':'s1','opening':100},{'id':'s2','opening':50}])]
    case=dict(conflict=True,count_delta=-1,quality_good=3,quality_bad=2,sale=4,return_good=1)
    target=m3_data.expected(masters,case)
    assert [(r['warehouse_id'],r['sku_id'],r['g'],r['q'],r['d'],r['online']) for r in target]==[('A0','s1',112,0,0,98),('A0','s2',50,0,0,0),('A1','s1',1,0,0,0)]
    assert m3_data.generate(5)==m3_data.generate(5)
    assert digest(m3_data.generate(5))!=digest(m3_data.generate(6))
    source=Path(m3_data.__file__).read_text();assert 'from app' not in source and 'import app' not in source


@isolated
def test_m3_manifest_reset_worker_isolation_and_rerun(control):
    config=small(seed=911001,purpose='validation');item=control.create(config,backend=backend(control),m3=True)
    path,manifest=control.verify(item['run_id']);assert manifest['acceptance_stage']=='M3' and 'm3' in read_json(path/'inputs.json')
    result=run(control,item['run_id']);assert result['status']=='passed',result
    same=run(control,item['run_id']);assert same==result
    before=(path/'reports/report.json').read_bytes()
    with pytest.raises(ValueError,match='fresh reset'):replay(control,item['run_id'])
    successor=control.reset(item['run_id'],item['fingerprint'])
    new_path,new_manifest=control.verify(successor['run_id']);assert new_manifest['acceptance_stage']=='M3'
    assert new_manifest['database_fingerprint']!=manifest['database_fingerprint']
    assert run(control,successor['run_id'])['status']=='passed'
    assert (path/'reports/report.json').read_bytes()==before
    control.destroy(successor['run_id'],successor['fingerprint']);assert (path/'reports/report.json').read_bytes()==before
