'use strict';
const m4Empty=()=>({items:[],offset:0,limit:20,has_more:false});
let m4={orders:m4Empty(),connections:m4Empty(),inbox:m4Empty(),outbox:m4Empty(),snapshots:m4Empty(),reconciliations:m4Empty(),error:''};
const m4Labels={active:'已启用',disabled:'已停用',pending_confirmation:'待确认',pending_allocation:'待分配',reserved:'已预占',processing:'处理中',partially_shipped:'部分发出',shipped:'已发出',completed:'已完成',cancelled:'已取消',waiting:'等待前序',queued:'待开始',started:'进行中',received:'已接收',processed:'已处理',duplicate:'重复已合并',stale:'旧版本已忽略',conflict:'冲突待核实',failed:'处理失败',unknown:'待核实',pending:'待发送',retry:'等待重试',leased:'处理中',succeeded:'已确认成功',dead:'死信',manual:'待人工接管',matched:'一致',different:'有差异',pick:'拣货',review:'复核',confirm:'确认订单',allocate:'分配库存',cancel:'取消未发数量',ship:'确认实物交接',start:'开始',complete:'完成',link:'关联供给',release:'解除供给关联',source:'导入模拟订单',direct:'新建订单'};
const m4Label=x=>m4Labels[x]||x;
const m4Read=()=>['admin','warehouse','operations','analyst'].includes(state.user?.role);
const m4Write=()=>['admin','warehouse','operations'].includes(state.user?.role);
const m4Physical=()=>['admin','warehouse'].includes(state.user?.role);
const m4Account=id=>m4.connections.items.find(a=>a.id===id)?.name||id;
function m4Reset(){for(const k of ['orders','connections','inbox','outbox','snapshots','reconciliations'])m4[k]=m4Empty();m4.error=''}
async function m4PageLoad(kind,offset=0){m4[kind]=await api(`/v2/${['orders','connections'].includes(kind)?kind:'connector-records/'+kind}?limit=20&offset=${offset}`)}
async function m4Load(){if(!m4Read()){m4Reset();return}m4.error='';try{await Promise.all(['orders','connections','inbox','outbox','snapshots','reconciliations'].map(k=>m4PageLoad(k)))}catch(e){m4.error=e.message}}
const m4Pill=s=>`<span class="pill ${['unknown','conflict','failed','dead','manual','different'].includes(s)?'warn':''}">${esc(m4Label(s))}</span>`;
const m4Button=(action,id,label,extra='')=>`<button class="secondary-button" data-m4="${action}" data-id="${esc(id)}" ${extra}>${esc(label||m4Label(action))}</button>`;
const m4Error=()=>m4.error?`<div class="notice">${esc(m4.error)} ${m4Button('refresh','','重新读取')}</div>`:'';
function m4Pager(kind){const p=m4[kind];return `<div class="table-foot"><span>共 ${p.total||0} 条 · 本页 ${p.items.length} 条</span><div>${m4Button('page',kind,'上一页',`data-offset="${Math.max(0,p.offset-p.limit)}" ${p.offset?'':'disabled'}`)} ${m4Button('page',kind,'下一页',`data-offset="${p.offset+p.limit}" ${p.has_more?'':'disabled'}`)}</div></div>`}
const m4Number=(name,label,max=1000000,value=1,min=1)=>`<label>${label}<input name="${name}" type="number" min="${min}" max="${max}" step="1" required value="${value}"></label>`;
const m4Time=(name,label)=>`<label>${label}<input name="${name}" type="datetime-local"><small class="field-hint">有依据时填写，留空保留未知。</small></label>`;
const m4Reason='<label>操作说明<textarea name="reason" minlength="2" maxlength="1000" required></textarea></label>';
const m4Timestamp=v=>v?new Date(v).toISOString():null;
const m4Options=()=>m4.connections.items.filter(a=>a.active).map(a=>`<option value="${esc(a.id)}">${esc(a.name)} · 模拟</option>`).join('');
function m4OrdersPage(){
 if(!m4Read())return heading('订单履约','当前岗位无订单履约权限。');
 return heading('订单履约','逐行分配、拣货复核与分批交接；取消只释放未发数量。',m4Write()?`<div class="action-buttons">${state.user.role==='admin'?m4Button('source','','导入模拟订单'):''}${m4Button('direct','','＋ 新建订单')}</div>`:'')+m4Error()+
 m4.orders.items.map(r=>`<section class="card form-card"><div class="card-head"><div><h2>${esc(r.source_order_id)} ${m4Pill(r.status)}</h2><p class="muted">${esc(m4Account(r.connection_id))} · 订单版本 ${r.version}<br>固定仓库顺序：${r.warehouse_ids.map(w=>esc(wh(w).name)).join(' → ')}</p></div><div class="action-buttons">${m4Write()&&r.status==='pending_confirmation'?m4Button('confirm',r.id):''}${state.user.role==='admin'&&r.status==='shipped'?m4Button('channel-complete',r.id,'登记渠道完成回执'):''}</div></div>
 <div class="table-wrap"><table><thead><tr><th>商品 / 来源行</th><th>订购</th><th>占用</th><th>已发</th><th>取消</th><th>待分配</th><th>操作</th></tr></thead><tbody>${r.lines.map(l=>`<tr><td>${productCell(l.sku_id)}<small>${esc(l.source_line_id)}</small></td><td>${l.quantity}</td><td>${l.reserved}</td><td>${l.shipped}</td><td>${l.cancelled}</td><td>${l.quantity-l.reserved-l.shipped-l.cancelled}</td><td><div class="action-buttons">${m4Write()&&r.status!=='pending_confirmation'&&l.quantity>l.reserved+l.shipped+l.cancelled?m4Button('allocate',r.id,null,`data-line="${l.id}"`):''}${m4Write()&&l.quantity>l.shipped+l.cancelled?m4Button('cancel',r.id,null,`data-line="${l.id}"`)+m4Button('link',r.id,null,`data-line="${l.id}"`):''}</div></td></tr>`).join('')}</tbody></table></div>
 <h3>履约批次</h3>${r.batches.map(b=>{const tasks=r.tasks.filter(t=>t.batch_id===b.id);const review=tasks.find(t=>t.kind==='review');return `<article class="m4-batch"><div><strong>${esc(wh(b.warehouse_id).name)} · ${esc(sku(b.sku_id).name)}</strong><p>预占 ${b.reserved} · 已发 ${b.shipped} · 已取消 ${b.cancelled}</p><small class="muted">批次 ${esc(b.id)}</small></div><div class="action-buttons">${tasks.map(t=>`<span>${m4Label(t.kind)} ${m4Pill(t.status)}</span>${m4Physical()&&b.reserved&&['queued','started'].includes(t.status)?m4Button(t.status==='queued'?'start':'complete',r.id,(t.status==='queued'?'开始':'完成')+m4Label(t.kind),`data-batch="${b.id}" data-kind="${t.kind}"`):''}`).join('')}${m4Physical()&&b.reserved&&review?.status==='completed'?m4Button('ship',r.id,null,`data-batch="${b.id}"`):''}</div></article>`}).join('')||'<p class="muted">尚未分配履约批次。</p>'}
 ${r.shipments.length?`<h3>实物交接与签收</h3>${r.shipments.map(x=>`<p>${esc(x.reference)} · ${x.quantity} ${esc(sku(x.sku_id).unit)} · 交接 ${date(x.occurred_at)} · 签收 ${date(x.delivered_at)}<br><small class="muted">退货关联单据 ${esc(x.document_id)}</small> ${state.user.role==='admin'&&!x.delivered_at?m4Button('delivered',r.id,'登记运输签收回执',`data-shipment="${x.id}"`):''}</p>`).join('')}`:''}
 <p class="muted">渠道完成：${date(r.channel_completed_at)}（与实物交接、运输签收分别记录）</p>
 ${r.supply_links.length?`<details><summary>供给依赖（${r.supply_links.length}）</summary>${r.supply_links.map(l=>`<p>${esc(l.source_type)} · ${esc(l.source_id)} · 当前关联 ${l.quantity-l.released} / ${l.quantity}${m4Write()&&l.quantity>l.released?m4Button('release',r.id,null,`data-link="${l.id}"`):''}</p>`).join('')}</details>`:''}
 <details class="m3-history"><summary>过程与证据（${r.history.length}）</summary>${r.history.map(h=>`<p>${date(h.created_at)} · ${esc(h.action)} · ${esc(h.payload.reason||'')}<br><small>${esc(h.event_id)}</small></p>`).join('')}</details></section>`).join('')+
 (!m4.orders.items.length?'<section class="card form-card"><p>暂无订单。先在「模拟协同与对账」建立连接，再新建或导入模拟订单。</p></section>':'')+m4Pager('orders');
}
function m4CollaborationPage(){
 if(!m4Read())return heading('模拟协同与对账','当前岗位无协同记录权限。');
 const admin=state.user.role==='admin';
 return heading('模拟协同与对账','核实消息、外发结果与库存观察值，每次处理保留证据。',`<div class="action-buttons">${m4Button('refresh','','刷新记录')}${admin?m4Button('connection-new','','＋ 模拟连接')+m4Button('snapshot-new','','录入模拟快照'):''}</div>`)+m4Error()+
 '<div class="notice neutral"><p>当前使用本地模拟渠道。快照只用于比较，差异通过异常任务核实；此页操作不会连接真实平台。</p></div>'+
 `<section class="card form-card"><h2>连接账号</h2>${m4.connections.items.map(a=>`<div class="connection-row m4-connection"><div><strong>${esc(a.name)}</strong><p>${esc(a.source_account)} · ${a.warehouse_ids.map(w=>esc(wh(w).name)).join('、')}</p><small class="muted">模拟查询 ${a.supports_query?'支持':'不支持'} · 幂等执行 ${a.supports_idempotency?'支持':'不支持'}</small></div>${m4Pill(a.active?'active':'disabled')}${admin?m4Button('connection-edit',a.id,'成员与启停'):''}</div>`).join('')||'<p class="muted">尚未建立模拟连接。</p>'}${m4Pager('connections')}</section>`+
 `<section class="card form-card"><h2>接收消息</h2><div class="table-wrap"><table><thead><tr><th>消息 / 类型</th><th>来源版本</th><th>状态</th><th>处理</th></tr></thead><tbody>${m4.inbox.items.map(x=>`<tr><td>${esc(x.source_event_id)}<br><small>${esc(x.kind)} · ${esc(x.entity_id)}</small></td><td>${x.source_version??'未知'}</td><td>${m4Pill(x.status)}<small>${esc(x.error||'')}</small></td><td>${admin&&['received','unknown','failed'].includes(x.status)?m4Button('inbox-process',x.id,'处理')+m4Button('inbox-verify',x.id,'查询来源核实'):''}<details><summary>接收证据</summary><pre class="m4-evidence">${esc(JSON.stringify({received_at:x.received_at,occurred_at:x.occurred_at,result:x.result,conflicts:x.conflicts},null,2))}</pre></details></td></tr>`).join('')||'<tr><td colspan="4" class="empty">暂无接收消息</td></tr>'}</tbody></table></div>${m4Pager('inbox')}</section>`+
 `<section class="card form-card"><div class="card-head"><h2>可靠外发</h2>${admin?m4Button('dispatch','','处理下一项'):''}</div><div class="table-wrap"><table><thead><tr><th>原业务编号</th><th>状态 / 尝试</th><th>操作与证据</th></tr></thead><tbody>${m4.outbox.items.map(x=>`<tr><td>${esc(x.id)}<br><small>资源内顺序 ${x.sequence}</small></td><td>${m4Pill(x.status)} · ${x.attempts} 次<br><small>${esc(x.last_error||'')}</small></td><td>${admin&&['manual','dead'].includes(x.status)?m4Button('retry',x.id,'人工重试原动作'):''}<details><summary>投递历史（${x.history.length}）</summary>${x.history.map(h=>`<p>${date(h.created_at)} · ${esc(h.action)}<br><small>${esc(JSON.stringify(h.payload))}</small></p>`).join('')}</details></td></tr>`).join('')||'<tr><td colspan="3" class="empty">实物交接后生成模拟渠道通知</td></tr>'}</tbody></table></div>${m4Pager('outbox')}</section>`+
 `<section class="card form-card"><h2>来源快照</h2>${m4.snapshots.items.map(x=>`<div class="m4-batch"><div><strong>${esc(x.snapshot_id)}</strong><p>${esc(wh(x.warehouse_id).name)} · ${esc(sku(x.sku_id).name)} · 良/待/残 ${x.quantities.g}/${x.quantities.q}/${x.quantities.d}</p><small>来源时间 ${date(x.snapshot_at)} · 水位 ${x.covered_watermark??'未知'}</small></div>${m4Write()?m4Button('compare',x.id,'执行对账'):''}</div>`).join('')||'<p class="muted">暂无来源快照。</p>'}${m4Pager('snapshots')}</section>`+
 `<section class="card form-card"><h2>对账报告</h2>${m4.reconciliations.items.map(x=>`<article class="m4-batch"><div><strong>${esc(wh(x.warehouse_id).name)} · ${esc(sku(x.sku_id).name)}</strong> ${m4Pill(x.status)}<p>本地良/待/残 ${['g','q','d'].map(k=>x.inputs.local[k]).join('/')} · 外部观察 ${['g','q','d'].map(k=>x.inputs.external_raw[k]).join('/')}<br>差异 ${x.difference?['g','q','d'].map(k=>x.difference[k]).join('/'):'未知，需核实覆盖范围'}</p><p class="muted">${date(x.created_at)} · ${esc(x.rule_version)}</p><details><summary>比较输入与水位证据</summary><pre class="m4-evidence">${esc(JSON.stringify(x.inputs,null,2))}</pre></details></div>${x.status!=='matched'?'<button class="text-button" data-view="exceptions">查看异常任务 →</button>':''}</article>`).join('')||'<p class="muted">暂无对账记录。</p>'}${m4Pager('reconciliations')}</section>`;
}
function m4OrderNew(simulated){
 if(!m4.connections.items.some(a=>a.active)){toast('请先建立模拟连接并分配账号成员');return}
 phaseModal(simulated?'导入模拟订单':'新建销售订单',`<label>来源账号<select name="connection_id">${m4Options()}</select></label>${phaseInput('source_order_id','来源订单号')}<fieldset id="m4-warehouses"></fieldset>${m4Time('planned_at','计划交接时间')}${m4Time('occurred_at','订单实际发生时间')}<div id="m4-lines"></div><button class="secondary-button" type="button" id="m4-add-line">＋ 添加商品行</button>`,async form=>{
  const data=new FormData(form);const body={connection_id:data.get('connection_id'),source_order_id:data.get('source_order_id'),warehouse_ids:data.getAll('warehouse_ids'),planned_at:m4Timestamp(data.get('planned_at')),occurred_at:m4Timestamp(data.get('occurred_at')),lines:[...form.querySelectorAll('[data-order-line]')].map(el=>({source_line_id:el.querySelector('[name=source_line_id]').value,sku_id:el.querySelector('[name=sku_id]').value,quantity:Number(el.querySelector('[name=quantity]').value)}))};
  if(simulated){const envelope={run_id:m1.runtime.run_id,tenant_id:m1.runtime.tenant_id||state.meta.tenant_id,source_event_id:crypto.randomUUID(),business_action_id:crypto.randomUUID(),entity_id:body.source_order_id,source_version:1,kind:'order.created',occurred_at:body.occurred_at,payload:body};await workflowWrite(`/v2/connections/${body.connection_id}/events`,envelope)}else await workflowWrite('/v2/orders',body);
 });
 const warehouses=()=>{const a=m4.connections.items.find(x=>x.id===$('#editor-form [name=connection_id]').value);$('#m4-warehouses').innerHTML='<legend>固定仓库顺序（按下列顺序分配）</legend>'+a.warehouse_ids.filter(w=>state.meta.warehouses.some(x=>x.id===w)).map(w=>`<label class="check-line"><input type="checkbox" name="warehouse_ids" value="${esc(w)}" checked>${esc(wh(w).name)}</label>`).join('')};warehouses();$('#editor-form [name=connection_id]').onchange=warehouses;
 let next=0;const add=()=>{next++;const row=document.createElement('fieldset');row.dataset.orderLine='true';row.innerHTML=`<legend>商品行 ${next}</legend>${phaseInput('source_line_id','来源行号',String(next))}<label>商品${phaseSkuSelect()}</label>${m4Number('quantity','订购数量')}<button type="button" class="text-button">删除此行</button>`;row.querySelector('button').onclick=()=>row.remove();$('#m4-lines').append(row)};$('#m4-add-line').onclick=add;add();
}
function m4OrderAction(button){
 const r=m4.orders.items.find(x=>x.id===button.dataset.id),action=button.dataset.m4,line=r.lines.find(x=>x.id===button.dataset.line),batch=r.batches.find(x=>x.id===button.dataset.batch),link=r.supply_links.find(x=>x.id===button.dataset.link);
 let fields=m4Reason;
 if(action==='allocate')fields=`<label>分配方式<select name="warehouse_id"><option value="">按订单固定仓库顺序</option>${r.warehouse_ids.map(w=>`<option value="${esc(w)}">${esc(wh(w).name)}</option>`).join('')}</select></label>${m4Number('quantity','分配数量',line.quantity-line.reserved-line.shipped-line.cancelled)}<label>渠道额度编号（可选）<input name="grant_id" maxlength="120"><small>使用渠道额度时须指定匹配仓库，留空使用本地未分配库存。</small></label>`+fields;
 if(action==='cancel')fields=m4Number('quantity','取消未发数量',line.quantity-line.shipped-line.cancelled)+fields;
 if(action==='ship')fields=phaseInput('reference','本次交接凭证号')+m4Number('quantity','实际交接数量',batch.reserved)+m4Time('occurred_at','实际交接时间')+fields;
 if(action==='link')fields=`<label>供给类型<select name="source_type"><option value="purchase_line">采购行</option><option value="transfer_line">调拨行</option><option value="inspection">检验批次</option></select></label>${phaseInput('source_id','供给行 / 批次编号')}${m4Number('quantity','关联数量',line.quantity-line.shipped-line.cancelled)}`+fields;
 if(action==='release')fields=m4Number('quantity','解除数量',link.quantity-link.released)+fields;
 phaseModal(m4Label(action)+' · '+r.source_order_id,fields,async form=>{const raw=Object.fromEntries(new FormData(form));const body={expected_version:r.version,action_id:crypto.randomUUID(),reason:raw.reason};
  if(line)body.line_id=line.id;if(batch)body.batch_id=batch.id;if(link)body.link_id=link.id;if(raw.quantity)body.quantity=Number(raw.quantity);
  if(action==='allocate'){body.warehouse_id=raw.warehouse_id||null;body.grant_id=raw.grant_id||null}
  if(action==='ship'){body.reference=raw.reference;body.occurred_at=m4Timestamp(raw.occurred_at)}
  if(['start','complete'].includes(action))body.kind=button.dataset.kind;
  if(action==='link'){body.source_type=raw.source_type;body.source_id=raw.source_id}
  await workflowWrite(`/v2/orders/${r.id}/${action}`,body);
 });
}
function m4ConnectionEditor(identity){
 const existing=m4.connections.items.find(x=>x.id===identity);const users=third.users.filter(u=>u.active&&['admin','warehouse','operations','analyst'].includes(u.role));
 const scopeFields=`<fieldset><legend>授权成员（还须满足成员自身仓库权限）</legend>${users.map(u=>`<label class="check-line"><input type="checkbox" name="member_ids" value="${esc(u.id)}" ${existing?.member_ids.includes(u.id)?'checked':''}>${esc(u.name)} · ${esc(u.username)}</label>`).join('')}</fieldset>`;
 phaseModal(existing?'模拟连接成员与状态':'建立模拟连接',existing?`<p>${esc(existing.name)}</p><label>连接状态<select name="active"><option value="true">启用</option><option value="false">停用</option></select></label>`+scopeFields:phaseInput('source_account','来源账号编码')+phaseInput('name','连接显示名称')+`<fieldset><legend>仓库授权</legend>${state.meta.warehouses.filter(w=>w.authority==='local').map(w=>`<label class="check-line"><input type="checkbox" name="warehouse_ids" value="${esc(w.id)}">${esc(w.name)}</label>`).join('')}</fieldset>`+scopeFields,async form=>{
  const data=new FormData(form);const body=existing?{expected_version:existing.version,active:data.get('active')==='true',member_ids:data.getAll('member_ids')}:{source_account:data.get('source_account'),name:data.get('name'),warehouse_ids:data.getAll('warehouse_ids'),member_ids:data.getAll('member_ids')};await workflowWrite('/v2/connections'+(existing?'/'+existing.id:''),body);
 });if(existing)$('#editor-form [name=active]').value=String(!!existing.active);
}
function m4Snapshot(){
 phaseModal('录入模拟库存快照',`<label>来源账号<select name="connection_id">${m4Options()}</select></label>${phaseInput('snapshot_id','来源快照号')}<label>仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>商品${phaseSkuSelect()}</label><div class="quantities">${['g','q','d'].map((k,i)=>m4Number(k,['良品','待验','残次'][i],1000000,0,0)).join('')}</div><label>可靠覆盖水位（可选）<input name="covered_watermark" type="number" min="0" step="1"><small>来源明确证明覆盖到的增量序号；没有依据请留空。</small></label>${m4Time('snapshot_at','来源快照时间')}`,async form=>{const d=Object.fromEntries(new FormData(form));await workflowWrite(`/v2/connections/${d.connection_id}/snapshots`,{snapshot_id:d.snapshot_id,warehouse_id:d.warehouse_id,sku_id:d.sku_id,covered_watermark:d.covered_watermark===''?null:Number(d.covered_watermark),snapshot_at:m4Timestamp(d.snapshot_at),quantities:Object.fromEntries(['g','q','d'].map(k=>[k,Number(d[k])]))})});
}
function m4Receipt(button){
 const r=m4.orders.items.find(x=>x.id===button.dataset.id),delivered=button.dataset.m4==='delivered';
 phaseModal(delivered?'登记运输签收回执':'登记渠道完成回执',phaseInput('reference','来源回执编号')+m4Time('occurred_at','实际发生时间'),async form=>{const data=Object.fromEntries(new FormData(form));await workflowWrite(`/v2/connections/${r.connection_id}/events`,{run_id:m1.runtime.run_id,tenant_id:state.meta.tenant_id,source_event_id:data.reference,business_action_id:data.reference,entity_id:delivered?button.dataset.shipment:r.source_order_id,source_version:delivered?1:(r.source_version||0)+1,kind:delivered?'shipment.delivered':'order.completed',occurred_at:m4Timestamp(data.occurred_at),payload:{}})});
}
function m4Bind(){document.querySelectorAll('[data-m4]').forEach(button=>button.onclick=async()=>{const action=button.dataset.m4,id=button.dataset.id;try{
 if(['direct','source'].includes(action)){m4OrderNew(action==='source');return}
 if(['confirm','allocate','cancel','ship','start','complete','link','release'].includes(action)){m4OrderAction(button);return}
 if(['connection-new','connection-edit'].includes(action)){m4ConnectionEditor(id);return}
 if(action==='snapshot-new'){m4Snapshot();return}
 if(['channel-complete','delivered'].includes(action)){m4Receipt(button);return}
 if(action==='retry'){phaseModal('重试原业务动作',m4Reason,async form=>workflowWrite(`/v2/outbox/${id}/retry`,{reason:new FormData(form).get('reason')}));return}
 button.disabled=true;
 if(action==='page'){await m4PageLoad(id,Number(button.dataset.offset));render();return}
 if(action==='compare')await workflowWrite(`/v2/snapshots/${id}/compare`,{});
 if(action==='dispatch')await workflowWrite('/v2/outbox/dispatch',{});
 if(action.startsWith('inbox-'))await workflowWrite(`/v2/inbox/${id}/${action.slice(6)}`,{});
 await load();render();
 }catch(e){toast(e.message)}finally{button.disabled=false}})}
