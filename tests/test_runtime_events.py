from dataclasses import replace
from pathlib import Path
import shutil
import time
import pytest
from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import DBAPIError
from app import db, events
from app.clock import VirtualBusinessClock, business_now, security_timestamp
from app.config import load_settings, bind_runtime, runtime_descriptor
from app.migrations import migrate
from app.domain import BusinessError
from app.backup import snapshot, restore
from test_inventory import env, actor, balance


def test_default_paths_are_independent_of_current_directory(tmp_path, monkeypatch):
    before=load_settings({})
    monkeypatch.chdir(tmp_path)
    after=load_settings({})
    assert before.database_url==after.database_url and before.model_key_path==after.model_key_path
    explicit=load_settings({'APP_DATA_DIR':str(tmp_path/'run-A')})
    assert explicit.database_url=='sqlite:///'+str(tmp_path/'run-A/inventory.db')
    assert explicit.model_key_path==tmp_path/'run-A/deepseek-api-key'


@pytest.mark.parametrize('change', [
    {'APP_DATA_DIR':'.local'}, {'APP_RUN_ID':'../other'}, {'APP_TIMEZONE':'not-a-zone'},
    {'APP_RUNTIME_MODE':'unknown'}, {'APP_RUNTIME_MODE':'production','APP_RUN_ID':'prod','APP_VIRTUAL_TIME':'2026-01-01T00:00:00Z'},
    {'APP_RUNTIME_MODE':'simulation'}, {'APP_EXTERNAL_WRITES':'true'}, {'APP_RUNTIME_FINGERPRINT':'oops'},
])
def test_invalid_environment_fails_closed(change):
    with pytest.raises(ValueError):load_settings(change)


def test_explicit_engines_have_independent_paths_clocks_and_identity(tmp_path):
    one=db.make_engine('sqlite:///'+str(tmp_path/'a/database.db'))
    two=db.make_engine('sqlite:///'+str(tmp_path/'b/database.db'))
    try:
        assert one.runtime_settings.data_dir==tmp_path/'a'
        assert two.runtime_settings.data_dir==tmp_path/'b'
        assert one.runtime_fingerprint!=two.runtime_fingerprint
        assert one.pool is not two.pool and one.business_clock is not two.business_clock
        migrate(one);migrate(two)
        with pytest.raises(RuntimeError):bind_runtime(two,'worker',one.runtime_fingerprint)
        assert bind_runtime(one,'worker',one.runtime_fingerprint)['run_id']==one.runtime_settings.run_id
    finally:one.dispose();two.dispose()


def test_changed_run_identity_on_same_database_is_rejected(tmp_path):
    settings=load_settings({'APP_DATA_DIR':str(tmp_path),'APP_RUN_ID':'run-A'})
    one=db.make_engine(settings=settings);migrate(one);one.dispose()
    wrong=db.make_engine(settings=replace(settings,run_id='run-B'))
    try:
        with pytest.raises(RuntimeError,match='different runtime'):migrate(wrong)
    finally:wrong.dispose()


def test_runtime_descriptor_never_exposes_database_or_key_path(env):
    engine,client=env
    descriptor=client.get('/api/v2/runtime').json()
    assert descriptor['stage']=='M1' and descriptor['simulated_data_only']
    assert 'database_url' not in descriptor and 'model_key_path' not in descriptor
    assert str(engine.runtime_settings.data_dir) not in str(descriptor)


def test_business_time_advance_does_not_expire_security_session(env):
    engine,client=env;engine.business_clock=VirtualBusinessClock('2026-01-01T00:00:00Z')
    stamp=security_timestamp();engine.business_clock.advance(seconds=100*365*86400)
    assert business_now(engine).startswith('2125')
    assert abs(security_timestamp()-stamp)<3
    assert client.get('/api/v1/session').status_code==200
    with pytest.raises(ValueError):engine.business_clock.advance(seconds=-1)


def record(c, who, **changes):
    args=dict(event_type='quality.started',object_type='return',object_id='RT-1',warehouse_id='wh-cn',
              sku_id='sku-1',source_system='mock',source_account='QC-A',source_event_id='EV-1',
              lifecycle='started',quantity=3,unit='件')
    return events.record_event(c,who,**(args|changes))


