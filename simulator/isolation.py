"""OS boundary for business processes. Unsupported hosts fail closed."""
import json
import os
from pathlib import Path
import platform
import selectors
import subprocess
import sys
import time
from sqlalchemy.engine import make_url
from .common import canonical

def quote(value):
    return json.dumps(value, ensure_ascii=False)


def sandbox_command(business_dir, database_url, command, serve_port=None):
    if platform.system() != 'Darwin' or not Path('/usr/bin/sandbox-exec').exists():
        raise RuntimeError('Managed simulator currently requires macOS sandbox-exec; no unrestricted fallback')
    business = Path(business_dir).resolve()
    root = Path(__file__).resolve().parents[1]
    # Only program/runtime reads and this run's business directory are allowed.
    # Parent control state, truth, reports, other runs and project .local remain
    # outside the allowlist. A symlink cannot escape sandbox resolved-path checks.
    read_paths = [root/'app', root/'simulator', Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                  Path(sys.executable).resolve().parent.parent]
    allowed_reads = ' '.join(f'(subpath {quote(str(p))})' for p in [*read_paths, business]) + f' (literal {quote(str(root))})'
    control_root = business.parents[2]
    rules = ['(version 1)', '(allow default)', '(deny network*)',
             f'(deny file-read-data (require-all (subpath {quote(str(control_root))}) (require-not (subpath {quote(str(business))}))))',
             f'(deny file-read-data (require-all (subpath {quote(str(Path.home()))}) (require-not (require-any {allowed_reads}))))',
             f'(deny file-write* (require-not (require-any (subpath {quote(str(business))}) (literal "/dev/null") (literal {quote(str(business.parent / "worker.log"))}))))']
    url = make_url(database_url)
    if serve_port is not None:
        if type(serve_port) is not int or not 1024<=serve_port<=65535:
            raise ValueError('Workbench port must be between 1024 and 65535')
        rules.append(f'(allow network-bind network-inbound (local ip "localhost:{serve_port}"))')
    if url.get_backend_name() == 'postgresql':
        host = url.query.get('host', url.host or '')
        if host.startswith('/'):
            # Grant only the server socket, never the server data directory.
            socket = str(Path(host)/('.s.PGSQL.'+str(url.query.get('port', url.port or 5432))))
            rules.append(f'(allow network-outbound (literal {quote(socket)}))')
            rules.append(f'(allow file-read* file-write* (literal {quote(socket)}))')
        elif host in ('127.0.0.1', 'localhost'):
            rules.append(f'(allow network-outbound (remote ip "localhost:{url.port or 5432}"))')
        else:
            raise ValueError('Simulation PostgreSQL must be local')
    return ['/usr/bin/sandbox-exec', '-p', '\n'.join(rules), *command]


class WorkerLost(RuntimeError):
    pass


class Worker:
    def __init__(self, run_dir, manifest, lock_fd=None, serve_port=None):
        self.directory = Path(run_dir)
        self.manifest = manifest
        self.process = None
        self.output = b''
        self.lock_fd = lock_fd
        self.serve_port = serve_port

    def start(self):
        from .common import read_json
        business = self.directory/'business'
        runtime = read_json(business/'runtime.json')
        command = [sys.executable, '-u', '-m', 'simulator.worker', str(business/'runtime.json')]
        if self.serve_port is not None: command += ['--serve', str(self.serve_port)]
        env = {'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
               'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'HOME': str(Path.home()),
               'TMPDIR': str(business/'tmp'), 'LANG': 'en_US.UTF-8'}
        # No inherited API keys, admin URLs, PGOPTIONS or application settings.
        self.log = open(self.directory/'worker.log', 'ab', buffering=0)
        self.process = subprocess.Popen(sandbox_command(business, runtime['database_url'], command, self.serve_port),
                    cwd=business, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self.log, start_new_session=True, umask=0o077,
                    pass_fds=(() if self.lock_fd is None else (self.lock_fd,)))
        self.output = b''
        ready = self.receive(30)
        if ready.get('ready') != self.manifest['database_fingerprint']:
            self.close()
            raise RuntimeError('Worker did not bind to the registered runtime')
        return self

    def receive(self, timeout=30):
        deadline = time.monotonic()+timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.output:
                remaining = deadline-time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.close()
                    raise WorkerLost('Worker timed out')
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    self.process.wait(timeout=5)
                    raise WorkerLost('Worker exited before acknowledging the attempt')
                self.output += data
        line, self.output = self.output.split(b'\n', 1)
        return json.loads(line)

    def call(self, operation, **payload):
        envelope = dict(operation=operation, run_id=self.manifest['run_id'],
                        fingerprint=self.manifest['database_fingerprint'], **payload)
        try:
            self.process.stdin.write((canonical(envelope)+'\n').encode()); self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise WorkerLost('Worker connection closed') from None
        return self.receive(.2 if payload.get('fault')=='timeout_after_success' else 30)

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout):
                if stream: stream.close()
            self.process = None
        if getattr(self, 'log', None): self.log.close()

    def __enter__(self):
        try: return self.start()
        except BaseException:
            self.close(); raise

    def __exit__(self, *args):
        self.close()
