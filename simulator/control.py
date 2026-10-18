"""Local control plane: identities and lifecycle only; business details stay per-run."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tarfile
from .common import canonical, digest, file_digest, read_json, write_json, reject_links, code_version
from .config import Scenario
from .generator import generate
from .oracle import expected

RUN_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')


class BusyRun(ValueError):
    pass


class Control:
    def __init__(self, root):
        root = Path(root).expanduser()
        if not root.is_absolute(): raise ValueError('Control root must be an absolute dedicated directory')
        reject_links(root)
        self.root = root
        marker = root/'control.json'
        if not marker.is_file(): raise ValueError('Unknown simulation control root; initialize explicitly')
        self.identity = read_json(marker)
        if self.identity.get('mode') != 'synthetic_only' or self.identity.get('root') != str(root):
            raise ValueError('Simulation control root marker or path mismatch')
        self.registry = root/'registry.sqlite'

    @classmethod
    def initialize(cls, root, pg_url_file=None):
        root=Path(root).expanduser()
        if not root.is_absolute(): raise ValueError('Control root must be absolute')
        reject_links(root)
        if root.exists() and any(root.iterdir()):
            existing=cls(root)
            if bool(pg_url_file) != bool(existing.identity['postgres']):
                raise ValueError('Existing control backend differs')
            return existing
        root.mkdir(parents=True, exist_ok=True, mode=0o700); root.chmod(0o700)
        identity=dict(control_id=secrets.token_hex(16), root=str(root), mode='synthetic_only', version=1, postgres=bool(pg_url_file))
        if pg_url_file:
            source=Path(pg_url_file)
            reject_links(source)
            if source.stat().st_mode & 0o077: raise ValueError('PostgreSQL credential file must have mode 0600')
            from .postgres import Postgres
            url=source.read_text().strip()
            Postgres(url,identity['control_id']).initialize()
            write_json(root/'postgres-secret.json', {'url':url})
        for name in ('runs','archives','regressions'): (root/name).mkdir(mode=0o700)
        with sqlite3.connect(root/'registry.sqlite') as c:
            c.executescript('''CREATE TABLE runs(run_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, seed INTEGER NOT NULL,
                scenario TEXT NOT NULL, backend TEXT NOT NULL, manifest_hash TEXT NOT NULL, status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, parent_id TEXT);
                CREATE TABLE audit(id INTEGER PRIMARY KEY, run_id TEXT, action TEXT, detail TEXT, at TEXT);
                CREATE TABLE batches(id TEXT PRIMARY KEY, purpose TEXT, config TEXT);''')
        write_json(root/'control.json',identity,immutable=True)
        return cls(root)

    def pg(self):
        if not self.identity['postgres']: raise ValueError('PostgreSQL control is not configured')
        from .postgres import Postgres
        return Postgres(read_json(self.root/'postgres-secret.json')['url'], self.identity['control_id'])

    @contextmanager
    def lock(self, run_id=None):
        if run_id and not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run ID')
        path=self.root/('run-'+run_id+'.lock' if run_id else 'control.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise BusyRun('Run/control has an active operation') from None
            yield fd
        finally:
            # Child processes inherit this open-file lock, so controller death
            # cannot make a still-running child eligible for deletion.
            os.close(fd)

    @contextmanager
    def connect(self):
        reject_links(self.registry)
        c=sqlite3.connect(self.registry,timeout=10); c.row_factory=sqlite3.Row
        try:
            with c: yield c
        finally: c.close()

    def audit(self, run_id, action, detail=''):
        with self.connect() as c:
            c.execute('INSERT INTO audit(run_id,action,detail,at) VALUES(?,?,?,?)',
                      (run_id,action,detail,datetime.now(timezone.utc).isoformat()))

    def update(self, run_id, status, progress=0):
        with self.connect() as c:
            c.execute('UPDATE runs SET status=?,progress=? WHERE run_id=?',(status,progress,run_id))

    def list(self):
        with self.connect() as c: return [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY created_at DESC')]

    def registered(self, run_id):
        if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run ID')
        with self.connect() as c: row=c.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if not row: raise ValueError('Run is not registered in this control plane')
        return dict(row)

    def verify(self, run_id, fingerprint=None, *, check_code=True):
        row=self.registered(run_id)
        if row['status']=='destroyed': raise ValueError('Run has already been destroyed')
        path=self.root/'runs'/run_id
        reject_links(path)
        if path.parent!=self.root/'runs': raise ValueError('Run path boundary mismatch')
        manifest=read_json(path/'manifest.json')
        if digest(manifest)!=row['manifest_hash'] or manifest['run_id']!=run_id or manifest['control_id']!=self.identity['control_id']:
            raise ValueError('Immutable manifest identity mismatch')
        if manifest['mode']!='simulation' or manifest['output_dir']!=str(path):
            raise ValueError('Not a registered simulation directory')
        if fingerprint and fingerprint!=manifest['database_fingerprint']:
            raise ValueError('Target runtime fingerprint mismatch')
        for filename, wanted in manifest['input_hashes'].items():
            reject_links(path/filename)
            if file_digest(path/filename)!=wanted: raise ValueError('Immutable input changed: '+filename)
        runtime=read_json(path/'business/runtime.json')
        from app.config import load_settings
        env={'APP_DATA_DIR':str(path/'business'),'APP_RUNTIME_MODE':'simulation','APP_RUN_ID':run_id,
             'APP_TIMEZONE':manifest['timezone'],'APP_VIRTUAL_TIME':manifest['config']['start_time']}
        settings=load_settings(env,database_url=runtime['database_url'])
        if settings.fingerprint()!=manifest['database_fingerprint'] or runtime['fingerprint']!=settings.fingerprint():
            raise ValueError('Runtime path/credentials identity changed')
        if check_code and manifest['code_version']!=code_version():
            raise ValueError('Code changed since generation; create a new run and retain this evidence')
        if row['backend']=='postgresql': self.pg().verify(run_id,runtime['database_url'])
        return path,manifest

    def create(self, config, *, run_id=None, backend='sqlite', parent_id=None, replay_schedule=None, m3=False):
        config=Scenario.model_validate(config) if isinstance(config,dict) else config
        if backend not in ('sqlite','postgresql'): raise ValueError('Unsupported backend')
        run_id=run_id or f'{config.purpose[:3]}-{secrets.token_hex(8)}'
        if not RUN_ID.fullmatch(run_id): raise ValueError('Invalid run ID')
        with self.lock():
            with self.connect() as c:
                if c.execute('SELECT 1 FROM runs WHERE run_id=?',(run_id,)).fetchone(): raise ValueError('Run ID already exists')
                if config.purpose=='validation' and not parent_id and c.execute('SELECT 1 FROM runs WHERE seed=?',(config.seed,)).fetchone():
                    raise ValueError('Validation seeds must be fresh, not reused from development or earlier verification')
            dataset,truth=generate(config); truth['expected']=expected(dataset)
            if m3:
                from .m3_data import generate as m3_generate, expected as m3_expected
                dataset['m3']=m3_generate(config.seed)
                truth['m3_expected']=m3_expected(dataset['masters'],dataset['m3'])
            path=self.root/'runs'/run_id; path.mkdir(mode=0o700)
            for directory in ('business','private','reports','business/tmp','business/attachments','business/backups'):
                (path/directory).mkdir(mode=0o700)
            backend_config={'database_url':'sqlite:///'+str(path/'business/inventory.db')}
            if backend=='postgresql': backend_config=self.pg().provision(run_id)
            from app.config import load_settings
            env={'APP_DATA_DIR':str(path/'business'),'APP_RUNTIME_MODE':'simulation','APP_RUN_ID':run_id,
                 'APP_TIMEZONE':config.timezone,'APP_VIRTUAL_TIME':config.start_time}
            settings=load_settings(env,database_url=backend_config['database_url'])
            runtime=dict(**backend_config, data_dir=str(path/'business'),run_id=run_id,
                         timezone=config.timezone,start_time=config.start_time,fingerprint=settings.fingerprint())
            write_json(path/'business/runtime.json',runtime,immutable=True)
            write_json(path/'inputs.json',dataset,immutable=True)
            write_json(path/'private/truth.json',truth,immutable=True)
            hashes={name:file_digest(path/name) for name in ('inputs.json','private/truth.json','business/runtime.json')}
            if replay_schedule is not None:
                write_json(path/'replay.json',replay_schedule,immutable=True); hashes['replay.json']=file_digest(path/'replay.json')
            manifest=dict(format_version=1, generator_version=config.generator_version,config=config.model_dump(),
                scenario_version=config.scenario_version, run_id=run_id, purpose=config.purpose, seed=config.seed,
                control_id=self.identity['control_id'], mode='simulation', timezone=config.timezone,
                code_version=code_version(), config_sha256=digest(config.model_dump()),
                normalized_events_sha256=digest(dataset['events']), input_sha256=digest(dataset), input_hashes=hashes,
                database_fingerprint=settings.fingerprint(), backend=backend,output_dir=str(path),
                created_at=datetime.now(timezone.utc).isoformat(),parent_run_id=parent_id,
                nondeterministic_fields=['run_id','database_fingerprint','created_at','business UUIDs','real timestamps','performance'])
            if m3:manifest['acceptance_stage']='M3'
            write_json(path/'manifest.json',manifest,immutable=True)
            with self.connect() as c:
                c.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,0,?,?)',
                    (run_id,config.purpose,config.seed,config.scenario,backend,digest(manifest),'ready',manifest['created_at'],parent_id))
            self.audit(run_id,'created',digest(manifest))
        return self.registered(run_id)|{'fingerprint':settings.fingerprint()}

    def reset(self, run_id, fingerprint):
        with self.lock(run_id):
            path,manifest=self.verify(run_id,fingerprint,check_code=False)
            self.archive(run_id,path,manifest)
            # New identity means old workers/tasks cannot target a newly empty DB.
            created=self.create(manifest['config'],backend=manifest['backend'],parent_id=run_id,m3=manifest.get('acceptance_stage')=='M3')
            self.audit(run_id,'reset_to_successor',created['run_id'])
            return created

    def archive(self, run_id, path, manifest):
        # Preserve input/trace/report evidence, without credentials or live DBs.
        for item in path.rglob('*'):
            if item.is_symlink(): raise ValueError('Symlink inside run; refusing evidence archive')
        archive=self.root/'archives'/f'{run_id}-{secrets.token_hex(4)}.tar.gz'
        with tarfile.open(archive,'w:gz') as bundle:
            for name in ('manifest.json','inputs.json','private/truth.json','trace.jsonl','schedule.json','reports'):
                target=path/name
                if target.exists(): bundle.add(target,arcname=name,recursive=True)
        archive.chmod(0o600)
        self.audit(run_id,'evidence_archived',archive.name+':'+file_digest(archive))
        return archive

    def destroy(self, run_id, fingerprint):
        with self.lock(run_id):
            path,manifest=self.verify(run_id,fingerprint,check_code=False)
            # Reject every link, including unexpected user-created attachments.
            for item in path.rglob('*'):
                if item.is_symlink(): raise ValueError('Symlink inside run; refusing recursive cleanup')
            archive=self.archive(run_id,path,manifest)
            if manifest['backend']=='postgresql': self.pg().destroy(run_id)
            shutil.rmtree(path)
            self.update(run_id,'destroyed'); self.audit(run_id,'destroyed',archive.name)
            return dict(run_id=run_id,status='destroyed',evidence_archive=archive.name)

    def report(self, run_id):
        path,_=self.verify(run_id,check_code=False)
        file=path/'reports/report.json'
        return read_json(file) if file.exists() else {'status':self.registered(run_id)['status']}

    def promote(self, run_id):
        path,manifest=self.verify(run_id,check_code=False)
        report=self.report(run_id)
        if report.get('status')!='failed': raise ValueError('Only a failed validation can become a regression case')
        target=self.root/'regressions'/(run_id+'.json')
        write_json(target,dict(config=manifest['config']|{'purpose':'regression'},source_run=run_id,
                    input_sha256=manifest['input_sha256'],acceptance_stage=manifest.get('acceptance_stage','M2'),report=report),immutable=True)
        self.audit(run_id,'promoted_regression',target.name)
        return str(target)