def test_event_source_retries_corrections_and_unknown_time(env):
    engine,_=env;who=actor(engine)
    with db.write(engine,who['tenant_id']) as c:
        event_id=record(c,who)
        assert record(c,who)==event_id
    with engine.connect() as c:
        row=c.execute(select(events.business_events).where(events.business_events.c.id==event_id)).mappings().one()
        assert row['occurred_at'] is None and row['time_quality']=='unknown'
    with pytest.raises(BusinessError) as conflict:
        with db.write(engine,who['tenant_id']) as c:record(c,who,quantity=4)
    assert conflict.value.code=='EVENT_CONTENT_CONFLICT'
    with db.write(engine,who['tenant_id']) as c:
        correction=record(c,who,source_event_id='EV-CORRECTION',lifecycle='corrected',correction_of=event_id,
                          occurred_at='2026-10-14T10:00:00+08:00',reason_code='timestamp_evidence_received')
    with engine.connect() as c:
        assert c.scalar(select(events.business_events.c.occurred_at).where(events.business_events.c.id==event_id)) is None
        assert c.scalar(select(events.business_events.c.correction_of).where(events.business_events.c.id==correction))==event_id


def test_event_negative_time_is_flagged_and_not_silently_clamped(env):
    engine,_=env;who=actor(engine)
    with db.write(engine,who['tenant_id']) as c:
        identity=record(c,who,occurred_at='2026-10-14T02:00:00Z',received_at='2026-10-14T01:00:00Z',registered_at='2026-10-14T01:00:00Z')
    with engine.connect() as c:
        row=c.execute(select(events.business_events).where(events.business_events.c.id==identity)).mappings().one()
        assert 'occurred_after_received' in row['quality_flags']
        assert row['occurred_at']=='2026-10-14T02:00:00+00:00'


@pytest.mark.parametrize('change', [dict(occurred_at='2026-01-01'),dict(quantity=True),dict(quantity=-1),
                                  dict(time_quality='verified'),dict(lifecycle='corrected'),dict(source_timezone='bad')])
def test_event_validation_prevents_invalid_evidence(env,change):
    engine,_=env
    with pytest.raises(BusinessError):
        with db.write(engine,'demo-yiwu') as c:record(c,actor(engine),**change)


def test_events_are_append_only_and_warehouse_scoped(env):
    engine,client=env;who=actor(engine)
    with db.write(engine,who['tenant_id']) as c:identity=record(c,who)
    with pytest.raises(DBAPIError):
        with db.write(engine,who['tenant_id']) as c:
            c.execute(update(events.business_events).where(events.business_events.c.id==identity).values(quantity=99))
    with pytest.raises(DBAPIError):
        with db.write(engine,who['tenant_id']) as c:c.execute(delete(events.business_events))
    with pytest.raises(BusinessError) as scope:
        with db.write(engine,who['tenant_id']) as c:record(c,actor(engine,'warehouse'),warehouse_id='wh-us')
    assert scope.value.status==404
    reply=client.post('/api/v1/session',json={'username':'warehouse','password':'test-password'})
    client.headers['X-CSRF-Token']=reply.json()['csrf']
    assert client.get('/api/v2/events?warehouse_id=wh-us').status_code==404


def test_verified_restore_can_rebind_but_unverified_copy_cannot(env,tmp_path):
    engine,_=env
    if engine.dialect.name!='sqlite':pytest.skip('SQLite restore; PostgreSQL has a separate restore drill')
    who=actor(engine)
    with db.write(engine,who['tenant_id']) as c:record(c,who)
    report=snapshot(engine.url.database,tmp_path/'backups')
    source=tmp_path/'backups'/report['file']
    unverified=tmp_path/'unverified.db';shutil.copy2(source,unverified)
    wrong=db.make_engine('sqlite:///'+str(unverified))
    try:
        with pytest.raises(RuntimeError,match='different runtime'):migrate(wrong)
    finally:wrong.dispose()
    target=tmp_path/'restored.db';result=restore(source,target)
    assert result['runtime_rebind_required']
    restored=db.make_engine('sqlite:///'+str(target))
    try:
        migrate(restored)
        with restored.connect() as c:
            assert c.scalar(select(func.count()).select_from(events.business_events))==1
            assert c.scalar(select(func.count()).select_from(db.sessions))==0
        assert restored.runtime_fingerprint!=engine.runtime_fingerprint
    finally:restored.dispose()


def test_model_configuration_is_bound_to_request_engine(env,tmp_path,monkeypatch):
    engine,client=env
    from app import agent_model
    seen=[]
    def read_key(*,settings=None):
        seen.append(settings)
        return ''
    monkeypatch.setattr(agent_model,'api_key',read_key)
    monkeypatch.chdir(tmp_path)
    assert client.get('/api/v1/agent/status').status_code==200
    assert seen==[engine.runtime_settings]
