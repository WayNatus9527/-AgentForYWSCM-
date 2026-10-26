"""M6 migration 9 -> 10 on a copy; compare every original business column."""
import argparse,hashlib,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.backup import snapshot,restore
from app import db
from app.migrations import migrate


def digest(path,columns=None):
    with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as c:
        if columns is None:
            names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            columns={name:[x[1] for x in c.execute('PRAGMA table_info("'+name+'")')] for name in names if name not in ('schema_migrations','sessions','runtime_bindings')}
        values={}
        for name,fields in columns.items():
            query='SELECT '+','.join('"'+x+'"' for x in fields)+' FROM "'+name+'"'
            rows=c.execute(query).fetchall();values[name]=dict(count=len(rows),sha256=hashlib.sha256(json.dumps(sorted(rows,key=repr),ensure_ascii=False).encode()).hexdigest())
        return columns,values


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    pre=snapshot(args.source,root/'before');backup=root/'before'/pre['file']
    with sqlite3.connect(backup) as c:assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==9
    columns,before=digest(backup);upgraded=root/'upgraded.db';restore(backup,upgraded)
    engine=db.make_engine('sqlite:///'+str(upgraded));migrate(engine);migrate(engine);engine.dispose()
    assert digest(upgraded,columns)[1]==before
    all_columns,after=digest(upgraded);assert set(all_columns)-set(columns)=={'analysis_job_history'}
    assert set(all_columns['analysis_jobs'])-set(columns['analysis_jobs'])=={'attempt_limit','next_attempt_at','started_at','finished_at'}
    post=snapshot(upgraded,root/'after');restored=root/'restored.db';restore(root/'after'/post['file'],restored)
    engine=db.make_engine('sqlite:///'+str(restored));migrate(engine);engine.dispose()
    assert digest(restored)[1]==after
    with sqlite3.connect(restored) as c:versions=[x[0] for x in c.execute('SELECT version FROM schema_migrations ORDER BY version')]
    assert versions==list(range(1,11))
    result=dict(verified=True,source=str(Path(args.source).resolve()),preserved_tables=len(before),added_tables=1,added_job_columns=4,versions=versions,
        migration_idempotent=True,restore_verified=True,before=before,after=after,backup_sha256=pre['sha256'],upgraded_backup_sha256=post['sha256'])
    (root/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');(root/'report.json').chmod(0o600)
    print(json.dumps(dict(verified=True,preserved_tables=len(before),report=str(root/'report.json'))))


if __name__=='__main__':main()
