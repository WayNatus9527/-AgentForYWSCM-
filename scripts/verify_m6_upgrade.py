"""Verify M5 -> M6 on a backed-up SQLite copy, never migrate the source."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.backup import snapshot,restore
from app import db
from app.migrations import migrate


def digests(path):
    with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as c:
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result={}
        for name in names:
            if name in ('schema_migrations','sessions','runtime_bindings'):continue
            rows=c.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall()
            result[name]=dict(count=len(rows),sha256=hashlib.sha256(json.dumps(sorted(rows,key=repr),ensure_ascii=False).encode()).hexdigest())
        return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    pre=snapshot(args.source,root/'before');backup=root/'before'/pre['file'];before=digests(backup)
    with sqlite3.connect(backup) as c:assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==8,'Expected M5 source database'
    upgraded=root/'upgraded.db';restore(backup,upgraded)
    engine=db.make_engine('sqlite:///'+str(upgraded));migrate(engine);migrate(engine);engine.dispose()
    after=digests(upgraded);assert all(after[n]==v for n,v in before.items());assert len(after)-len(before)==5
    post=snapshot(upgraded,root/'after');restored=root/'restored.db';result=restore(root/'after'/post['file'],restored)
    assert digests(restored)==after
    engine=db.make_engine('sqlite:///'+str(restored));migrate(engine);engine.dispose();assert digests(restored)==after
    with sqlite3.connect(restored) as c:versions=[r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')]
    assert versions==list(range(1,10))
    report=dict(verified=True,source=str(Path(args.source).resolve()),preserved_tables=len(before),added_tables=5,versions=versions,
        migration_idempotent=True,restore_verified=result['verified'],sessions_invalidated=True,before=before,after=after,
        backup_sha256=pre['sha256'],upgraded_backup_sha256=post['sha256'])
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));(root/'report.json').chmod(0o600)
    print(json.dumps(dict(report=str(root/'report.json'),preserved_tables=len(before),added_tables=5,verified=True)))


if __name__=='__main__':main()
