"""Validated process configuration and an explicit, non-secret runtime identity.

Paths are anchored to this checkout, never to the caller's working directory.
The identity is a guard against accidental reuse, not a substitute for M2's
separate databases, restricted PostgreSQL roles and process isolation.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODES = frozenset({'development', 'test', 'simulation', 'production'})
RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


def _absolute_path(value, name):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{name} must be an absolute path')
    return path.resolve()


def _database_url(value):
    parsed = make_url(value)
    if parsed.get_backend_name() not in {'sqlite', 'postgresql'}:
        raise ValueError('Only SQLite and PostgreSQL databases are supported')
    if parsed.get_backend_name() == 'sqlite' and parsed.database not in (None, '', ':memory:'):
        target = Path(parsed.database).expanduser()
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        parsed = parsed.set(database=str(target.resolve()))
    return parsed.render_as_string(hide_password=False)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    runtime_mode: str
    run_id: str
    timezone: str
    model_key_path: Path
    virtual_time: str | None = None
    expected_fingerprint: str | None = None
    external_writes_enabled: bool = False
    allow_legacy_receipts: bool = False
    allow_legacy_sales: bool = False

    def fingerprint(self, connect_options=None):
        parsed = make_url(self.database_url)
        # Password rotation must not change the identity. Credentials are never
        # persisted or exposed through the runtime descriptor.
        identity = parsed.set(password=None).render_as_string(hide_password=False)
        return _hash({'version': 1, 'database': identity,
                      'connect_options': connect_options or {},
                      'data_dir': str(self.data_dir), 'mode': self.runtime_mode,
                      'run_id': self.run_id, 'timezone': self.timezone,
                      'clock_start': self.virtual_time,
                      'model_key_path': str(self.model_key_path),
                      'external_writes_enabled': self.external_writes_enabled})


def load_settings(environ=None, *, database_url=None, explicit_database=False, connect_options=None):
    env = os.environ if environ is None else environ
    default_dir = PROJECT_ROOT / '.local'
    explicit = explicit_database and database_url is not None
    resolved_url = _database_url(database_url or env.get('DATABASE_URL') or
                                 f'sqlite:///{default_dir / "inventory.db"}')
    if explicit:
        parsed = make_url(resolved_url)
        # Test fixtures supplied an engine explicitly long before runtime
        # configuration existed. They must never create/read the demo data dir.
        if parsed.get_backend_name() == 'sqlite' and parsed.database not in (None, '', ':memory:'):
            default_dir = Path(parsed.database).parent
        else:
            import tempfile
            safe_id = _hash({'database': parsed.set(password=None).render_as_string(),
                             'options': connect_options or {}})[:20]
            default_dir = Path(tempfile.gettempdir()) / 'inventory-explicit' / safe_id
    data_dir = _absolute_path(env.get('APP_DATA_DIR', str(default_dir)), 'APP_DATA_DIR')
    if database_url is None and not env.get('DATABASE_URL'):
        resolved_url = f'sqlite:///{data_dir / "inventory.db"}'
    mode = env.get('APP_RUNTIME_MODE', 'test' if explicit else 'development')
    if mode not in MODES:
        raise ValueError('APP_RUNTIME_MODE must be development, test, simulation or production')
    default_run = ('test-' + _hash({'url': make_url(resolved_url).set(password=None).render_as_string(),
                                  'options': connect_options or {}})[:16]) if explicit else 'development'
    run_id = env.get('APP_RUN_ID', default_run if mode in {'development', 'test'} else '')
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('APP_RUN_ID must be a non-empty safe identifier (maximum 80 characters)')
    timezone = env.get('APP_TIMEZONE', 'Asia/Shanghai')
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('APP_TIMEZONE must be an IANA timezone') from None
    key_input = Path(env.get('APP_MODEL_KEY_PATH', str(data_dir / 'deepseek-api-key'))).expanduser()
    if not key_input.is_absolute():
        raise ValueError('APP_MODEL_KEY_PATH must be an absolute path')
    # Keep the final component unresolved so api_key can reject symlinks.
    key_path = key_input.parent.resolve() / key_input.name
    virtual_time = env.get('APP_VIRTUAL_TIME') or None
    if virtual_time:
        if mode not in {'simulation', 'test'}:
            raise ValueError('Virtual business time is allowed only in simulation or test mode')
        try:
            parsed_time = datetime.fromisoformat(virtual_time.replace('Z', '+00:00'))
            if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise ValueError('APP_VIRTUAL_TIME must include an explicit UTC offset') from None
    expected = env.get('APP_RUNTIME_FINGERPRINT') or None
    if expected and not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('APP_RUNTIME_FINGERPRINT must be a SHA256 hex digest')
    external = env.get('APP_EXTERNAL_WRITES', 'false').lower()
    if external not in {'false', '0'}:
        raise ValueError('Real external writes are not available in this release')
    legacy = env.get('APP_ALLOW_LEGACY_RECEIPTS', 'false').lower()
    if legacy not in {'true', 'false', '1', '0'}:
        raise ValueError('APP_ALLOW_LEGACY_RECEIPTS must be true or false')
    if mode == 'production' and legacy in {'true', '1'}:
        raise ValueError('Legacy receipts without vouchers are forbidden in production mode')
    legacy_sales = env.get('APP_ALLOW_LEGACY_SALES', 'false').lower()
    if legacy_sales not in {'true', 'false', '1', '0'}:
        raise ValueError('APP_ALLOW_LEGACY_SALES must be true or false')
    if mode == 'production' and legacy_sales in {'true', '1'}:
        raise ValueError('Legacy sales without vouchers are forbidden in production mode')
    return Settings(data_dir, resolved_url, mode, run_id, timezone, key_path, virtual_time, expected,
                    allow_legacy_receipts=legacy in {'true', '1'},
                    allow_legacy_sales=legacy_sales in {'true', '1'})


def settings_for(engine):
    settings = getattr(engine, 'runtime_settings', None)
    if settings is None:
        raise RuntimeError('Engine is missing validated runtime settings; use db.make_engine')
    return settings


def runtime_descriptor(engine):
    settings = settings_for(engine)
    return {'runtime_mode': settings.runtime_mode, 'run_id': settings.run_id,
            'timezone': settings.timezone, 'fingerprint': engine.runtime_fingerprint,
            'simulated': settings.runtime_mode != 'production',
            'business_clock': 'virtual' if settings.virtual_time else 'system',
            'legacy_receipts_enabled': settings.allow_legacy_receipts,
            'legacy_sales_enabled': settings.allow_legacy_sales,
            'external_writes_enabled': False}


def bind_runtime(engine, role='api', expected_fingerprint=None, connection=None):
    """Bind API/worker to one persisted identity; mismatch fails closed.

    Called by migrations on startup and reusable by a future worker startup.
    Does not implement a worker, runtime provisioning or database permissions.
    """
    from sqlalchemy import select, insert
    from .events import runtime_bindings
    from . import db
    if role not in {'api', 'worker', 'migration', 'bootstrap', 'test'}:
        raise ValueError('Unknown runtime process role')
    settings = settings_for(engine)
    fingerprint = engine.runtime_fingerprint
    expected = expected_fingerprint or settings.expected_fingerprint
    if expected is not None and fingerprint != expected:
        raise RuntimeError('Runtime fingerprint does not match the expected environment')
    if connection is None:
        with db.write(engine, '__runtime_binding__') as c:
            return bind_runtime(engine, role, expected, c)
    row = connection.execute(select(runtime_bindings).where(runtime_bindings.c.id == 'runtime')).mappings().first()
    if row:
        if row['fingerprint'] != fingerprint or row['run_id'] != settings.run_id:
            raise RuntimeError('Database is bound to a different runtime; refusing cross-runtime reuse')
    else:
        connection.execute(insert(runtime_bindings).values(id='runtime', fingerprint=fingerprint,
                           run_id=settings.run_id, runtime_mode=settings.runtime_mode,
                           timezone=settings.timezone))
    return runtime_descriptor(engine)
