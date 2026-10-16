from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from sqlalchemy import select
from app import db
from app.config import load_settings
from app.migrations import migrate
from validation.m1_scenarios import run_scenario


def test_ten_fresh_datasets_with_identical_business_ids_remain_independent(tmp_path):
    seeds=[151,173,197,211,239,251,281,307,337,359]
    with ThreadPoolExecutor(2) as pool:
        reports=list(pool.map(lambda seed:run_scenario(seed,tmp_path/str(seed)),seeds))
    assert all(r['status']=='passed' for r in reports)
    assert len({r['database_fingerprint'] for r in reports})==10
    assert len({r['input_sha256'] for r in reports})==10
    with __import__('pytest').raises(FileExistsError):run_scenario(seeds[0],tmp_path/str(seeds[0]))
    # Creating additional input/output never changes already completed run B.
    b=tmp_path/str(seeds[1])
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in b.iterdir() if p.is_file()}
    run_scenario(383,tmp_path/'another')
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in b.iterdir() if p.is_file()}


def test_same_seed_generates_same_inputs_and_quantity_result_in_fresh_spaces(tmp_path):
    a=run_scenario(401,tmp_path/'a');b=run_scenario(401,tmp_path/'b')
    assert a['input_sha256']==b['input_sha256'] and a['stock_digest']==b['stock_digest']
    assert a['database_fingerprint']!=b['database_fingerprint']


def test_regression_full_quota_reserved_and_cancellation_without_restore(tmp_path):
    # Discovered by the first independent 100-seed run. Exhausted quota is a
    # legitimate business state; the generator must test its expected rejection.
    assert run_scenario(634405215,tmp_path/'zero-free-quota')['status']=='passed'
