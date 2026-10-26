"""Pure deterministic durations, explicit dependency intervals and transparent ranking."""
from collections import defaultdict
from datetime import datetime,timedelta,time
from math import ceil
from statistics import median
from zoneinfo import ZoneInfo
from .clock import parse_timestamp as ts
from .inventory import fingerprint

RULE='process-review-v1'
WAIT={'quality_wait','restock_wait','pick_wait','review_wait','approval_wait'}
OPERATIONS=WAIT|{'quality_work','pick_work','review_work','transit'}


def working_hours(start,end,calendar,zone):
    a,b=ts(start),ts(end)
    if b<a or (b-a).days>3660:return None
    tz=ZoneInfo(zone);day=a.astimezone(tz).date();last=b.astimezone(tz).date();seconds=0
    while day<=last:
        if day.weekday() in calendar['weekdays']:
            left=datetime.combine(day,time(calendar['start_hour']),tzinfo=tz)
            right=datetime.combine(day+timedelta(days=1),time(),tzinfo=tz) if calendar['end_hour']==24 else datetime.combine(day,time(calendar['end_hour']),tzinfo=tz)
            seconds+=max(0,(min(b,right.astimezone(a.tzinfo))-max(a,left.astimezone(a.tzinfo))).total_seconds())
        day+=timedelta(days=1)
    return round(seconds/3600,6)


def union_hours(intervals):
    intervals=sorted((ts(a),ts(b)) for a,b in intervals if a and b and ts(b)>ts(a));merged=[]
    for a,b in intervals:
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    return round(sum((b-a).total_seconds() for a,b in merged)/3600,6)


def summarize(rows,minimum):
    valid=[r['hours'] for r in rows if r['completed'] and r['hours'] is not None];valid.sort()
    known_due=[r for r in rows if r['completed'] and r['late'] is not None]
    return dict(samples=len(rows),completed_samples=sum(r['completed'] for r in rows),valid_completed=len(valid),
        unknown_samples=sum(r['hours'] is None for r in rows),coverage=round(sum(r['hours'] is not None for r in rows)/len(rows),4) if rows else None,
        median_hours=median(valid) if valid else None,p90_hours=valid[ceil(.9*len(valid))-1] if valid else None,p95_hours=valid[ceil(.95*len(valid))-1] if valid else None,
        unstable=len(valid)<minimum,open_quantity=sum(r['quantity'] for r in rows if not r['completed'] and r.get('quantity_known',True)),
        unknown_quantity_samples=sum(not r.get('quantity_known',True) for r in rows),
        completed_quantity=sum(r['quantity'] for r in rows if r['completed']),ontime_denominator=len(known_due),
        ontime_rate=sum(not r['late'] for r in known_due)/len(known_due) if known_due else None,
        overdue_quantity=sum(r['quantity'] for r in rows if r['late']),
        longest_open_hours=max((r['hours'] for r in rows if not r['completed'] and r['hours'] is not None),default=None))


def measure(raw,data,body):
    result=[];pause_events=defaultdict(list)
    for e in data['events']:
        if e['lifecycle'] in ('paused','resumed') and e['occurred_at'] and e['time_quality']!='unknown':pause_events[e['task_id'] or e['object_id']].append(e)
    for index,source in enumerate(raw):
        r=dict(source);start=r['start_at'];finish=r['end_at'] if r['completed'] else body['end_at'];flags=list(r['flags'])
        raw_hours=(ts(finish)-ts(start)).total_seconds()/3600 if start and finish else None
        if raw_hours is None:flags.append('missing_actual_time')
        elif raw_hours<0:flags.append('negative_duration')
        r.update(raw_hours=raw_hours,hours=round(raw_hours,6) if raw_hours is not None and not flags else None,flags=list(dict.fromkeys(flags)),
            paused_hours=None,active_hours=None,working_hours=None,late=None,observed_until=finish,
            id=fingerprint(dict(index=index,metric=r['metric'],object=r['object_id'],events=r['event_ids']))[:32])
        if r['hours'] is not None:
            r['working_hours']=working_hours(start,finish,body['calendar'],data['timezone'])
            intervals=[];paused=None;bad=False
            for e in sorted(pause_events[r['object_id']],key=lambda e:e['occurred_at']):
                t=e['occurred_at']
                if ts(t)<ts(start) or ts(t)>ts(finish):continue
                if e['lifecycle']=='paused':
                    if paused:bad=True
                    paused=t
                elif paused:intervals.append((paused,t));paused=None
                else:bad=True
            if paused:intervals.append((paused,finish))
            if bad:r['flags'].append('invalid_pause_sequence')
            else:r['paused_hours']=union_hours(intervals);r['active_hours']=round(r['hours']-r['paused_hours'],6)
            if r['planned_at']:r['late']=ts(finish)>ts(r['planned_at'])
        result.append(r)
    return result


