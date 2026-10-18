"""Generate related business inputs. No business-system imports or database copies."""
from datetime import datetime, timedelta, timezone
import random
from .common import digest
from .config import Scenario, working_finish


def generate(config: Scenario):
    rng = random.Random(config.seed)
    start = datetime.fromisoformat(config.start_time)
    events, masters, factors = [], [], []
    clock = {}

    def emit(kind, tenant, sku, at, values, *, dependencies=(), resource='operations', planned=None):
        logical_id = f'EV-{len(events)+1:07d}'
        delivery = at + timedelta(hours=36 if config.scenario in ('data_latency', 'mixed') else 0)
        item = dict(id=logical_id, business_action_id=logical_id, kind=kind, tenant_id=tenant,
                    sku_id=sku, warehouse_id=tenant+'-wh-0', values=values,
                    dependencies=list(dependencies), process_id=tenant+'-'+sku,
                    planned_at=(planned or at).astimezone(timezone.utc).isoformat(),
                    occurred_at=at.astimezone(timezone.utc).isoformat(),
                    deliver_at=delivery.astimezone(timezone.utc).isoformat(), resource=resource,
                    source_account='SIM-01', source_version=1, source_event_id=logical_id)
        events.append(item)
        clock[logical_id] = at
        return logical_id

    for ti in range(config.tenant_count):
        tenant = f'tenant-{ti}'
        warehouses = [dict(id=f'{tenant}-wh-{i}', code=f'WH-{i:02d}', name=f'模拟仓库 {i+1}')
                      for i in range(config.warehouse_count)]
        products = [dict(id=f'{tenant}-sku-{i}', code=f'SKU-{i:05d}', name=f'模拟商品 {i+1}',
                         opening=150+40*((config.order_count+config.sku_count-1)//config.sku_count))
                    for i in range(config.sku_count)]
        masters.append(dict(tenant_id=tenant, name=f'独立模拟商户 {ti+1}', warehouses=warehouses,
                            skus=products, supplier=dict(code='SUP-01', name='模拟供应商',
                            lead_days=config.supplier_lead_days)))
        for si, product in enumerate(products):
            sku = product['id']; wh = warehouses[0]['id']
            base = start + timedelta(minutes=si)
            supplied = rng.randint(12, 48)
            first = supplied//2
            quality = round(first*config.inspection_rate)
            po = emit('purchase', tenant, sku, base, {'code': f'PO-{si:05d}', 'quantity': supplied, 'supplier_code':f'SUP-01-{si//1000}'}, resource='supplier')
            promised = base + timedelta(days=config.supplier_lead_days)
            late = 4 if config.scenario in ('supplier_delay', 'mixed') else 0
            at = promised + timedelta(days=late)
            one = emit('purchase_receipt', tenant, sku, at,
                       dict(parent=po, reference=f'PO-{si}-DEL-1', g=first-quality, q=quality, d=0),
                       dependencies=[po], resource='supplier', planned=promised)
            two = emit('purchase_receipt', tenant, sku, at+timedelta(hours=4),
                       dict(parent=po, reference=f'PO-{si}-DEL-2', g=supplied-first, q=0, d=0),
                       dependencies=[one], resource='supplier', planned=promised+timedelta(hours=4))
            previous = two
            if quality:
                capacity = max(1, config.quality_units_per_hour//8) if config.scenario in ('warehouse_congestion', 'mixed') else config.quality_units_per_hour
                queued = max(clock[two], clock.get(tenant+'-quality-queue', start))
                done = working_finish(queued, quality/capacity, config.calendar, config.timezone)
                clock[tenant+'-quality-queue'] = done
                previous = emit('quality', tenant, sku, done, dict(good=quality, bad=0, reference=f'QC-{si}'),
                                dependencies=[two], resource='quality', planned=clock[two]+timedelta(hours=1))
            sales = []
            for oi in range(si, config.order_count, config.sku_count):
                amount = rng.randint(1, 10)
                if config.sales_distribution == 'peaked':
                    amount = rng.choice([2, 2, 3, 10])
                elif config.sales_distribution == 'long_tail':
                    amount = min(12, max(1, int(rng.paretovariate(2))))
                if config.scenario in ('demand_spike', 'mixed'):
                    amount *= 3
                if config.scenario == 'slow_sales':
                    amount = 1
                dispatch = max(clock[previous], clock.get(tenant+'-shipping-queue', start))
                if config.scenario == 'slow_sales':
                    dispatch += timedelta(days=2)
                done = working_finish(dispatch, amount/config.shipping_units_per_hour, config.calendar, config.timezone)
                clock[tenant+'-shipping-queue'] = done
                sale = emit('sale', tenant, sku, done, dict(quantity=amount, reference=f'ORDER-{oi:06d}-SHIP'),
                            dependencies=[previous], resource='shipping', planned=dispatch+timedelta(hours=1))
                previous = sale
                sales.append((sale, amount))
                return_rate = max(config.return_rate, .8) if config.scenario in ('returns_surge', 'mixed') else config.return_rate
                if rng.random() < return_rate:
                    returned = rng.randint(1, amount)
                    ret = emit('return', tenant, sku, done+timedelta(hours=5),
                               dict(parent=sale, quantity=returned, reference=f'RET-{oi:06d}'), dependencies=[sale], resource='returns')
                    bad = rng.randint(0, returned)
                    qc_start = max(clock[ret], clock.get(tenant+'-quality-queue', start))
                    qc_done = working_finish(qc_start, returned/config.quality_units_per_hour, config.calendar, config.timezone)
                    clock[tenant+'-quality-queue'] = qc_done
                    previous = emit('return_quality', tenant, sku, qc_done,
                                    dict(parent=ret, good=returned-bad, bad=bad, reference=f'RET-QC-{oi:06d}'),
                                    dependencies=[ret], resource='quality')
            quantity = rng.randint(6, 12)
            destination = warehouses[1+si%(config.warehouse_count-1)]['id']
            transfer = emit('transfer', tenant, sku, clock[previous]+timedelta(minutes=1),
                            dict(code=f'TR-{si:05d}', quantity=quantity, destination_id=destination), dependencies=[previous], resource='transport')
            reserved = emit('transfer_reserve', tenant, sku, clock[transfer]+timedelta(minutes=1),
                            dict(parent=transfer), dependencies=[transfer], resource='shipping')
            sent = emit('transfer_ship', tenant, sku, clock[reserved]+timedelta(hours=1),
                        dict(parent=transfer), dependencies=[reserved], resource='transport')
            arrive = clock[sent]+timedelta(days=3)
            delay = 5 if config.scenario in ('transport_exception', 'mixed') else 0
            part = quantity//2
            received = emit('transfer_receive', tenant, sku, arrive+timedelta(days=delay),
                            dict(parent=transfer, destination_id=destination, g=part, q=0, d=0, reference=f'TR-{si}-DEL-1'),
                            dependencies=[sent], resource='transport', planned=arrive)
            if not delay:
                emit('transfer_receive', tenant, sku, arrive+timedelta(hours=6),
                     dict(parent=transfer, destination_id=destination, g=quantity-part, q=0, d=0, reference=f'TR-{si}-DEL-2'),
                     dependencies=[received], resource='transport', planned=arrive)
            else:
                factors.append(dict(process_id=tenant+'-'+sku, factor='transport', affected_units=quantity-part,
                                    evidence=[sent, received], actual_delay_days=delay))
            if late:
                factors.append(dict(process_id=tenant+'-'+sku, factor='supplier', affected_units=supplied,
                                    evidence=[po, one], actual_delay_days=late))
            if config.scenario in ('warehouse_congestion', 'mixed'):
                factors.append(dict(process_id=tenant+'-'+sku, factor='quality_capacity', affected_units=quality,
                                    capacity_per_hour=max(1, config.quality_units_per_hour//8)))
            if 'oversell' in config.declared_anomalies:
                emit('sale', tenant, sku, clock[received]+timedelta(hours=12),
                     dict(quantity=1_000_000, reference=f'REJECT-{si}'), dependencies=[received], resource='shipping')
        if 'cross_tenant' in config.declared_anomalies:
            foreign = f'tenant-{(ti+1)%config.tenant_count}'
            emit('foreign_receipt', tenant, products[0]['id'], base,
                 dict(warehouse_id=foreign+'-wh-0', g=1, q=0, d=0, reference='FORBIDDEN'), resource='permission')

    dataset = dict(version='m2-input-1', config=config.model_dump(), masters=masters, events=events)
    # Ground truth remains in the control process. Never include these labels in
    # business evidence or the worker configuration.
    truth = dict(version='truth-1', input_sha256=digest(dataset), known_factors=factors,
                 capacities=dict(quality=config.quality_units_per_hour, shipping=config.shipping_units_per_hour),
                 physical_events=[dict(id=e['id'], occurred_at=e['occurred_at'], values=e['values']) for e in events])
    validate_dataset(dataset)
    return dataset, truth


def validate_dataset(dataset):
    ids, known = set(), {}
    tenant_skus = {m['tenant_id']: {s['id'] for s in m['skus']} for m in dataset['masters']}
    for event in dataset['events']:
        if event['id'] in ids or not set(event['dependencies']) <= known.keys():
            raise ValueError('Duplicate event or invalid/cyclic dependency')
        if event['sku_id'] not in tenant_skus[event['tenant_id']]:
            raise ValueError('SKU does not belong to the event tenant')
        if 'parent' in event['values'] and event['values']['parent'] not in known:
            raise ValueError('Unknown parent business action')
        if any(datetime.fromisoformat(known[dep]) >
               datetime.fromisoformat(event['occurred_at']) for dep in event['dependencies']):
            raise ValueError('Dependent event predates its prerequisite')
        ids.add(event['id']); known[event['id']]=event['occurred_at']
    return {'events': len(ids), 'tenants': len(tenant_skus), 'valid': True}
