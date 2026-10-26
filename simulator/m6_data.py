"""Private-independent time and quantity oracle, without business imports.

Adjacent even/odd seeds are paired copies. Only quality queue service capacity
changes; all demand, transport, picking and observation quality remain fixed.
"""
import random
from .oracle import FIELDS

KINDS=('quality','transport','data_delay','unknown','parallel','unlinked','mixed','pick','stock_available','negative')

def generate(seed):
    rng=random.Random((seed//2)^0x6A019);kind=rng.choice(KINDS);improved=bool(seed%2)
    queue=rng.choice([36,48,60,72]) if kind in ('quality','unknown','parallel','unlinked','mixed','stock_available','negative') else 0
    count=2 if kind=='parallel' else 1;quantities=[rng.randint(2,12) for _ in range(count)]
    return dict(version='m6-1',seed=seed,pair=seed//2,variant='improved' if improved else 'baseline',kind=kind,
        quality_queue_hours=queue//6 if improved else queue,baseline_queue_hours=queue,
        picking_queue_hours=rng.choice([30,36,48]) if kind in ('mixed','pick') else 0,
        transit_hours=120,arrival_offsets=list(range(count)),quantities=quantities,
        registration_hours=48 if kind=='data_delay' else 0,linked=kind!='unlinked',
        actual_quality='unknown' if kind=='unknown' else 'invalid_clock' if kind=='negative' else 'reported',
        stock_available=kind=='stock_available',fault=rng.choice(['none','after_report_commit']))

def expected(masters,case):
    rows=[]
    for master in masters:
        for index,sku in enumerate(master['skus']):
            values=dict.fromkeys(FIELDS,0);values['g']=sku['opening']
            if index==0 and not case['stock_available']:values['h']=sku['opening']
            rows.append(dict(tenant_id=master['tenant_id'],warehouse_id=master['warehouses'][0]['id'],sku_id=sku['id'],**values))
    return sorted(rows,key=lambda x:(x['tenant_id'],x['warehouse_id'],x['sku_id']))

def diagnosis(case):
    constraints=[];unknown=case['actual_quality']!='reported';n=sum(case['quantities'])
    if case['linked'] and not case['stock_available'] and not unknown and case['quality_queue_hours']>24:constraints.append('quality_wait')
    if case['picking_queue_hours']>24:constraints.append('pick_wait')
    received=case['transit_hours']+max(case['arrival_offsets'])+case['registration_hours']
    available=received if case['registration_hours'] else received+case['quality_queue_hours']+1
    elapsed=1.5 if case['stock_available'] else available+case['picking_queue_hours']+1.5
    return dict(constraints=sorted(constraints),constraint_quantity=n if constraints else 0,
        data_delay=case['registration_hours']>24,long_transport=not unknown,
        unknown_operations=['quality_wait','transit'] if unknown else [],order_ship_hours=elapsed,
        transit_hours=None if unknown else case['transit_hours']+(max(case['arrival_offsets'])/2))