def impacts(rows,data,body):
    """Intersect source unavailability, historical active links and unmet demand.

    No same-SKU guessing. One line is bounded by its unfulfilled/unreserved
    quantity. Overlapping supply/inspection intervals are deduplicated by link.
    """
    events=data['events'];lines={x['id']:x for x in data['lines']};orders={x['id']:x for x in data['orders']}
    line_events=defaultdict(list);link_events=defaultdict(list);confirm={}
    for e in events:
        if not e['occurred_at'] or e['time_quality']=='unknown' or e['quality_flags']:continue
        if e['event_type']=='fulfillment.confirmed':confirm[e['object_id']]=e['occurred_at']
        if e['object_line_id']:line_events[e['object_line_id']].append(e)
        if e['payload'].get('link_id'):link_events[e['payload']['link_id']].append(e)
    source_links=defaultdict(list)
    for link in data['links']:source_links[(link['source_type'],link['source_id'])].append(link)
    stock_events=defaultdict(list)
    for e in events:
        if e['event_type']=='inventory.posted':stock_events[e['sku_id']].append(e)
    balances={x['sku_id']:x for x in data.get('balances',[])}
    def free_stock(sku,instant):
        stock=balances.get(sku);history=stock_events[sku]
        if not stock:return None
        versions={e['payload'].get('balance_version') for e in history}
        if len(history)!=stock['version'] or versions!=set(range(1,stock['version']+1)):return None
        keys=('g','r','t','h','b','offline','online','pending','withdrawing');values=dict.fromkeys(keys,0)
        for e in history:
            if ts(e['registered_at'])<=instant:
                for k in keys:values[k]+=e['payload']['delta'].get(k,0)
        return max(0,values['g']-sum(values[k] for k in keys if k!='g'))
    by_line=defaultdict(list)
    for row in rows:
        if row['metric'] not in OPERATIONS or row['hours'] is None:continue
        start=max(ts(row['start_at']),ts(body['start_at']));end=min(ts(row['observed_until']),ts(body['end_at']))
        if end<=start:continue
        for ref in row['sources']:
            for link in source_links[(ref['type'],ref['id'])]:
                if link['line_id'] in lines:by_line[link['line_id']].append((row,link,start,end))
        if row.get('direct_line_id') in lines:by_line[row['direct_line_id']].append((row,None,start,end))
    output=[]
    for lid,spans in by_line.items():
        line=lines[lid];confirmed=confirm.get(line['order_id'])
        if not confirmed:continue
        boundaries={ts(confirmed),ts(body['start_at']),ts(body['end_at'])}
        for _,link,a,b in spans:
            boundaries.update((a,b))
            if link:boundaries.update(ts(e['occurred_at']) for e in link_events[link['id']])
        boundaries.update(ts(e['occurred_at']) for e in line_events[lid]);boundaries.update(ts(e['registered_at']) for e in stock_events[line['sku_id']]);boundaries=sorted(t for t in boundaries if ts(body['start_at'])<=t<=ts(body['end_at']))
        segments=[]
        for a,b in zip(boundaries,boundaries[1:]):
            if a<ts(confirmed) or b<=a:continue
            reserved=shipped=cancelled=0
            for e in line_events[lid]:
                if ts(e['occurred_at'])>a:continue
                n=e['quantity'] or 0;kind=e['event_type']
                if kind=='fulfillment.allocated':reserved+=n
                elif kind=='fulfillment.shipped':reserved-=n;shipped+=n
                elif kind=='fulfillment.batch_cancelled':reserved-=n
                elif kind=='fulfillment.cancelled':cancelled+=n
            if min(reserved,shipped,cancelled)<0:continue
            available=free_stock(line['sku_id'],a)
            unmet=max(0,line['quantity']-reserved-shipped-cancelled-(available or 0));used={};link_limits={};direct={};rowids=set();sources=set()
            for row,link,start,finish in spans:
                if not start<=a<finish:continue
                if link:
                    history=link_events[link['id']];added=any(e['event_type']=='fulfillment.supply_linked' and ts(e['occurred_at'])<=a for e in history)
                    if not added:continue
                    released=sum(e['quantity'] or 0 for e in history if e['event_type']=='fulfillment.supply_released' and ts(e['occurred_at'])<=a)
                    n=min(max(0,link['quantity']-released),row['quantity'],unmet)
                    if n:
                        branches=used.setdefault(link['id'],{});branch=row['dependency_branch'] or row['object_id']
                        branches[branch]=max(branches.get(branch,0),n);link_limits[link['id']]=max(0,link['quantity']-released)
                        sources.add(link['source_type']+':'+link['source_id']);rowids.add(row['id'])
                elif reserved:
                    n=min(reserved,row['quantity']);direct[row['object_id']]=n;rowids.add(row['id'])
            quantity=min(line['quantity']-shipped-cancelled,min(unmet,sum(min(link_limits[k],sum(v.values())) for k,v in used.items()))+min(reserved,sum(direct.values())))
            if quantity:segments.append(dict(start_at=a.isoformat(),end_at=b.isoformat(),quantity=quantity,row_ids=sorted(rowids),sources=sorted(sources),inventory_known=available is not None or bool(direct)))
        if not segments:continue
        # Latest active dependency is the blocking frontier. Parallel branches
        # are represented explicitly, never summed into end-to-end elapsed time.
        finish=max(ts(x['end_at']) for x in segments);frontier=sorted({source for x in segments if ts(x['end_at'])==finish for source in x['sources']})
        graph_nodes={r['id']:dict(sample_id=r['id'],metric=r['metric'],quantity=min(r['quantity'],line['quantity']),branch=r['dependency_branch'],
            start_at=r['start_at'],end_at=r['observed_until'],hours=r['hours']) for r,_,_,_ in spans}
        edges=[];longest={};paths={}
        # Edges require the same explicit receipt/fulfillment batch, temporal
        # order and a legal stage transition. Parallel receipts never add up.
        transitions={('transit','quality_wait'),('quality_wait','quality_work'),('quality_work','restock_wait'),
            ('pick_wait','pick_work'),('pick_work','review_wait'),('review_wait','review_work')}
        ordered=sorted(graph_nodes.values(),key=lambda n:(ts(n['end_at']),ts(n['start_at']),n['sample_id']))
        for node in ordered:
            previous=[p for p in ordered if p['sample_id'] in longest and p['branch'] and p['branch']==node['branch']
                and (p['metric'],node['metric']) in transitions and ts(p['end_at'])<=ts(node['start_at'])]
            for p in previous:edges.append(dict(before=p['sample_id'],after=node['sample_id'],quantity=min(p['quantity'],node['quantity'])))
            best=max(previous,key=lambda p:longest[p['sample_id']],default=None)
            identity=node['sample_id'];longest[identity]=node['hours']+(longest[best['sample_id']] if best else 0)
            paths[identity]=([*paths[best['sample_id']]] if best else [])+[identity]
        terminal=max(longest,key=longest.get) if longest else None
        output.append(dict(object_id=line['order_id'],order_id=line['order_id'],line_id=lid,sku_id=line['sku_id'],connection_id=orders[line['order_id']]['connection_id'],
            quantity=max(x['quantity'] for x in segments),blocked_hours=union_hours([(x['start_at'],x['end_at']) for x in segments]),
            first_blocked_at=min(x['start_at'] for x in segments),last_blocked_at=finish.isoformat(),critical_frontier=frontier,
            dependency_graph=dict(nodes=list(graph_nodes.values()),edges=edges,critical_sample_ids=paths.get(terminal,[]),
                critical_hours=round(longest.get(terminal,0),6),coverage='仅显式收货批次和履约批次内的已知阶段；跨批次衔接未知，不补造边'),
            segments=segments,evidence_scope='explicit_links_and_unreserved_demand_or_active_fulfillment_task'))
    return output


