"""Independent quantity model. Deliberately imports no app/business code."""
from .common import digest

FIELDS = ('g', 'q', 'd', 'r', 't', 'h', 'b', 'offline', 'online', 'pending', 'withdrawing')


def expected(dataset):
    stock, objects, accepted, errors = {}, {}, [], {}
    postings = 0
    def row(tenant, warehouse, sku):
        return stock.setdefault((tenant, warehouse, sku), dict.fromkeys(FIELDS, 0))
    for master in dataset['masters']:
        for sku in master['skus']:
            row(master['tenant_id'], master['warehouses'][0]['id'], sku['id'])['g'] = sku['opening']
            postings += 1
    for event in dataset['events']:
        kind = event['kind']; v = event['values']; identity = event['id']
        state = row(event['tenant_id'], event['warehouse_id'], event['sku_id'])
        if kind == 'foreign_receipt':
            errors[identity] = 'NOT_FOUND'; continue
        if kind == 'sale' and v['quantity'] > state['g']-state['r']-state['t']-state['h']-state['b']:
            errors[identity] = 'INSUFFICIENT_STOCK'; continue
        accepted.append(identity)
        if kind == 'purchase':
            objects[identity] = dict(kind=kind, quantity=v['quantity'], received=0)
        elif kind == 'purchase_receipt':
            total = v['g']+v['q']+v['d']
            purchase = objects[v['parent']]
            purchase['received'] += total
            if purchase['received'] > purchase['quantity']:
                raise ValueError('Generated purchase receipts exceed supply')
            for f in ('g', 'q', 'd'): state[f] += v[f]
            postings += 1
        elif kind in ('quality', 'return_quality'):
            state['q'] -= v['good']+v['bad']; state['g'] += v['good']; state['d'] += v['bad']
            postings += 1
        elif kind == 'sale':
            state['g'] -= v['quantity']; objects[identity] = dict(kind=kind, quantity=v['quantity'], returned=0)
            postings += 1
        elif kind == 'return':
            sale = objects[v['parent']]; sale['returned'] += v['quantity']
            if sale['returned'] > sale['quantity']:
                raise ValueError('Generated returns exceed source shipment')
            state['q'] += v['quantity']; postings += 1
        elif kind == 'transfer':
            objects[identity] = dict(kind=kind, quantity=v['quantity'], sent=0, received=0)
        elif kind == 'transfer_reserve':
            state['t'] += objects[v['parent']]['quantity']; postings += 1
        elif kind == 'transfer_ship':
            transfer = objects[v['parent']]; transfer['sent'] = transfer['quantity']
            state['t'] -= transfer['quantity']; state['g'] -= transfer['quantity']; postings += 1
        elif kind == 'transfer_receive':
            target = row(event['tenant_id'], v['destination_id'], event['sku_id'])
            for f in ('g', 'q', 'd'): target[f] += v[f]
            objects[v['parent']]['received'] += sum(v[f] for f in ('g', 'q', 'd')); postings += 1
        else:
            raise ValueError('Unmodelled event: '+kind)
        if any(value < 0 for value in state.values()):
            raise ValueError('Undeclared impossible quantity constraint')
    balances = [dict(tenant_id=t, warehouse_id=w, sku_id=s, **value) for (t,w,s),value in sorted(stock.items())]
    transit = sum(x['sent']-x['received'] for x in objects.values() if x['kind']=='transfer')
    return dict(balances=balances, accepted=accepted, errors=errors, postings=postings, in_transit=transit,
                purchases=sum(x['kind']=='purchase' for x in objects.values()),
                transfers=sum(x['kind']=='transfer' for x in objects.values()))


def compare(dataset, actual, outcomes):
    target = expected(dataset)
    checks = []
    def check(name, wanted, found):
        checks.append(dict(name=name, passed=wanted==found, expected=wanted, actual=found))
    check('inventory_quantities', target['balances'], actual['balances'])
    for table in ('documents', 'ledger', 'outbox'):
        check(table+'_atomic_count', target['postings'], actual['counts'][table])
    check('business_events_count', target['postings']+len(target['accepted'])+2*sum(e['kind'] in ('return','quality','return_quality') and e['id'] in target['accepted'] for e in dataset['events']), actual['counts']['business_events'])
    check('in_transit_conservation', target['in_transit'], actual['in_transit'])
    check('purchase_count', target['purchases'], actual['counts']['purchase_orders'])
    check('transfer_count', target['transfers'], actual['counts']['transfers'])
    for identity in target['accepted']:
        check(identity+':accepted', 'ok', outcomes.get(identity, {}).get('status'))
    for identity, error in target['errors'].items():
        check(identity+':expected_rejection', error, outcomes.get(identity, {}).get('error'))
    check('tenant_permission_checks', True, all(actual['permission_checks'].values()))
    check('ledger_arithmetic_and_links', True, actual['ledger_consistent'])
    failed = [item for item in checks if not item['passed']]
    return dict(passed=not failed, checks=len(checks), failures=failed,
                expected_sha256=digest(target), actual_sha256=digest(actual))
