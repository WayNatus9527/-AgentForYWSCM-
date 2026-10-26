"""Collect v0.10.1 evidence without replacing the historical M6 release archive."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from simulator.common import code_version


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path):
    return str(path.resolve().relative_to(REPO))


def evidence(path):
    return dict(evidence=relative(path), sha256=sha(path))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root, output = Path(args.root).resolve(), Path(args.output).resolve()
    version = code_version()
    assert version == '59bbbf2810af72f6f22ae7b89ae74bb859bc2b8e502f67698b81113274b069e0'
    batches, manifests, inputs, seeds = [], [], [], []
    for name in ('validation-sqlite-final', 'postgresql-final'):
        for path in sorted((root / name / 'archives').glob('*/report.json')):
            report = read(path)
            stage = report.get('stage', 'M2')
            assert report['failed'] == 0
            assert report['passed'] == (200 if stage == 'M6' else 10)
            compact = {k: v for k, v in report.items() if k not in ('cases', 'paired_experiments', 'report_path')}
            compact.update(stage=stage, independent_groups=report.get('independent_scenario_groups', report['passed']), **evidence(path))
            if stage == 'M6':
                compact['paired_experiments'] = {k: v for k, v in report['paired_experiments'].items() if k != 'cases'}
            batches.append(compact)
            for case in report['cases']:
                assert case['status'] == 'passed'
                manifest = read(root / name / 'runs' / case['run_id'] / 'manifest.json')
                assert manifest['code_version'] == version and manifest['purpose'] == 'validation'
                manifests.append(manifest)
                inputs.append(case['input_sha256'])
                seeds.append(case['seed'])
    assert len(batches) == 6 and len(manifests) == len(set(inputs)) == len(set(seeds)) == 440
    assert sum(b['independent_groups'] for b in batches) == 240
    tests = {}
    for backend, expected in [('sqlite', (387, 1)), ('postgresql', (386, 2))]:
        path = root / (backend + '-final.log')
        match = re.search(r'(\d+) passed, (\d+) skipped, (\d+) warning.*? in ([\d.]+)s', path.read_text())
        assert match and (int(match[1]), int(match[2])) == expected
        tests[backend] = dict(passed=int(match[1]), skipped=int(match[2]), warnings=int(match[3]), seconds=float(match[4]), **evidence(path))
    fixed_path = root / 'fixed-final/archives/fixed-regression-summary.json'
    fixed = read(fixed_path)
    assert fixed['passed'] == len(fixed['cases']) == 9
    for case in fixed['cases']:
        assert case['status'] == 'passed'
        assert read(root / 'fixed-final/runs' / case['run_id'] / 'manifest.json')['code_version'] == version
    browsers = {}
    for name in ('m6', 'queue'):
        path = root / f'browser-{name}-verified/report.json'
        report = read(path)
        assert report['passed'] and not report['pageErrors']
        browsers[name] = dict(report, **evidence(path))
    assert browsers['queue']['asset_sha256'] == sha(REPO / 'app/static/m6.js')
    layout_path = root / 'browser-layout-verified/report.json'
    layout = read(layout_path)
    assert layout['passed'] and layout['actionsReachable']
    fixture = read(root / 'development-verified/queue-browser-fixture.json')
    assert fixture['run_id'] == browsers['queue']['run_id']
    assert read(root / 'development-verified/runs' / fixture['run_id'] / 'manifest.json')['code_version'] == version
    upgrade_path = root / 'upgrade-final/report.json'
    upgrade = read(upgrade_path)
    assert upgrade['verified'] and upgrade['restore_verified'] and upgrade['migration_idempotent']
    assert (upgrade['preserved_tables'], upgrade['added_tables'], upgrade['added_job_columns']) == (87, 1, 4)
    accuracy = Counter()
    m6 = [b for b in batches if b['stage'] == 'M6']
    for batch in m6:
        assert len(batch['coverage']['kind']) == 10
        for key, value in batch['diagnostic_accuracy'].items():
            if not key.endswith('_rate'):
                accuracy[key] += value
    accuracy = dict(accuracy)
    accuracy.update(false_positive_rate=accuracy['false_positives'] / (accuracy['false_positives'] + accuracy['true_negatives']),
                    false_negative_rate=accuracy['false_negatives'] / (accuracy['false_negatives'] + accuracy['true_positives']),
                    unknown_rate=accuracy['unknown_diagnoses'] / accuracy['observed_diagnoses'])
    paths = []
    for name in ('app', 'simulator', 'scripts', 'tests', '.github'):
        paths.extend(p for p in (REPO / name).rglob('*') if p.is_file() and not p.is_symlink()
                     and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo') and p.name != '.DS_Store')
    paths = sorted(set(paths + list(REPO.glob('requirements*.txt'))))
    delivery = root / 'delivery'
    delivery.mkdir(exist_ok=True)
    manifest_path, archive_path = delivery / 'source-files.json', delivery / 'tested-source.tar.gz'
    source_files = dict(code_version=version, files=[dict(path=relative(p), sha256=sha(p), bytes=p.stat().st_size) for p in paths])
    manifest_path.write_text(json.dumps(source_files, ensure_ascii=False, indent=2) + '\n')
    manifest_path.chmod(0o600)
    with tarfile.open(archive_path, 'w:gz') as archive:
        for path in paths:
            archive.add(path, arcname=relative(path), recursive=False)
    archive_path.chmod(0o600)
    with tarfile.open(archive_path, 'r:gz') as archive:
        assert len(archive.getmembers()) == len(paths)
        for item in source_files['files']:
            assert hashlib.sha256(archive.extractfile(item['path']).read()).hexdigest() == item['sha256']
    result = dict(
        product='跨境电商供应链管理Agent助手', version='0.10.1', stage='M6.1', schema_version=10, date='2026-10-26',
        generated_at=datetime.now(timezone.utc).isoformat(), synthetic_data_only=True,
        source_code_sha256=version, source_code_hash_scope='Paths and contents of app/**/*.py and simulator/**/*.py',
        source_archive=dict(path=relative(archive_path), sha256=sha(archive_path), files=len(paths), manifest=relative(manifest_path),
                            manifest_sha256=sha(manifest_path), scope=['app', 'simulator', 'scripts', 'tests', '.github', 'requirements*.txt']),
        tests=tests, skip_reasons=dict(sqlite=['PostgreSQL restricted-role privilege drift'], postgresql=['Two SQLite backup/restore tests']),
        warnings=['Existing Starlette TestClient httpx deprecation'],
        fixed_regression=dict(passed=9, checks=sum(x['business']['checks'] for x in fixed['cases']), **evidence(fixed_path)),
        independent_validation=dict(groups=240, runs=440, checks=sum(b['checks'] for b in batches), failed=0,
                                    unique_seeds=len(set(seeds)), unique_inputs=len(set(inputs)),
                                    code_versions=dict(Counter(m['code_version'] for m in manifests)),
                                    counting='200 M6 baseline/improvement pairs + 40 PostgreSQL M2-M5 runs; tenants do not increase group count'),
        batches=batches, m6_diagnostics=accuracy,
        m6_paired_tenant_comparisons=sum(b['paired_experiments']['passed'] for b in m6),
        m6_actual_after_commit_crash_recoveries=sum(b['coverage']['fault'].get('after_report_commit', 0) for b in m6),
        queue_regression=dict(unit_tests=10, included_in_both_full_suites=True,
                              real_before_commit_process_exit=dict(exit_code=97, backends=['sqlite', 'postgresql'],
                                  test='tests/test_m6_simulator.py::test_real_process_death_before_publish_then_continuous_recovery'),
                              **evidence(root / 'queue-final-targeted.log')),
        browser=browsers, layout=dict(layout, **evidence(layout_path)),
        upgrade=dict({k: v for k, v in upgrade.items() if k not in ('before', 'after')}, **evidence(upgrade_path)),
        early_verification=[dict(reason='Test fixture inserted response-only replayed field into a database row; fixed fixture', **evidence(root / 'queue-a.log')),
                            dict(reason='Browser fixture assumed a warehouse account existed; final script creates it explicitly', **evidence(root / 'browser-queue-final/report.json'))],
        development=dict(root=relative(root / 'development-verified'), run_id=fixture['run_id'], url='http://127.0.0.1:8026/',
                         username='tenant-0-admin', password_location='business/login.json; excluded from release artifacts'),
        historical_capacity=dict(version='0.10.0', evidence='docs/M6_验收摘要.json', rerun_for_this_version=False),
        limitations=['No real model calls, external connections or production deployment',
                     'In-process recovery worker and optional CLI loop; no OS automatic restart service configured',
                     'Worker status is local process health, not a distributed heartbeat or tenant queue metric',
                     'Long report transactions can contend with same-tenant writes; no new capacity or concurrency claim',
                     'Cross-batch causality, incomplete historical time/stock and calendar holidays retain M6 limitations',
                     'M3-M6 captured time-slice replay and remote GitHub CI remain unverified'])
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    index = delivery / 'evidence-index.json'
    index.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    index.chmod(0o600)
    print(json.dumps(dict(output=str(output), source_files=len(paths), archive_sha256=sha(archive_path),
                          groups=240, runs=440, checks=result['independent_validation']['checks']), ensure_ascii=False))


if __name__ == '__main__':
    main()
