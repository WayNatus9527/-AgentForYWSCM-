"""Small persistence primitives shared by the simulation control plane."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value, *, immutable=False):
    path = Path(path)
    data = (canonical(value) + '\n').encode()
    if immutable:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
    else:
        fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def append_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(canonical(value) + '\n'); f.flush(); os.fsync(f.fileno())


def read_lines(path):
    path = Path(path)
    if not path.exists():
        return []
    # A partial final line is evidence of an interrupted write, never silently
    # discard it and continue a possibly different delivery sequence.
    return [json.loads(line) for line in path.read_text().splitlines()]


def reject_links(path):
    path = Path(path)
    for item in [path, *path.parents]:
        if item.is_symlink():
            raise ValueError('Symlink paths are not permitted for simulation storage')


def code_version():
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for folder in ('app', 'simulator'):
        for path in sorted((root / folder).rglob('*.py')):
            h.update(str(path.relative_to(root)).encode()); h.update(path.read_bytes())
    return h.hexdigest()
