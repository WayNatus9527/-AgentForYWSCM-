"""Controller-only diagnostic error and paired-intervention accounting."""
from .common import read_json

def diagnostics_summary(results,cases,control):
    tp=fp=fn=tn=impact_error=unknown=observed=0;pairs={};missing=0
    universe={'transit','quality_wait','quality_work','restock_wait','pick_wait','pick_work','review_wait','review_work','approval_wait'}
    for result,item in zip(results,cases):
        dataset=read_json(control.root/'runs'/item['run_id']/'inputs.json');case=dataset['m6'];wanted=result.get('expected_diagnosis',{})
        found=result.get('diagnoses',{});missing+=len(dataset['masters'])-len(found)
        for actual in found.values():
            target=set(wanted.get('constraints',[]));predicted=set(actual['constraints'])
            tp+=len(target&predicted);fp+=len(predicted-target);fn+=len(target-predicted)
            tn+=len(universe-target-predicted-set(wanted['unknown_operations']))
            impact_error+=abs(actual['constraint_quantity']-wanted['constraint_quantity']);unknown+=bool(actual['unknown_operations']);observed+=1
        pairs.setdefault(case['pair'],{})[case['variant']]=dict(case=case,diagnoses=found,status=result['status'])
    compared=[]
    for pair,variants in pairs.items():
        if set(variants)!=set(('baseline','improved')):continue
        before,after=variants['baseline'],variants['improved']
        for tenant in before['diagnoses'].keys()&after['diagnoses'].keys():
            a,b=before['diagnoses'][tenant],after['diagnoses'][tenant];case=before['case']
            expected=case['quality_queue_hours']-after['case']['quality_queue_hours'] if not case['stock_available'] else 0
            delta=a['order_ship_hours']-b['order_ship_hours']
            compared.append(dict(pair=pair,tenant=tenant,kind=case['kind'],single_changed_parameter='quality_queue_service_capacity',
                before_hours=a['order_ship_hours'],after_hours=b['order_ship_hours'],reduction_hours=delta,expected_reduction=expected,
                before_constraints=a['constraints'],after_constraints=b['constraints'],passed=delta==expected))
    return dict(independent_scenario_groups=len(pairs),diagnostic_accuracy=dict(true_positives=tp,false_positives=fp,false_negatives=fn,true_negatives=tn,impact_absolute_error=impact_error,
        false_positive_rate=fp/(fp+tn) if fp+tn else None,false_negative_rate=fn/(fn+tp) if fn+tp else None,
        unknown_diagnoses=unknown,observed_diagnoses=observed,missing_diagnoses=missing,unknown_rate=unknown/observed if observed else None),
        paired_experiments=dict(compared=len(compared),passed=sum(x['passed'] for x in compared),cases=compared),
        interpretation='模拟单参数干预结果；不外推为真实仓库因果效果或模型准确率')
