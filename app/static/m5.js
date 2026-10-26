'use strict';
const m5Kinds=['purchase','transfer','policies','plans','demand'];
let m5={purchase:m4Empty(),transfer:m4Empty(),policies:m4Empty(),plans:m4Empty(),demand:m4Empty(),kind:'purchase',error:''};
const m5Read=()=>['admin','warehouse','operations','analyst'].includes(state.user?.role);
const m5Write=()=>['admin','warehouse'].includes(state.user?.role);
const m5Labels={purchase:'采购计划',transfer:'分批调拨',open:'待收货',partial:'部分完成',closed:'余量已关闭',completed:'已完成',draft:'待分批',reserved:'已预占',planned:'已计划',in_transit:'运输中',received:'已收齐',cancelled:'已取消',closed_difference:'差异已结案',pending:'待独立复核',approved:'已批准',rejected:'已驳回',confirmed:'已确认采购',declined:'本次不采购',ready:'可确认',unknown:'依据不足',known:'已确认',abnormal:'需核实',unbatched:'未分批计划',unshipped_batch:'未发批次',legacy_unreceived:'历史未收计划',legacy_in_transit:'历史在途',uncommitted:'尚未预占',unknown_time:'交期未知',overdue:'已逾期',outside_horizon:'超出覆盖期'};
const m5Label=x=>m5Labels[x]||x;
const m5Reasons={SUPPLIER_UNAVAILABLE:'供应商不可用',SUPPLIER_SKU_UNAVAILABLE:'缺少供货关系',POLICY_DISABLED:'策略已停用',INVENTORY_UNKNOWN:'库存基准未知',REPLENISHMENT_DISABLED:'未启用补货',DEMAND_INCOMPLETE:'需求历史缺日',DEMAND_UNRELIABLE:'需求异常或未核实',ORDER_SCOPE_INCOMPLETE:'订单依据未完整授权',QUANTITY_EXCEEDS_LIMIT:'建议量超过单次采购上限'};
const m5Btn=(action,id,label,extra='')=>`<button class="secondary-button" data-m5="${action}" data-id="${esc(id)}" ${extra}>${esc(label)}</button>`;
const m5Pill=s=>`<span class="pill ${['unknown','pending','closed_difference'].includes(s)?'warn':''}">${esc(m5Label(s))}</span>`;
function m5Reset(){for(const kind of m5Kinds)m5[kind]=m4Empty();m5.error='';m5.kind='purchase'}
async function m5PageLoad(kind,offset=0){m5[kind]=await api(`/v2/${['purchase','transfer'].includes(kind)?'supply-orders/':'planning/'}${kind}?limit=20&offset=${offset}`)}
async function m5Load(){if(!m5Read()){m5Reset();return}m5.error='';try{await Promise.all(m5Kinds.map(k=>m5PageLoad(k)))}catch(e){m5.error=e.message}}
const m5Error=()=>m5.error?`<div class="notice"><p>${esc(m5.error)}</p>${m5Btn('refresh','','重新读取')}</div>`:'';
function m5Pager(kind){const p=m5[kind];return `<div class="table-foot"><span>本页 ${p.items.length} 条</span><div class="action-buttons">${m5Btn('page',kind,'上一页',`data-offset="${Math.max(0,p.offset-p.limit)}" ${p.offset?'':'disabled'}`)}${m5Btn('page',kind,'下一页',`data-offset="${p.offset+p.limit}" ${p.has_more?'':'disabled'}`)}</div></div>`}
const m5Orders=()=>[...m5.purchase.items,...m5.transfer.items];
const m5Vendor=id=>m1.suppliers.items.find(x=>x.id===id)?.name||id;
const m5Vendors=()=>m1.suppliers.items.filter(x=>x.active).map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
const m5History=r=>`<details class="m3-history"><summary>过程与凭证（${r.history.length}）</summary>${r.history.map(x=>`<p>${date(x.created_at)} · ${esc(({created:'创建计划',promise_changed:'变更承诺',planned:'计划批次',reserved:'分批预占',ship:'实际发出',receive:'实际签收',cancel:'取消未发批次',remaining_closed:'关闭未发余量',loss_approved:'核销在途差异'})[x.action]||x.action)} · ${esc(x.payload.reason||'')}<br><small>${esc(x.payload.reference||'')}${x.action==='promise_changed'?` · 原承诺 ${date(x.payload.original_due)} → 本次承诺 ${date(x.payload.promised_at)}`:''}</small></p>`).join('')}</details>`;
function m5SupplyPage(){
 if(!m5Read())return heading('采购与运输计划','当前岗位无供给计划权限。');
 const kind=m5.kind;
 return heading('采购与运输计划','承诺变更留痕；每批发出、签收与质量结果独立记录。',m5Write()?`<div class="action-buttons">${m5Btn('new',kind,'＋ '+m5Label(kind))}${m5Btn('scan','','检查采购延期')}</div>`:'')+m5Error()+
 `<div class="tabs agent-tabs">${['purchase','transfer'].map(k=>`<button data-m5="tab" data-id="${k}" class="${kind===k?'active':''}">${m5Label(k)}</button>`).join('')}</div>`+
 m5[kind].items.map(r=>`<section class="card form-card m5-order" data-order="${esc(r.id)}"><div class="card-head"><div><h2>${esc(r.code)} ${m5Pill(r.status)}</h2><p class="muted">${kind==='purchase'?esc(m5Vendor(r.terms.supplier_id)):esc(wh(r.warehouse_id).name)+' → '+esc(wh(r.destination_id).name)} · 版本 ${r.version}</p></div><div class="action-buttons">${m5Write()&&kind==='purchase'&&['open','partial'].includes(r.status)?m5Btn('promise',r.id,'变更承诺交期'):''}${m5Write()&&r.lines.some(x=>x.unbatched>0)?m5Btn('close',r.id,'关闭未分批余量'):''}</div></div>
 ${r.terms?.replacement_return_id?`<p class="muted">补发关联退货单 ${esc(r.terms.replacement_return_id)}</p>`:''}
 ${r.terms?`<div class="m5-facts"><p>下单时间<strong>${date(r.terms.ordered_at)}</strong></p><p>原始承诺<strong>${date(r.terms.original_due)}</strong></p><p>最新承诺<strong>${date(r.terms.latest_due)}</strong></p></div>`:''}
 <div class="table-wrap"><table><thead><tr><th>商品</th><th>计划</th><th>未分批</th><th>已预占</th><th>实收</th><th>在途</th><th>待验</th><th>累计已可用</th><th>操作</th></tr></thead><tbody>${r.lines.map(l=>`<tr><td>${productCell(l.sku_id)}</td><td>${l.quantity}</td><td>${l.unbatched}</td><td>${l.reserved}</td><td>${l.received}</td><td>${l.in_transit}</td><td>${l.pending_quality}</td><td>${l.became_available}</td><td>${m5Write()&&l.unbatched>0?m5Btn('batch',r.id,kind==='transfer'?'分批预占':'计划到货批次',`data-line="${l.id}"`):''}</td></tr>`).join('')}</tbody></table></div>
 <p class="muted">累计已可用表示到货后曾入良品或检验上架的数量，不等于当前剩余库存。</p>
 ${r.lines.flatMap(l=>l.batches.map(b=>`<article class="m5-batch" data-batch="${esc(b.id)}"><div class="card-head"><div><h3>${esc(b.reference)} ${m5Pill(b.status)}</h3><p class="muted">${esc(sku(b.sku_id).name)} · 数量 ${b.quantity} · 已发 ${b.sent} · 已收 ${b.received} · 核销差异 ${b.lost}</p></div><div class="action-buttons">${m5Write()&&['planned','reserved'].includes(b.status)?m5Btn('ship',b.id,'登记本批发出')+m5Btn('cancel',b.id,'取消未发批次'):''}${m5Write()&&b.in_transit>0?m5Btn('receive',b.id,'登记本批签收')+(b.kind==='transfer'&&!b.claims.some(x=>x.status==='pending')?m5Btn('claim',b.id,'申报本批差异'):''):''}</div></div>
 <p class="muted">承运 ${esc(b.carrier||'未知')} · 方式 ${esc(b.transport_mode||'未知')} · 运单 ${esc(b.tracking_number||'未知')}<br>预计发出 ${date(b.planned_ship_at)} · 实际发出 ${date(b.shipped_at)}<br>预计到达 ${date(b.expected_at)} · 最近实际签收 ${date(b.arrived_at)}</p>
 ${b.receipts.map(x=>`<div class="m5-receipt"><strong>${esc(x.reference)}</strong> · 良/待/残 ${x.g}/${x.q}/${x.d}<p class="muted">实际到达 ${date(x.occurred_at)} · 登记 ${date(x.created_at)}<br>待开始检验 ${x.uninspected} · 当前待验 ${x.pending_quality} · 已退供应商 ${x.returned}<br>关联单据 ${esc(x.document_id)}</p>${m5Write()&&x.uninspected?m5Btn('inspect',x.id,'开始本次到货检验'):''}${x.inspections.length?'<button class="text-button" data-view="quality">查看检验与上架 →</button>':''}</div>`).join('')}
 ${b.claims.map(x=>`<div class="notice"><p>本批差异 ${x.quantity} · ${esc(x.details.opinion)} ${m5Pill(x.status)}</p>${state.user.role==='admin'&&x.status==='pending'&&x.requester_id!==state.user.id?m5Btn('review',x.id,'独立复核'):''}</div>`).join('')}${m5History(b)}</article>`)).join('')}${m5History(r)}</section>`).join('')+
 (!m5[kind].items.length?'<section class="card form-card"><p>暂无计划。新采购先在基础资料中维护供应商及供货商品。</p></section>':'')+m5Pager(kind);
}
function m5PlanningPage(){
 if(!m5Read())return heading('规则补货','当前岗位无计划权限。');
 return heading('规则补货','先核对需求、库存与及时供给，再由方案创建者确认采购。',m5Write()?`<div class="action-buttons">${m5Btn('policy-new','','＋ 补货策略')}${m5Btn('demand','','登记日需求')}</div>`:'')+m5Error()+
 `<section class="card form-card"><h2>补货策略</h2>${m5.policies.items.map(p=>`<article class="m4-batch"><div><strong>${esc(wh(p.warehouse_id).name)} · ${esc(sku(p.sku_id).name)}</strong><p>需求窗口 ${p.window_days} 天 · 覆盖 ${p.coverage_days} 天 · 提前期 ${p.lead_days} 天<br>目标安全量 ${p.safety} · 最小采购 ${p.minimum} · 包装倍数 ${p.pack}</p><small>${p.includes_open_orders?'预测已包含未履约订单':'预测为未履约订单以外的新增需求'} · ${p.active?'启用':'停用'}</small></div>${m5Write()?`<div class="action-buttons">${m5Btn('calculate',p.id,'计算补货建议')}${m5Btn('policy-edit',p.id,'编辑策略')}</div>`:''}</article>`).join('')||'<p class="muted">暂无策略。需求历史缺失时会显示依据不足。</p>'}${m5Pager('policies')}</section>
 <section class="card form-card"><h2>待确认方案与历史</h2>${m5.plans.items.map(p=>{const d=p.snapshot.calculation;return `<article class="m5-plan" data-plan="${esc(p.id)}"><div class="card-head"><div><h3>${esc(wh(p.warehouse_id).name)} · ${esc(sku(p.sku_id).name)} ${m5Pill(p.decision?(p.decision.quantity?'confirmed':'declined'):p.status)}</h3><p class="muted">数据时点 ${date(p.as_of)} · ${esc(p.snapshot.rule_version)}</p></div><strong class="m5-quantity">${p.suggested===null?'待核实':num(p.suggested)+' '+esc(sku(p.sku_id).unit)}</strong></div>
 ${p.snapshot.reasons.length?`<div class="notice"><p>${p.snapshot.reasons.map(x=>esc(m5Reasons[x]||x)).join('；')}</p></div>`:''}
 <div class="m5-facts"><p>覆盖期预测<strong>${d.forecast??'未知'}</strong></p><p>现有未履约订单<strong>${d.order_unfulfilled}</strong></p><p>订单已占用量<strong>${d.order_reserved_already_deducted}</strong></p><p>未分配现货<strong>${d.unallocated_stock}</strong></p><p>可信及时供给<strong>${d.eligible_supply}</strong></p><p>风险供给（未计入）<strong>${d.risky_supply}</strong></p></div>
 <p>需另覆盖需求 ${d.demand_after_reservations??'未知'} ＋ 目标安全量 ${d.target_safety} − 未分配现货 ${d.unallocated_stock} − 及时供给 ${d.eligible_supply}<br><span class="muted">净补量 ${d.net_before_purchase_constraints??'未知'}，再按最小采购量和包装倍数调整；订单占用从需求和现货两边对应扣除。</span></p>
 ${p.decision?`<p>已确认数量 ${p.decision.quantity} · ${esc(p.decision.reason)}${p.decision.order_id?' · 已生成采购计划':''}</p>`:m5Write()&&p.creator_id===state.user.id&&p.status==='ready'?m5Btn('confirm-plan',p.id,'核对并确认采购'):''}
 <details><summary>需求与供给依据</summary><p class="muted">需求归属日 ${esc(p.snapshot.inputs.day)} · 覆盖至 ${esc(p.snapshot.inputs.horizon)}${p.snapshot.inputs.missing_days.length?' · 缺日 '+p.snapshot.inputs.missing_days.map(esc).join('、'):''}</p><div class="table-wrap"><table><thead><tr><th>需求日期</th><th>数量</th><th>来源凭证</th><th>质量</th></tr></thead><tbody>${p.snapshot.inputs.demand_days.map(x=>`<tr><td>${esc(x.day)}</td><td>${x.quantity}</td><td>${esc(x.source_reference)}</td><td>${m5Label(x.quality)}</td></tr>`).join('')}</tbody></table></div><div class="table-wrap"><table><thead><tr><th>供给阶段 / 编号</th><th>数量</th><th>预计到达</th><th>计算口径</th></tr></thead><tbody>${p.snapshot.inputs.supplies.map(x=>`<tr><td>${m5Label(x.phase)}<br><small>${esc(x.id)}</small></td><td>${x.quantity}</td><td>${date(x.expected_at)}</td><td>${x.eligible?'计入及时供给':m5Label(x.risk)}</td></tr>`).join('')||'<tr><td colspan="4">无未来供给</td></tr>'}</tbody></table></div></details></article>`}).join('')||'<p class="muted">计算后会保存一份不可覆盖的方案和依据。</p>'}${m5Pager('plans')}</section>
 <section class="card form-card"><h2>需求历史</h2><div class="table-wrap"><table><thead><tr><th>日期</th><th>仓库 / 商品</th><th>数量</th><th>质量 / 来源</th></tr></thead><tbody>${m5.demand.items.map(x=>`<tr><td>${esc(x.day)}</td><td>${esc(wh(x.warehouse_id).name)}<br>${esc(sku(x.sku_id).name)}</td><td>${x.quantity}</td><td>${m5Label(x.quality)}<br>${esc(x.source_reference)}</td></tr>`).join('')}</tbody></table></div>${m5Pager('demand')}</section>`;
}
function m5New(kind){
 if(kind==='purchase'&&!m1.suppliers.items.some(x=>x.active)){toast('请先建立供应商并关联供货商品');return}
 phaseModal('新建'+m5Label(kind),phaseInput('code','计划编号')+`<label>${kind==='purchase'?'收货仓库':'调出仓库'}<select name="warehouse_id">${warehouseOptions(false,true)}</select></label>`+(kind==='purchase'?`<label>供应商<select name="supplier_id">${m5Vendors()}</select></label><label>补发关联退货单（普通采购留空）<select name="replacement_return_id"><option value="">普通采购</option>${third.returns.filter(r=>r.kind==='supplier'&&r.status==='completed').map(r=>`<option value="${esc(r.id)}">${esc(r.reference)} · ${esc(sku(r.sku_id).name)} · 已退 ${r.quantity}</option>`).join('')}</select></label>${m4Time('ordered_at','实际下单时间')}${m4Time('promised_at','原始承诺到货时间')}`:`<label>调入仓库<select name="destination_id">${warehouseOptions(false,true)}</select></label>${m4Time('expected_at','计划到达时间')}`)+`<div id="m5-lines"></div><button type="button" id="m5-add-line" class="secondary-button">＋ 商品行</button>`+m3Reason,async form=>{
  const d=Object.fromEntries(new FormData(form)),body={code:d.code,warehouse_id:d.warehouse_id,lines:[...form.querySelectorAll('[data-m5-line]')].map(el=>({sku_id:el.querySelector('select').value,quantity:Number(el.querySelector('input').value)}))};
  if(kind==='purchase')Object.assign(body,{supplier_id:d.supplier_id,replacement_return_id:d.replacement_return_id||null,ordered_at:m4Timestamp(d.ordered_at),promised_at:m4Timestamp(d.promised_at),note:d.reason});else Object.assign(body,{destination_id:d.destination_id,expected_at:m4Timestamp(d.expected_at),reason:d.reason});
  await workflowWrite('/v2/'+(kind==='purchase'?'purchase-plans':'transfer-plans'),body);
 });
 const add=()=>{const row=document.createElement('fieldset');row.dataset.m5Line='true';row.innerHTML=`<label>商品${phaseSkuSelect()}</label>${m4Number('quantity','计划数量')}<button class="text-button" type="button">移除此行</button>`;row.querySelector('button').onclick=()=>row.remove();$('#m5-lines').append(row)};$('#m5-add-line').onclick=add;add();
}
function m5OrderDialog(button){
 const r=m5Orders().find(x=>x.id===button.dataset.id),action=button.dataset.m5,line=r.lines.find(x=>x.id===button.dataset.line);
 let extra='';
 if(action==='promise')extra=m4Time('promised_at','最新承诺到货时间');
 if(action==='batch')extra=m4Number('quantity','本批数量',line.unbatched)+phaseInput('carrier','承运方（未知可留空）')+phaseInput('transport_mode','运输方式（未知可留空）')+phaseInput('tracking_number','运单号（未知可留空）')+m4Time('planned_ship_at','预计发出时间')+m4Time('expected_at','预计到达时间');
 phaseModal(({promise:'变更承诺交期',batch:r.kind==='transfer'?'分批预占':'计划到货批次',close:'关闭未分批余量'})[action],phaseInput('reference',action==='batch'?'批次凭证号':'本次操作凭证号')+extra+m3Reason,async f=>{
  const d=Object.fromEntries(new FormData(f)),body={expected_version:r.version,reference:d.reference,reason:d.reason};let path=`/v2/supply-orders/${r.kind}/${r.id}/`;
  if(action==='promise'){body.promised_at=m4Timestamp(d.promised_at);path=`/v2/purchase-plans/${r.id}/promise`}
  else if(action==='close')path+='close';
  else{Object.assign(body,{supply_line_id:line.id,quantity:Number(d.quantity),carrier:d.carrier||null,transport_mode:d.transport_mode||null,tracking_number:d.tracking_number||null,planned_ship_at:m4Timestamp(d.planned_ship_at),expected_at:m4Timestamp(d.expected_at),expected_balance_version:r.kind==='transfer'?stockVersion(r.warehouse_id,line.sku_id):null});path+='batches'}
  await workflowWrite(path,body);
 });if(action==='batch')for(const name of ['carrier','transport_mode','tracking_number'])$(`#editor-form [name=${name}]`).required=false;
}
function m5BatchDialog(id,action){
 const all=m5Orders().flatMap(x=>x.lines.flatMap(l=>l.batches));const b=all.find(x=>x.id===id);
 let extra=action==='ship'?m4Time('occurred_at','实际发出时间'):action==='receive'?`<div class="quantities">${['g','q','d'].map((k,i)=>m3Qty(k,['良品','待验','残次'][i],b.in_transit,0)).join('')}</div>`+m4Time('occurred_at','实际签收时间'):action==='claim'?m4Number('quantity','本批差异量',b.in_transit)+`<label>差异处置类型<select name="kind"><option value="shortage">短少核销</option><option value="refused">拒收后确认核销在途</option></select></label><p class="muted">仍在退运或等待核实的货物不要核销；已到破损品按残次签收。</p>`+m3DetailsFields():'';
 phaseModal(({ship:'登记本批发出',receive:'登记本批签收',cancel:'取消未发批次',claim:'申报本批差异'})[action],phaseInput('reference','本次业务凭证号')+extra+m3Reason,async f=>{
  const d=Object.fromEntries(new FormData(f)),body={expected_version:b.version,reference:d.reference,reason:d.reason};
  if(['ship','receive'].includes(action))body.occurred_at=m4Timestamp(d.occurred_at);
  if(action==='receive'){for(const k of ['g','q','d'])body[k]=Number(d[k]);body.expected_balance_version=stockVersion(b.warehouse_id,b.sku_id)}
  else if(action==='claim'){body.quantity=Number(d.quantity);body.kind=d.kind;body.details=m3Details(f)}
  else body.expected_balance_version=b.kind==='transfer'?stockVersion(b.source_warehouse_id,b.sku_id):null;
  await workflowWrite(`/v2/supply-batches/${id}/${action==='claim'?'discrepancy':action}`,body);
 });
}
function m5Inspect(id){const r=m5Orders().flatMap(x=>x.lines.flatMap(l=>l.batches.flatMap(b=>b.receipts))).find(x=>x.id===id);phaseModal('开始到货检验',phaseInput('reference','本次检验凭证号')+m4Number('quantity','本批检验量',r.uninspected)+m3DetailsFields(),f=>workflowWrite(`/v2/supply-receipts/${id}/inspect`,{reference:f.elements.reference.value,quantity:Number(f.elements.quantity.value),expected_balance_version:stockVersion(r.warehouse_id,r.sku_id),details:m3Details(f)}));}
function m5Review(id){const claim=m5Orders().flatMap(x=>x.lines.flatMap(l=>l.batches.flatMap(b=>b.claims))).find(x=>x.id===id);phaseModal('独立复核运输差异',`<p>本批核销 ${claim.quantity} · ${esc(claim.details.opinion)}</p><label>决定<select name="decision"><option value="approve">批准核销</option><option value="reject">驳回重查</option></select></label>`+phaseInput('reference','复核凭证号')+m3Reason,f=>workflowWrite(`/v2/supply-discrepancies/${id}/review`,{expected_version:claim.version,reference:f.elements.reference.value,reason:f.elements.reason.value,decision:f.elements.decision.value}));}
function m5Policy(id){
 const p=m5.policies.items.find(x=>x.id===id);
 phaseModal(p?'编辑补货策略':'新增补货策略',`<label>仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>商品${phaseSkuSelect()}</label><label>供应商<select name="supplier_id">${m5Vendors()}</select></label>`+
 [['window_days','需求窗口（天）',90,3,1],['coverage_days','覆盖周期（天）',90,5,1],['lead_days','供应商提前期（天）',365,2,0],['safety','目标安全量',1000000,0,0],['minimum','最小采购量',1000000,1,1],['pack','包装倍数',1000000,1,1],['max_daily','单日需求核实阈值',1000000,1000000,1]].map(([k,l,max,value,min])=>m4Number(k,l,max,p?.[k]??value,min)).join('')+
 `<label>预测需求口径<select name="includes_open_orders"><option value="true">已包含现有未履约订单</option><option value="false">仅现有订单以外的新增需求</option></select></label><label>状态<select name="active"><option value="true">启用</option><option value="false">停用</option></select></label>`,f=>{const d=Object.fromEntries(new FormData(f)),body={warehouse_id:p?.warehouse_id||d.warehouse_id,sku_id:p?.sku_id||d.sku_id,supplier_id:d.supplier_id,expected_version:p?.version||0,includes_open_orders:d.includes_open_orders==='true',active:d.active==='true'};for(const k of ['window_days','coverage_days','lead_days','safety','minimum','pack','max_daily'])body[k]=Number(d[k]);return workflowWrite('/v2/planning/policies',body)});
 if(p){for(const k of ['warehouse_id','sku_id','supplier_id'])$(`#editor-form [name=${k}]`).value=p[k];for(const k of ['warehouse_id','sku_id'])$(`#editor-form [name=${k}]`).disabled=true;for(const k of ['includes_open_orders','active'])$(`#editor-form [name=${k}]`).value=String(!!p[k])}
}
function m5Demand(){phaseModal('登记日需求',`<label>仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>商品${phaseSkuSelect()}</label><label>需求日期<input name="day" type="date" required></label>`+m3Qty('quantity','当日需求量',1000000,0)+`<label>数据质量<select name="quality"><option value="known">有依据，已确认</option><option value="unknown">待核实</option><option value="abnormal">异常</option></select></label>`+phaseInput('source_reference','模拟来源凭证')+'<p class="muted">仅登记已结束业务日。零需求必须有依据；缺日保持未知，不能补零。</p>',f=>{const d=Object.fromEntries(new FormData(f));return workflowWrite('/v2/planning/demand',{...d,quantity:Number(d.quantity)})});}
function m5Confirm(id){const p=m5.plans.items.find(x=>x.id===id);phaseModal('核对并确认采购',`<p>建议 ${p.suggested} ${esc(sku(p.sku_id).unit)}，预计到达 ${date(p.snapshot.expected_arrival)}。确认时重新核对全部输入；填 0 表示本次不采购。</p>`+m3Qty('quantity','确认采购量',1000000,p.suggested)+m3Reason,f=>workflowWrite(`/v2/planning/plans/${id}/confirm`,{quantity:Number(f.elements.quantity.value),reason:f.elements.reason.value}));}
function m5Bind(){document.querySelectorAll('[data-m5]').forEach(button=>button.onclick=async()=>{const action=button.dataset.m5,id=button.dataset.id;try{
 if(action==='tab'){m5.kind=id;render();return}if(action==='new'){m5New(id);return}
 if(['promise','batch','close'].includes(action)){m5OrderDialog(button);return}if(['ship','receive','cancel','claim'].includes(action)){m5BatchDialog(id,action);return}
 if(action==='inspect'){m5Inspect(id);return}if(action==='review'){m5Review(id);return}
 if(action.startsWith('policy-')){m5Policy(id);return}if(action==='demand'){m5Demand();return}if(action==='confirm-plan'){m5Confirm(id);return}
 button.disabled=true;
 if(action==='page'){await m5PageLoad(id,Number(button.dataset.offset));render();return}
 if(action==='calculate')await workflowWrite(`/v2/planning/policies/${id}/calculate`,{});
 if(action==='scan')await workflowWrite('/v2/purchase-plans/scan-delays',{});
 await load();render();
 }catch(e){toast(e.message)}finally{button.disabled=false}})}
