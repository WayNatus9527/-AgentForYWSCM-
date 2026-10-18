"""M3 synthetic workload and independent arithmetic; imports no application code."""
import random
from .oracle import FIELDS


def generate(seed):
    rng=random.Random(seed ^ 0x3C013)
    return dict(version='m3-1',seed=seed,conflict=rng.choice([True,False]),count_delta=rng.choice([-1,0,1,4]),
        quality_good=rng.randint(1,5),quality_bad=rng.randint(1,4),sale=rng.randint(2,4),return_good=rng.randint(1,2))


def expected(masters,case):
    rows=[]
    for master in masters:
        first=master['skus'][0]
        for item in master['skus']:
            row=dict.fromkeys(FIELDS,0);row['g']=item['opening']
            if item['id']==first['id']:
                delta=3 if case['conflict'] else case['count_delta']
                row['g']+=delta+10-case['sale']+case['quality_good']+case['return_good']-1
                row['online']=item['opening']-2 if case['conflict'] else 0
            rows.append(dict(tenant_id=master['tenant_id'],warehouse_id=master['warehouses'][0]['id'],sku_id=item['id'],**row))
        row=dict.fromkeys(FIELDS,0);row['g']=1
        rows.append(dict(tenant_id=master['tenant_id'],warehouse_id=master['warehouses'][1]['id'],sku_id=first['id'],**row))
    return sorted(rows,key=lambda x:(x['tenant_id'],x['warehouse_id'],x['sku_id']))
