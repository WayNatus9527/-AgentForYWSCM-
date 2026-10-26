"""Independent generated supply quantities and hand-auditable planning oracle."""
import random
from .oracle import FIELDS

def generate(seed):
    rng=random.Random(seed ^ 0x5A017)
    qgood,qbad=rng.randint(1,5),rng.randint(0,3)
    sent=rng.randint(5,12);received=rng.randint(1,sent-1)
    fault=rng.choice(['none','restart_after_confirm'])
    return dict(version='m5-1',seed=seed,good=rng.randint(2,9),quality_good=qgood,quality_bad=qbad,restocked=rng.randint(0,qgood),
        bad=rng.randint(0,2),remaining=rng.randint(1,8),transfer_sent=sent,transfer_received=received,transfer_bad=rng.randint(0,min(1,received)),
        transfer_lost=rng.randint(0,sent-received),supply_due=rng.choice(['timely','overdue','unknown','outside']),
        demand_quality='known' if fault=='restart_after_confirm' else rng.choice(['known','missing','abnormal']),
        daily=[rng.randint(50,160) for _ in range(3)],coverage=rng.randint(1,5),lead=rng.randint(1,4),safety=rng.randint(0,30),
        minimum=rng.choice([1,10,20]),pack=rng.choice([1,4,6,12]),open_order=rng.randint(1,10),reserve_order=rng.choice([True,False]),
        includes_open_orders=rng.choice([True,False]),close_remaining=rng.choice([True,False]),fault=fault)

def expected(masters,case):
    result=[]
    for master in masters:
        for index,item in enumerate(master['skus']):
            values=dict.fromkeys(FIELDS,0);values['g']=item['opening']
            if index==0:
                values['g']+=case['good']+case['restocked']-case['transfer_sent']
                values['q']=case['quality_good']-case['restocked'];values['d']=case['bad']+case['quality_bad']
                values['r']=case['open_order'] if case['reserve_order'] else 0
            result.append(dict(tenant_id=master['tenant_id'],warehouse_id=master['warehouses'][0]['id'],sku_id=item['id'],**values))
        values=dict.fromkeys(FIELDS,0);values['g']=case['transfer_received']-case['transfer_bad'];values['d']=case['transfer_bad']
        result.append(dict(tenant_id=master['tenant_id'],warehouse_id=master['warehouses'][1]['id'],sku_id=master['skus'][0]['id'],**values))
    return sorted(result,key=lambda r:(r['tenant_id'],r['warehouse_id'],r['sku_id']))

def expected_plans(masters,case):
    result={}
    for master in masters:
        if case['demand_quality']!='known':result[master['tenant_id']]=None;continue
        stock=master['skus'][0]['opening']+case['good']+case['restocked']-case['transfer_sent']
        reserved=case['open_order'] if case['reserve_order'] else 0
        forecast=(sum(case['daily'])*(case['lead']+case['coverage'])+2)//3
        demand=(max(forecast,case['open_order']) if case['includes_open_orders'] else forecast+case['open_order'])-reserved
        incoming=case['remaining'] if case['supply_due']=='timely' and not case['close_remaining'] else 0
        shortage=max(0,demand+case['safety']-(stock-reserved)-incoming)
        quantity=((max(shortage,case['minimum'])+case['pack']-1)//case['pack'])*case['pack'] if shortage else 0
        result[master['tenant_id']]=quantity
    return result
