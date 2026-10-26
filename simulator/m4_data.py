"""Independent M4 inputs and quantity oracle. Does not import business services."""
import random
from .oracle import FIELDS


def generate(seed):
    rng = random.Random(seed ^ 0x4F041)
    shipped, cancelled, remaining = rng.randint(1, 7), rng.randint(1, 5), rng.randint(1, 4)
    return dict(version='m4-1', seed=seed, shipped=shipped, cancelled=cancelled, remaining=remaining,
        quantity=shipped + cancelled + remaining, finish=rng.choice([True, False]), grant=rng.choice([True, False]),
        restore=rng.choice([True, False]), inbox_cancel=rng.choice([True, False]),
        fault=rng.choice(['success', 'timeout_after', 'disconnect', 'rate_limit', 'no_query_timeout', 'worker_crash']),
        snapshot_order=rng.choice(['snapshot_first', 'delta_first', 'no_watermark']))


def expected(masters, case):
    result = []
    for master in masters:
        for index, item in enumerate(master['skus']):
            quantities = dict.fromkeys(FIELDS, 0); quantities['g'] = item['opening']
            if index == 0:
                quantities['g'] -= case['shipped'] + (case['remaining'] if case['finish'] else 0)
                quantities['r'] = 0 if case['finish'] else case['remaining']
                quantities['online'] = (7 + (case['cancelled'] if case['restore'] else 0)) if case['grant'] else 0
            result.append(dict(tenant_id=master['tenant_id'], warehouse_id=master['warehouses'][0]['id'], sku_id=item['id'], **quantities))
    return sorted(result, key=lambda r: (r['tenant_id'], r['warehouse_id'], r['sku_id']))
