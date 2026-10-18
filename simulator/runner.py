"""Durable deterministic scheduler and fault transport around isolated workers."""
from datetime import datetime, timedelta, timezone
import heapq
import os
import platform
import time
from .common import append_json, canonical, digest, file_digest, read_json, read_lines, write_json
from .generator import validate_dataset
from .isolation import Worker, WorkerLost
from .oracle import compare


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat()


def initial_schedule(dataset):
    events=dataset['events']
    candidates=[e['id'] for e in events if e['kind'] in ('purchase_receipt','quality','sale','return','transfer_reserve','transfer_ship','transfer_receive')
                and e['values'].get('quantity',0)<1_000_000]
    policies={identity:fault for identity,fault in zip(candidates,dataset['config']['faults'])}
    if len(policies)<len(dataset['config']['faults']): raise ValueError('Not enough actions to exercise all configured faults')
    schedule=[]
    for index,event in enumerate(events):
        fault=policies.get(event['id'])
        at=timestamp(event['deliver_at'])
        if fault=='reorder':
            by_id={e['id']:e for e in events}
            at=min([timestamp(by_id[d]['deliver_at']) for d in event['dependencies']]+[at])-60
            at=max(timestamp(dataset['config']['start_time']),at)
        schedule.append(dict(ticket=event['id']+':1',event_id=event['id'],attempt=1,at=at,
                             fault=fault,order=index,duplicate_remaining=1 if fault=='duplicate' else 0))
    return schedule


def trace_records(path):
    records=read_lines(path)
    previous=''
    for number,row in enumerate(records):
        contents={k:v for k,v in row.items() if k!='hash'}
        if row['seq']!=number or row['previous_hash']!=previous or digest(contents)!=row['hash']:
            raise ValueError('Delivery trace hash chain was changed or truncated')
        previous=row['hash']
    return records


def signature(outcome):
    return {k:outcome[k] for k in ('status','error') if k in outcome}