def analyze(raw,data,body):
    rows=measure(raw,data,body);affected=impacts(rows,data,body);groups=defaultdict(list);aggregates=defaultdict(list)
    for r in rows:
        aggregates[r['metric']].append(r)
        groups[(r['metric'],r['sku_id'],r['supplier_id'],r['connection_id'])].append(r)
    candidates=[];start=ts(body['start_at']);finish=ts(body['end_at']);hours=(finish-start).total_seconds()/3600
    for (metric,sku,supplier,account),items in sorted(groups.items(),key=lambda x:str(x[0])):
        if metric not in OPERATIONS|{'registration_delay'}:continue
        stat=summarize(items,body['min_samples']);ids={x['id'] for x in items};matches=[]
        for line in affected:
            segments=[x for x in line['segments'] if ids.intersection(x['row_ids'])]
            if segments:matches.append(dict(order_id=line['order_id'],line_id=line['line_id'],quantity=max(x['quantity'] for x in segments),
                blocked_hours=union_hours([(x['start_at'],x['end_at']) for x in segments]),inventory_known=all(x['inventory_known'] for x in segments)))
        budget=body['registration_hours'] if metric=='registration_delay' else body['waiting_hours']
        long=[r for r in items if r['hours'] is not None and r['hours']>budget]
        checkpoints=[start+(finish-start)*f for f in (1/3,2/3,1)]
        backlog=[sum(r['quantity'] for r in items if r['start_at'] and not r['flags'] and ts(r['start_at'])<=t and (not r['end_at'] or ts(r['end_at'])>t)) for t in checkpoints]
        arrivals=sum(r['quantity'] for r in items if r['start_at'] and start<=ts(r['start_at'])<=finish)
        exits=sum(r['quantity'] for r in items if r['end_at'] and start<=ts(r['end_at'])<=finish)
        signals=dict(over_budget_samples=len(long),promise_late_samples=sum(bool(r['late']) for r in items),backlog_checkpoints=backlog,
            persistent_backlog=sum(n>0 for n in backlog)>=2,arrival_units_per_hour=round(arrivals/hours,6),completion_units_per_hour=round(exits/hours,6),
            arrival_exceeds_completion=arrivals>exits and backlog[-1]>0,waiting_budget_hours=budget)
        pressure=bool((metric in WAIT and long) or signals['promise_late_samples'] or signals['arrival_exceeds_completion'] and signals['persistent_backlog'])
        classification='unknown' if stat['unknown_samples'] and not long and not pressure else 'normal'
        if metric=='registration_delay' and long:classification='data_delay'
        elif pressure:classification='evidenced_constraint' if any(m['inventory_known'] for m in matches) else 'suspected'
        elif long:classification='long_duration'
        if classification=='normal':continue
        quantity=sum(m['quantity'] for m in matches);orderids=sorted({m['order_id'] for m in matches})
        candidates.append(dict(object_id=fingerprint([metric,sku,supplier,account])[:32],metric=metric,sku_id=sku,supplier_id=supplier,connection_id=account,
            classification=classification,statistics=stat,signals=signals,affected_orders=orderids,impact_quantity=quantity,impacts=matches,
            sample_ids=sorted(ids),event_ids=sorted({e for x in items for e in x['event_ids']}),
            missing=(['actual_time_or_valid_clock'] if stat['unknown_samples'] else [])+(['inventory_history_incomplete'] if any(not x['inventory_known'] for x in matches) else []),
            rank_components=[len(orderids),stat['overdue_quantity'],stat['open_quantity']],
            interpretation='仅模拟条件和当前授权范围内的证据；长耗时本身不是限制原因'))
    candidates.sort(key=lambda r:(-r['rank_components'][0],-r['rank_components'][1],-r['rank_components'][2],r['metric'],r['object_id']))
    confirmed_ids={sid for candidate in candidates if candidate['classification']=='evidenced_constraint' for sid in candidate['sample_ids']}
    constraint_lines=[]
    for line in affected:
        matched=[segment for segment in line['segments'] if segment['inventory_known'] and confirmed_ids.intersection(segment['row_ids'])]
        if matched:constraint_lines.append(dict(line_id=line['line_id'],order_id=line['order_id'],quantity=max(x['quantity'] for x in matched)))
    summary=dict(rule_version=RULE,timezone=data['timezone'],calendar=body['calendar'],period=dict(start=body['start_at'],end=body['end_at']),
        metric_definitions=dict(grain='每次到货、检验批次、履约批次或审批单；百分位按样本不按数量加权',unit='小时 / 件',
            quantiles='中位数；P90/P95 最近秩，已完成且有效时间为分母',open_work='未完成单列，不进入已完成分位数',
            impact='同一订单行按时间区间并集去重；影响量为期间最大受阻量，各候选不可相加为总损失',
            latest_promise='该次签收前最近已登记承诺',critical_path='显式批次内阶段有向无环图最长路径；并行不相加，跨批次缺边保持未知'),
        metrics={metric:summarize(items,body['min_samples']) for metric,items in sorted(aggregates.items())},
        affected_order_count=len({x['order_id'] for x in affected}),impact_quantity=sum(x['quantity'] for x in affected),
        constraint_order_count=len({x['order_id'] for x in constraint_lines}),constraint_quantity=sum(x['quantity'] for x in constraint_lines),
        rework_events=sum(e['event_type'] in ('inspection.cancel','exception_task.reopen') for e in data['events']
            if e['occurred_at'] and start<=ts(e['occurred_at'])<=finish),
        source_scope_incomplete=data['scope_incomplete'],candidate_count=len(candidates),sample_count=len(rows),
        unknown_samples=sum(r['hours'] is None for r in rows),correction_count=sum(len(e['evidence_ids'])-1 for e in data['events']))
    return dict(summary=summary,samples=rows,candidates=candidates,impacts=affected)
