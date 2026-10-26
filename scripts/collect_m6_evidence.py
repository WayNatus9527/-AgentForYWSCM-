"""Collect retained M6 release evidence and archive exactly the current source."""
import argparse,hashlib,json,re,tarfile
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulator.common import code_version

REPO=Path(__file__).resolve().parents[1]


def read(path):return json.loads(path.read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def relative(path):return str(path.resolve().relative_to(REPO))
def evidence(path):return dict(evidence=relative(path),sha256=sha(path))


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    root=Path(args.root).resolve();output=Path(args.output).resolve();delivery=root/'delivery';delivery.mkdir(exist_ok=True)
    version=code_version();batches=[];manifests=[];inputs=[];seeds=[]
    for name in ('validation-sqlite-final','postgresql-final'):
        for path in sorted((root/name/'archives').glob('*/report.json')):
            report=read(path);stage=report.get('stage','M2');assert report['failed']==0
            count=report['passed'];assert count==(200 if stage=='M6' else 100)
            compact={k:v for k,v in report.items() if k not in ('cases','paired_experiments')}
            compact.update(stage=stage,independent_groups=report.get('independent_scenario_groups',count),**evidence(path))
            if stage=='M6':compact['paired_experiments']={k:v for k,v in report['paired_experiments'].items() if k!='cases'}
            batches.append(compact)
            for case in report['cases']:
                assert case['status']=='passed';manifest=read(root/name/'runs'/case['run_id']/'manifest.json')
                assert manifest['code_version']==version and manifest['purpose']=='validation'
                manifests.append(manifest);inputs.append(case['input_sha256']);seeds.append(case['seed'])
    assert len(batches)==10 and len(inputs)==1200 and len(set(inputs))==1200 and len(set(seeds))==1200
    assert sum(b['independent_groups'] for b in batches)==1000
    tests={}
    for backend,logname in [('sqlite','sqlite-final.log'),('postgresql','postgresql-final.log')]:
        path=root/logname;match=re.search(r'(\d+) passed, (\d+) skipped, (\d+) warning.*? in ([\d.]+)s',path.read_text())
        assert match,logname
        tests[backend]=dict(passed=int(match[1]),skipped=int(match[2]),warnings=int(match[3]),seconds=float(match[4]),**evidence(path))
    fixed_path=root/'fixed-final/archives/fixed-regression-summary.json';fixed=read(fixed_path);assert fixed['passed']==len(fixed['cases'])==9
    fixed_checks=sum(x.get('business',{}).get('checks',0) for x in fixed['cases'])
    capacities=[]
    for tier in ('small','medium','large'):
        paths=list((root/f'capacity-{tier}-final/runs').glob('*/reports/performance.json'));assert len(paths)==1
        report=read(paths[0]);assert report['passed'] and report['code_version']==version
        capacities.append(dict(report,**evidence(paths[0])))
    browser_path=root/'browser-final/report.json';browser=read(browser_path);assert browser['passed'] and not browser['pageErrors']
    layout_path=root/'browser-layout-final/layout-report.json';layout=read(layout_path);assert layout['passed']
    upgrade_path=root/'upgrade-final/report.json';upgrade=read(upgrade_path);assert upgrade['verified'] and upgrade['restore_verified']
    regressions=[]
    for path in (root/'retained-regression-final/archives').glob('m6-*/report.json'):
        d=read(path);assert d['failed']==0
        regressions.append(dict(passed=d['passed'],failed=d['failed'],seeds=[x['seed'] for x in d['cases']],**evidence(path)))
    assert sum(x['passed'] for x in regressions)==20
    early=[]
    for name,reason in [('probe-a','Outer sandbox blocked isolated workers'),('probe-b','Invalid source clock incorrectly appeared as open transport'),('probe-c','Follow-up development verification')]:
        for path in (root/name/'archives').glob('m6-*/report.json'):
            d=read(path);early.append(dict(name=name,reason=reason,passed=d['passed'],failed=d['failed'],
                failed_cases=[{k:x.get(k) for k in ('run_id','seed','error','failures')} for x in d['cases'] if x['status']!='passed'],**evidence(path)))
    paths=[]
    for name in ('app','simulator','scripts','tests','.github'):
        paths.extend(p for p in (REPO/name).rglob('*') if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo') and p.name!='.DS_Store')
    paths.extend(REPO.glob('requirements*.txt'));paths=sorted(set(paths))
    manifest_path=delivery/'source-files.json';manifest=dict(code_version=version,files=[dict(path=relative(p),sha256=sha(p),bytes=p.stat().st_size) for p in paths])
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');manifest_path.chmod(0o600)
    archive_path=delivery/'tested-source.tar.gz'
    with tarfile.open(archive_path,'w:gz') as tar:
        for path in paths:tar.add(path,arcname=relative(path),recursive=False)
    archive_path.chmod(0o600)
    with tarfile.open(archive_path,'r:gz') as tar:
        for item in manifest['files']:assert hashlib.sha256(tar.extractfile(item['path']).read()).hexdigest()==item['sha256']
    m6=[b for b in batches if b['stage']=='M6'];accuracy=Counter()
    for b in m6:
        for k,v in b['diagnostic_accuracy'].items():
            if not k.endswith('_rate'):accuracy[k]+=v
    accuracy=dict(accuracy);accuracy.update(false_positive_rate=accuracy['false_positives']/(accuracy['false_positives']+accuracy['true_negatives']),
        false_negative_rate=accuracy['false_negatives']/(accuracy['false_negatives']+accuracy['true_positives']),unknown_rate=accuracy['unknown_diagnoses']/accuracy['observed_diagnoses'])
    result=dict(product='跨境电商供应链管理Agent助手',version='0.10.0',stage='M6',schema_version=9,date='2026-10-25',generated_at=datetime.now(timezone.utc).isoformat(),
        synthetic_data_only=True,source_code_sha256=version,source_code_hash_scope='Paths and contents of app/**/*.py and simulator/**/*.py',
        source_archive=dict(path=relative(archive_path),sha256=sha(archive_path),files=len(paths),manifest=relative(manifest_path),manifest_sha256=sha(manifest_path),
            scope=['app','simulator','scripts','tests','.github','requirements*.txt']),tests=tests,
        skip_reasons=dict(sqlite=['PostgreSQL restricted-role privilege drift'],postgresql=['Two SQLite backup/restore tests']),
        warnings=['Existing Starlette TestClient httpx deprecation'],
        fixed_regression=dict(passed=9,checks=fixed_checks,**evidence(fixed_path)),
        independent_validation=dict(groups=1000,runs=1200,checks=sum(b['checks'] for b in batches),failed=0,unique_seeds=len(set(seeds)),unique_inputs=len(set(inputs)),
            code_versions=dict(Counter(m['code_version'] for m in manifests)),counting='M2-M5: one run per group; M6: baseline+improved pair counts once; tenants do not increase group count'),
        batches=batches,m6_diagnostics=accuracy,m6_paired_tenant_comparisons=sum(b['paired_experiments']['passed'] for b in m6),
        m6_actual_crash_recoveries=sum(b['coverage']['fault'].get('after_report_commit',0) for b in m6),
        capacity_host=read(root/'capacity-host.json'),capacity=capacities,browser=dict(browser,**evidence(browser_path)),mobile_layout=dict(layout,**evidence(layout_path)),
        upgrade={k:v for k,v in upgrade.items() if k not in ('before','after')},
        retained_regression=regressions,early_development=early,
        early_browser=dict(reason='Analyst navigation omitted review entry; fixed before final browser verification',**evidence(root/'browser-a/report.json')),
        development=dict(root=relative(root/'development-final'),run_id='dev-013eb0c441d28346',url='http://127.0.0.1:8025/',username='tenant-0-admin',password_location='business/login.json; never included in release artifacts'),
        limitations=['No real model calls, external connections or production deployment','Capacity: single-client SQLite confirmed/unallocated uniform orders only; no PostgreSQL capacity or mixed concurrent workload',
            'Background analysis has durable lease recovery but no persistent scheduler daemon','Cross-batch causal edges and incomplete historical stock/time remain unknown','Current calendar supports weekdays and one shift, not holiday master data',
            'M3-M6 use fresh reset runs; captured time-slice replay unavailable','Model rate limiting is process-local','GitHub CI has not run remotely'])
    result['upgrade'].update(evidence(upgrade_path))
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(output=str(output),archive_sha256=result['source_archive']['sha256'],source_files=len(paths),independent_groups=1000,runs=1200,capacity_tiers=3),ensure_ascii=False))


if __name__=='__main__':main()