def run(control, run_id, *, until=None):
    _, dispatch_manifest=control.verify(run_id)
    if dispatch_manifest.get('acceptance_stage')=='M3':
        if until is not None:raise ValueError('M3 acceptance runs complete workflows; time slicing is available for M2 scenarios')
        from .m3_validation import run as m3_run
        return m3_run(control,run_id)
    started=time.monotonic()
    with control.lock(run_id) as lock_fd:
        path,manifest=control.verify(run_id)
        dataset=read_json(path/'inputs.json')
        trace_path=path/'trace.jsonl'
        records=trace_records(trace_path)
        replay=read_json(path/'replay.json') if (path/'replay.json').exists() else None
        if not (path/'schedule.json').exists():
            write_json(path/'schedule.json',initial_schedule(dataset),immutable=True)
        initial=read_json(path/'schedule.json')
        if initial!=initial_schedule(dataset): raise ValueError('Initial delivery schedule changed')
        pending={item['ticket']:item for item in initial}
        outcomes={}; durations=[]; intent_tickets=set(); done_replay=0
        for row in records:
            if row['phase']=='intent': intent_tickets.add(row['ticket'])
            if row['phase']=='outcome':
                pending.pop(row['ticket'],None)
                pending.update({item['ticket']:item for item in row['next']})
                if row['result']['status'] in ('ok','rejected'): outcomes[row['event_id']]=row['result']
                durations.append(row['elapsed_ms']); done_replay+=1
        counter=max([item['order'] for item in pending.values()]+[len(initial)])+1
        queue=[(x['at'],x['order'],x['ticket']) for x in pending.values()]
        heapq.heapify(queue)
        by_id={e['id']:e for e in dataset['events']}
        end_time=timestamp(until) if until else float('inf')
        control.update(run_id,'running',len(outcomes)); control.audit(run_id,'resumed' if records else 'started')
        worker=None
        def record(value):
            value=dict(value,seq=len(records),previous_hash=records[-1]['hash'] if records else '')
            value['hash']=digest(value); append_json(trace_path,value); records.append(value)
        try:
            worker=Worker(path,manifest,lock_fd).start()
            initialized=worker.call('initialize',masters=dataset['masters'])
            if not initialized.get('initialized'): raise RuntimeError('Business initialization failed')
            status=worker.call('snapshot',permissions=False)
            current=timestamp(status['clock'])
            if end_time<current: raise ValueError('Fast-forward target precedes the saved business clock')
            replay_remaining=replay['attempts'][done_replay:] if replay else None
            while replay_remaining if replay else pending:
                if replay:
                    step=replay_remaining.pop(0); item=step['item']
                else:
                    while queue and queue[0][2] not in pending: heapq.heappop(queue)
                    item=pending[queue[0][2]]
                    if item['at']>end_time: break
                    heapq.heappop(queue)
                event=by_id[item['event_id']]
                current=max(current,item['at'])
                advanced=worker.call('advance',target=iso(current))
                if 'clock' not in advanced: raise RuntimeError('Business clock rejected scheduled event')
                fault=item['fault']
                # The previous controller may have died after dispatch. Retry
                # its identical business action, without re-injecting the crash.
                if item['ticket'] in intent_tickets and not replay: fault=None
                record(dict(phase='intent',ticket=item['ticket'],event_id=event['id'],item=item,
                            at=iso(current),fault=fault,business_action_id=event['business_action_id']))
                begin=time.monotonic()
                if fault in ('rate_limit','disconnect','loss_redelivery','delay'):
                    outcome=dict(status='transport_failure',error={
                        'rate_limit':'RATE_LIMITED','disconnect':'DISCONNECTED','loss_redelivery':'LOST_BEFORE_DELIVERY','delay':'DELAYED'}[fault])
                else:
                    try: outcome=worker.call('deliver',event=event,fault=fault)
                    except WorkerLost:
                        outcome=dict(status='lost',error='UNACKNOWLEDGED_RESULT')
                        worker.close(); worker=Worker(path,manifest,lock_fd).start()
                elapsed=round((time.monotonic()-begin)*1000,3); durations.append(elapsed)
                successors=[]
                if not replay:
                    retry=outcome['status'] in ('blocked','lost','transport_failure')
                    duplicate=outcome['status'] in ('ok','rejected') and item['duplicate_remaining']>0
                    if retry or duplicate:
                        if item['attempt']>=16: raise RuntimeError('Retry limit reached for '+event['id'])
                        at=current+(21600 if fault in ('delay','loss_redelivery') else 1)
                        if outcome['status']=='blocked':
                            dep_times=[x['at'] for x in pending.values() if x['event_id'] in event['dependencies']]
                            at=max([at]+[x+1 for x in dep_times])
                        number=item['attempt']+1
                        successors=[dict(item,ticket=event['id']+':'+str(number),attempt=number,at=at,
                                         order=counter,fault=None,duplicate_remaining=0 if duplicate else item['duplicate_remaining'])]
                        counter+=1
                if replay and signature(outcome)!=step['expected']:
                    raise AssertionError('Replay outcome differs at '+event['id'])
                record(dict(phase='outcome',ticket=item['ticket'],event_id=event['id'],result=outcome,
                            next=successors,elapsed_ms=elapsed))
                if not replay:
                    pending.pop(item['ticket'],None); pending.update({x['ticket']:x for x in successors})
                    for successor in successors: heapq.heappush(queue,(successor['at'],successor['order'],successor['ticket']))
                if outcome['status'] in ('ok','rejected'): outcomes[event['id']]=outcome
                if outcome['status']=='failed': raise RuntimeError('Business worker failed at '+event['id']+': '+outcome['error'])
                control.update(run_id,'running',len(outcomes))
            paused=not replay and bool(pending)
            if until and end_time!=float('inf'):
                worker.call('advance',target=iso(max(current,end_time)))
            actual=worker.call('snapshot',permissions=not paused)
            if 'balances' not in actual: raise RuntimeError('Business snapshot failed')
            assertions=compare(dataset,actual,outcomes) if not paused else {'passed':None,'checks':0,'failures':[]}
            truth_probe=worker.call('probe',path=str(path/'private/truth.json'))
            control_probe=worker.call('probe',path=str(control.root/'registry.sqlite'))
            isolated=truth_probe=={'read':'denied','write':'denied'} and control_probe=={'read':'denied','write':'denied'}
            status='paused' if paused else ('passed' if assertions['passed'] and isolated else 'failed')
            ordered=sorted(durations)
            report=dict(stage='M2',version='0.6.0',run_id=run_id,purpose=manifest['purpose'],seed=manifest['seed'],
                scenario=manifest['config']['scenario'],backend=manifest['backend'],status=status,
                manifest_sha256=digest(manifest),input_sha256=manifest['input_sha256'],
                normalized_events_sha256=manifest['normalized_events_sha256'],trace_sha256=file_digest(trace_path),
                generation=validate_dataset(dataset),business=assertions,
                isolation={'passed':isolated,'private_truth':truth_probe,'control_registry':control_probe,
                           'runtime_fingerprint':manifest['database_fingerprint'],'process_boundary':'macOS sandbox-exec'},
                progress={'completed_events':len(outcomes),'total_events':len(by_id),'business_time':actual['clock']},
                performance={'measurement_clock':'real_monotonic','attempts':len(durations),
                             'p95_attempt_ms':ordered[min(len(ordered)-1,int(len(ordered)*.95))] if ordered else None,
                             'elapsed_seconds':round(time.monotonic()-started,3),
                             'host':platform.platform(),'cpu_count':os.cpu_count(),
                             'scope':'generated scale only; includes controlled retries and process restart'},
                bottleneck_comparison={'status':'not_implemented_until_M6','truth_available':True},
                known_limits=['M3 exception tasks, M4 Inbox/order lifecycle and M6 diagnosis remain future milestones',
                              'Fault transport is deterministic simulation; no real external service calls'],
                failures=assertions['failures'])
            write_json(path/'reports/snapshot.json',actual)
            write_json(path/'reports/report.json',report)
            control.update(run_id,status,len(outcomes)); control.audit(run_id,status,report['trace_sha256'])
            return report
        except BaseException as exc:
            report=dict(stage='M2',run_id=run_id,purpose=manifest['purpose'],seed=manifest['seed'],status='failed',
                        error=type(exc).__name__+': '+str(exc),
                        last_event=records[-1].get('event_id') if records else None,
                        manifest_sha256=digest(manifest),trace_sha256=file_digest(trace_path) if trace_path.exists() else None)
            write_json(path/'reports/report.json',report); control.update(run_id,'failed',len(outcomes)); control.audit(run_id,'failed',type(exc).__name__)
            raise
        finally:
            if worker: worker.close()


def replay(control, source_id):
    with control.lock(source_id):
        path,manifest=control.verify(source_id)
        if manifest.get('acceptance_stage')=='M3':
            raise ValueError('M3 acceptance uses fresh reset successors; captured delivery replay is available for M2 scenarios')
        records=trace_records(path/'trace.jsonl')
        intents={}; attempts=[]
        for row in records:
            if row['phase']=='intent': intents[row['ticket']]=row
            elif row['phase']=='outcome':
                intent=intents.pop(row['ticket'])
                item=intent['item'] | {'at':timestamp(intent['at']), 'fault':intent['fault']}
                attempts.append(dict(item=item,expected=signature(row['result'])))
        if intents: raise ValueError('Source has an unacknowledged attempt; resume it before replay')
        if control.registered(source_id)['status'] not in ('passed','failed'):
            raise ValueError('Replay requires a completed source trace')
        created=control.create(manifest['config'],backend=manifest['backend'],parent_id=source_id,
                               replay_schedule=dict(source_run_id=source_id,source_trace_sha256=file_digest(path/'trace.jsonl'),attempts=attempts))
    return run(control,created['run_id'])
