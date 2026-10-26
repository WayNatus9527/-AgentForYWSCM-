"""Business-only child. Receives public inputs, never truth or admin credentials."""
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from .common import canonical, digest, read_json, write_json


class Driver:
    def __init__(self, runtime):
        # Environment is assigned before importing app.main (which constructs
        # its default ASGI application at module load time).
        self.runtime = runtime
        self.directory = Path(runtime['data_dir'])
        env = dict(APP_DATA_DIR=str(self.directory), DATABASE_URL=runtime['database_url'],
                   APP_RUNTIME_MODE='simulation', APP_RUN_ID=runtime['run_id'],
                   APP_TIMEZONE=runtime['timezone'], APP_VIRTUAL_TIME=runtime['start_time'],
                   APP_RUNTIME_FINGERPRINT=runtime['fingerprint'], APP_EXTERNAL_WRITES='false')
        os.environ.update(env)
        from app import db
        from app.config import load_settings
        from app.migrations import migrate
        self.engine = db.make_engine(settings=load_settings(env))
        if runtime.get('schema'):
            with self.engine.connect() as c:
                actual = c.exec_driver_sql('SELECT current_user,current_schema(),current_database()').one()
                from sqlalchemy.engine import make_url
                u = make_url(runtime['database_url'])
                if tuple(actual) != (runtime['role'], runtime['schema'], u.database):
                    raise ValueError('Runtime database identity mismatch')
        migrate(self.engine)
        self.journal = sqlite3.connect(self.directory/'delivery.sqlite')
        self.journal.execute('PRAGMA journal_mode=WAL')
        self.journal.executescript('CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, event_hash TEXT NOT NULL, prepared TEXT NOT NULL, result TEXT); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
        previous = self.journal.execute("SELECT value FROM meta WHERE key='clock'").fetchone()
        if previous: self.advance(previous[0])

    def advance(self, target):
        from app.clock import parse_timestamp
        current = parse_timestamp(self.engine.business_clock.now())
        wanted = parse_timestamp(target)
        if wanted < current:
            raise ValueError('Business clock cannot move backwards')
        self.engine.business_clock.advance(seconds=(wanted-current).total_seconds())
        self.journal.execute("INSERT OR REPLACE INTO meta VALUES('clock',?)", (wanted.isoformat(),))
        self.journal.commit()
        return wanted.isoformat()

    def initialize(self, masters):
        from sqlalchemy import select, insert, func
        from app import db, inventory, catalog
        from app.security import hash_password
        credentials_path = self.directory/'login.json'
        if not credentials_path.exists():
            write_json(credentials_path, {'password': secrets.token_urlsafe(24)})
        credentials = read_json(credentials_path)
        hashed = hash_password(credentials['password'])
        actors = []
        with db.write(self.engine, '__sim_initialize__') as c:
            existing = c.scalar(select(func.count()).select_from(db.tenants))
            if existing and existing != len(masters):
                raise ValueError('Runtime contains unexpected master data')
            for master in masters:
                tenant = master['tenant_id']
                if existing:
                    actors.append(dict(c.execute(select(db.users).where(db.users.c.id==tenant+'-admin')).mappings().one()))
                    continue
                c.execute(insert(db.tenants).values(id=tenant, name=master['name']))
                actor = dict(id=tenant+'-admin', tenant_id=tenant, username=tenant+'-admin',
                             name='模拟管理员', role='admin', warehouse_ids=[], password_hash=hashed)
                c.execute(insert(db.users).values(**actor)); actors.append(actor)
                c.execute(insert(db.users).values(**(actor | dict(id=tenant+'-cashier', username=tenant+'-cashier',
                    role='cashier', warehouse_ids=[master['warehouses'][0]['id']]))))
                for warehouse in master['warehouses']:
                    c.execute(insert(db.warehouses).values(**warehouse, tenant_id=tenant,
                              country='CN', timezone=self.runtime['timezone'], authority='local'))
                for item in master['skus']:
                    c.execute(insert(db.skus).values(id=item['id'], tenant_id=tenant, code=item['code'],
                              name=item['name'], spec='模拟基础规格', unit='件', barcode=''))
                    balance = inventory.load_balance(c, actor, master['warehouses'][0]['id'], item['id'])
                    inventory.movement(c, actor, balance, {'g': item['opening']}, 'opening',
                                       '模拟可信期初', '', 0, occurred_at=self.runtime['start_time'])
        for master, actor in zip(masters, actors):
            # Chunk the existing 1000-SKU supplier contract for large generation.
            for offset in range(0, len(master['skus']), 1000):
                supplier = master['supplier']
                catalog.save_supplier(self.engine, actor, dict(code=supplier['code']+'-'+str(offset//1000),
                    name=supplier['name'], default_lead_days=supplier['lead_days'],
                    sku_ids=[s['id'] for s in master['skus'][offset:offset+1000]]), 'initialize-supplier-'+str(offset))
        stored = self.journal.execute("SELECT value FROM meta WHERE key='masters'").fetchone()
        if stored and stored[0] != digest(masters):
            raise ValueError('Master input changed during a run')
        self.journal.execute("INSERT OR REPLACE INTO meta VALUES('masters',?)", (digest(masters),)); self.journal.commit()
        return {'initialized': True}

    def actor(self, tenant):
        from sqlalchemy import select
        from app import db
        with self.engine.connect() as c:
            return dict(c.execute(select(db.users).where(db.users.c.id==tenant+'-admin')).mappings().one())

    def result(self, identity):
        result = self.journal.execute('SELECT result FROM actions WHERE id=?', (identity,)).fetchone()
        return json.loads(result[0]) if result and result[0] else None

    def prepare(self, event):
        from sqlalchemy import select
        from app import db, workflows as w, operations as op
        v = event['values']; kind = event['kind']; actor = self.actor(event['tenant_id'])
        parent = self.result(v['parent'])['response'] if v.get('parent') else None
        warehouse = v.get('destination_id', event['warehouse_id']) if kind=='transfer_receive' else v.get('warehouse_id', event['warehouse_id'])
        with self.engine.connect() as c:
            version = c.scalar(select(db.balances.c.version).where(db.balances.c.tenant_id==actor['tenant_id'], db.balances.c.warehouse_id==warehouse, db.balances.c.sku_id==event['sku_id'])) or 0
            body = dict(warehouse_id=warehouse, sku_id=event['sku_id'], expected_version=version)
            if kind=='purchase':
                body = dict(warehouse_id=warehouse, code=v['code'], supplier=v['supplier_code'], note='模拟采购',
                            lines=[dict(sku_id=event['sku_id'], quantity=v['quantity'])])
            elif kind in ('sale', 'foreign_receipt'):
                body |= dict(source='模拟业务契约', source_account=event['source_account'], reference=v['reference'], note='', occurred_at=event['occurred_at'])
                body |= {key:v[key] for key in (('quantity',) if kind=='sale' else ('g','q','d'))}
            elif kind=='purchase_receipt':
                ov = c.scalar(select(w.orders.c.version).where(w.orders.c.id==parent['id']))
                body = dict(reference=v['reference'], expected_version=ov,
                            lines=[dict(sku_id=event['sku_id'], expected_version=version, **{f:v[f] for f in ('g','q','d')})])
            elif kind=='quality':
                body |= dict(good=v['good'], bad=v['bad'], reference=v['reference'], reason='模拟入库质检')
            elif kind=='return':
                body = dict(kind='customer', original_id=parent['document_id'], quantity=v['quantity'],
                            reference=v['reference'], expected_version=version, reason='模拟客户退货')
            elif kind=='return_quality':
                rv = c.scalar(select(op.returns.c.version).where(op.returns.c.id==parent['id']))
                body = dict(return_id=parent['id'], return_version=rv, good=v['good'], bad=v['bad'],
                            reference=v['reference'], expected_version=version, reason='模拟退货质检')
            elif kind=='transfer':
                body = dict(code=v['code'], warehouse_id=event['warehouse_id'], destination_id=v['destination_id'],
                            reason='模拟补仓调拨', lines=[dict(sku_id=event['sku_id'], quantity=v['quantity'])])
            elif kind.startswith('transfer_'):
                tv = c.scalar(select(op.transfers.c.version).where(op.transfers.c.id==parent['id']))
                line = dict(sku_id=event['sku_id'], expected_version=version)
                body = dict(expected_version=tv, lines=[line])
                if kind=='transfer_receive':
                    line |= {f:v[f] for f in ('g','q','d')}; body['reference']=v['reference']
                else: body['reason']='模拟调拨执行'
            else: raise ValueError('Unsupported public business action')
        return dict(body=body, parent_id=parent.get('id') if parent else None)

    def deliver(self, event, fault=None):
        from app import inventory, workflows as w, operations as op, db, events as evidence
        from app.domain import BusinessError
        from sqlalchemy import event as hooks
        identity = event['id']; kind = event['kind']; actor = self.actor(event['tenant_id'])
        cached = self.journal.execute('SELECT event_hash,prepared FROM actions WHERE id=?', (identity,)).fetchone()
        if cached and cached[0] != digest(event):
            return dict(status='rejected', error='SOURCE_EVENT_CONFLICT')
        for dep in event['dependencies']:
            result = self.result(dep)
            if not result or result['status']!='ok':
                return dict(status='blocked', error='DEPENDENCY_NOT_READY')
        prepared = json.loads(cached[1]) if cached else self.prepare(event)
        if not cached:
            self.journal.execute('INSERT INTO actions VALUES(?,?,?,NULL)', (identity, digest(event), canonical(prepared))); self.journal.commit()
        body = prepared['body']; parent = prepared['parent_id']; key = 'simulation-'+event['business_action_id']
        def crash_before_commit(conn):
            os._exit(86)
        if fault=='crash_before_commit': hooks.listen(self.engine, 'commit', crash_before_commit, once=True)
        try:
            if kind=='purchase': response=w.purchase_create(self.engine, actor, body, key)
            elif kind=='purchase_receipt': response=w.purchase_receive(self.engine, actor, parent, body, key)
            elif kind=='sale': response=inventory.sell(self.engine, actor, body, key)
            elif kind=='foreign_receipt': response=inventory.receive(self.engine, actor, body, key)
            elif kind in ('quality','return_quality'): response=op.inspect_quality(self.engine, actor, body, key)
            elif kind=='return': response=op.return_create(self.engine, actor, body, key)
            elif kind=='transfer': response=op.transfer_create(self.engine, actor, body, key)
            elif kind.startswith('transfer_'): response=op.transfer_action(self.engine, actor, parent, kind.removeprefix('transfer_'), body, key)
            else: raise ValueError('Unsupported action')
            if fault=='crash_after_commit': os._exit(87)
            if fault=='timeout_after_success':
                # Business transaction is committed. Hold the response longer
                # than the transport's real monotonic deadline.
                __import__('time').sleep(2)
            with db.write(self.engine, actor['tenant_id']) as c:
                evidence.record_event(c, actor, event_type='simulation.'+kind, object_type='simulation_action',
                    object_id=event['business_action_id'], lifecycle='completed',
                    process_id=event['process_id'], object_line_id=event['sku_id'],
                    warehouse_id=event['warehouse_id'], sku_id=event['sku_id'],
                    planned_at=event['planned_at'], occurred_at=event['occurred_at'],
                    received_at=self.engine.business_clock.now(), source_system='simulated_connector',
                    source_account=event['source_account'], source_event_id=identity,
                    source_version=str(event['source_version']), dependencies=event['dependencies'],
                    resource_group=event['resource'], time_quality='reported',
                    rule_version='sim-contract-1', calendar_version='calendar-1',
                    evidence_refs=[response.get('document_id') or response.get('id') or identity],
                    payload={'action':kind,'values':event['values']})
            result = dict(status='ok', response=response)
        except BusinessError as exc:
            result = dict(status='rejected', error=exc.code, http_status=exc.status)
        self.journal.execute('UPDATE actions SET result=? WHERE id=?', (canonical(result), identity)); self.journal.commit()
        return result

    def snapshot(self, permissions=True):
        from sqlalchemy import select, func
        from app import db, operations, events
        from app.domain import FIELDS
        with self.engine.connect() as c:
            balances = [dict(r) for r in c.execute(select(db.balances.c.tenant_id,db.balances.c.warehouse_id,db.balances.c.sku_id,
                *[db.balances.c[f] for f in FIELDS]).order_by(db.balances.c.tenant_id,db.balances.c.warehouse_id,db.balances.c.sku_id)).mappings()]
            counts = {name:c.scalar(select(func.count()).select_from(db.metadata.tables[name])) for name in
                      ('documents','ledger','outbox','business_events','purchase_orders','transfers')}
            transit = c.scalar(select(func.coalesce(func.sum(operations.transfer_lines.c.sent-operations.transfer_lines.c.received-operations.transfer_lines.c.lost),0)))
            rows = [dict(r) for r in c.execute(select(db.ledger)).mappings()]
            docs = set(c.execute(select(db.documents.c.id)).scalars())
            consistent = len(docs)==len(rows) and all(r['document_id'] in docs and all(
                r['before'][f]+r['delta'].get(f,0)==r['after'][f] for f in FIELDS) for r in rows)
        return dict(balances=balances, counts=counts, in_transit=transit, ledger_consistent=consistent,
                    permission_checks=self.permission_checks() if permissions else {},
                    clock=self.engine.business_clock.now())

    def permission_checks(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        password=read_json(self.directory/'login.json')['password']
        checks={}
        with TestClient(create_app(self.engine)) as client:
            response=client.post('/api/v1/session',json={'username':'tenant-0-cashier','password':password})
            checks['login']=response.status_code==200
            client.headers['X-CSRF-Token']=response.json()['csrf']
            checks['foreign_warehouse_read']=client.get('/api/v1/inventory?warehouse_id=tenant-1-wh-0').status_code==404
            checks['foreign_catalog_hidden']='tenant-1' not in client.get('/api/v1/bootstrap').text
            checks['business_account_has_no_control_routes']=client.post('/api/simulation/reset',json={}).status_code==404
            checks['source_truth_not_served']=client.get('/private/truth.json').status_code==404
            checks['cannot_choose_database']=client.post('/api/v2/inventory/receipts',json={'database_url':'other-run'}).status_code==422
        return checks

    def probe(self, path=None, schema=None, expected_role=None):
        result={}
        if path:
            target=Path(path)
            for op in ('read','write'):
                try:
                    if op=='read': target.read_bytes()
                    else:
                        with open(target,'ab') as stream: stream.write(b'forbidden')
                except PermissionError: result[op]='denied'
                except FileNotFoundError: result[op]='missing'
                else: result[op]='allowed'
        if schema:
            from psycopg import sql
            from .postgres import connection
            with connection(self.runtime['database_url']) as c:
                for op,stmt in [('read',sql.SQL('SELECT * FROM {}.tenants').format(sql.Identifier(schema))),
                                ('write',sql.SQL('INSERT INTO {}.tenants VALUES (\'forbidden\',\'forbidden\')').format(sql.Identifier(schema)))]:
                    try: c.execute(stmt)
                    except __import__('psycopg').errors.InsufficientPrivilege: result[op]='denied'
                    else: result[op]='allowed'
                if expected_role:
                    try: c.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(expected_role)))
                    except __import__('psycopg').errors.InsufficientPrivilege: result['role_switch']='denied'
                    else: result['role_switch']='allowed'
        return result

    def close(self):
        self.journal.close(); self.engine.dispose()


def main():
    runtime=read_json(sys.argv[1]); driver=Driver(runtime)
    print(canonical({'ready':driver.engine.runtime_fingerprint}),flush=True)
    if len(sys.argv)>2 and sys.argv[2]=='--serve':
        import uvicorn
        from app.main import create_app
        try: uvicorn.run(create_app(driver.engine,review_worker_enabled=True),host='127.0.0.1',port=int(sys.argv[3]),log_level='warning')
        finally: driver.close()
        return
    try:
        for line in sys.stdin:
            request=json.loads(line)
            if request.get('run_id')!=runtime['run_id'] or request.get('fingerprint')!=runtime['fingerprint']:
                response=dict(status='rejected',error='RUNTIME_MISMATCH')
            else:
                op=request['operation']
                try:
                    if op=='initialize': response=driver.initialize(request['masters'])
                    elif op=='review_queue_fixture':
                        from .review_queue_workload import fixture
                        response=fixture(driver,request['master'])
                    elif op=='review_queue_crash':
                        from .review_queue_workload import crash
                        response=crash(driver,request['identity'])
                    elif op=='review_queue_recover':
                        from .review_queue_workload import recover
                        response=recover(driver,request['master'],request['identities'])
                    elif op=='advance': response={'clock':driver.advance(request['target'])}
                    elif op=='deliver': response=driver.deliver(request['event'],request.get('fault'))
                    elif op=='m3':
                        from .m3_workload import execute
                        response=execute(driver,request['masters'],request['case'])
                    elif op=='m4':
                        from .m4_workload import execute
                        response=execute(driver,request['masters'],request['case'])
                    elif op=='m5':
                        from .m5_workload import execute
                        response=execute(driver,request['masters'],request['case'])
                    elif op=='m6_capacity_populate':
                        from .m6_performance import populate
                        response=populate(driver,request['master'],request['offset'],request['count'])
                    elif op=='m6_capacity_measure':
                        from .m6_performance import measure
                        response=measure(driver,request['master'],request['orders'])
                    elif op=='m6':
                        from .m6_workload import execute
                        response=execute(driver,request['masters'],request['case'])
                    elif op=='m6_report_crash':
                        from .m6_workload import first_job
                        first_job(driver,request['tenant_id']);os._exit(96)
                    elif op=='m6_recover':
                        from .m6_workload import recover
                        response=recover(driver,request['masters'],request['case'])
                    elif op=='m5_confirmation_crash':
                        from .m5_workload import confirm_first
                        confirm_first(driver,request['tenant_id']);os._exit(95)
                    elif op=='m5_recover':
                        from .m5_workload import recover
                        response=recover(driver,request['masters'],request['case'])
                    elif op=='m4_recover':
                        from .m4_workload import recover
                        response=recover(driver,request['masters'],request['case'])
                    elif op=='m4_delivery_crash':
                        from app import delivery
                        delivery.tick(driver.engine,driver.actor(request['tenant_id']),lease_seconds=1,after_external=lambda _:os._exit(94))
                        raise RuntimeError('Expected an external operation before crash')
                    elif op=='snapshot': response=driver.snapshot(request.get('permissions',True))
                    elif op=='probe': response=driver.probe(request.get('path'),request.get('schema'),request.get('expected_role'))
                    else: response=dict(error='UNKNOWN_OPERATION')
                except Exception as exc:
                    # Runtime URLs/credentials must not escape SQL exceptions.
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                    response=dict(status='failed',error=type(exc).__name__)
            print(canonical(response),flush=True)
    finally: driver.close()


if __name__=='__main__': main()
