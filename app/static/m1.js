'use strict';
// M1 pages share the existing authenticated client and never write stock directly.
const m1EmptyPage=()=>({items:[],total:0,offset:0,limit:50});
let m1={runtime:null,skus:m1EmptyPage(),warehouses:m1EmptyPage(),suppliers:m1EmptyPage(),mappings:m1EmptyPage(),assortments:m1EmptyPage(),events:m1EmptyPage(),inventorySku:'',eventWarehouse:'',eventSku:'',eventsLoaded:false,error:'',loading:false};
function m1Reset(){m1={...m1,runtime:null,skus:m1EmptyPage(),warehouses:m1EmptyPage(),suppliers:m1EmptyPage(),mappings:m1EmptyPage(),assortments:m1EmptyPage(),events:m1EmptyPage(),eventsLoaded:false,error:''}}
function m1StorageScope(){return [m1.runtime?.runtime_mode||'legacy',m1.runtime?.run_id||'default',state.user?.id||state.user?.name||'anonymous'].join(':')}
function m1Storage(){return 'catalog-pending:'+m1StorageScope()}
function m1Pending(){try{return JSON.parse(sessionStorage.getItem(m1Storage())||'null')}catch{return null}}
async function m1Write(path,body,method='POST',original=null){
 if(state.busy)throw new Error('操作处理中，请稍候');
 if((m1Pending()&&!original)||workflowPending()||getPending())throw new Error('有一笔操作结果未确认，请先重试原请求。');
 const storage=m1Storage(),req=original||{path,body,method,key:crypto.randomUUID()};
 sessionStorage.setItem(storage,JSON.stringify(req));state.busy=true;
 try{const result=await api(req.path,{method:req.method,headers:{'Idempotency-Key':req.key},body:JSON.stringify(req.body)});sessionStorage.removeItem(storage);return result}
 catch(e){if(!e.unknown&&e.code!=='UNAUTHENTICATED')sessionStorage.removeItem(storage);throw e}
 finally{state.busy=false}
}
function m1Banner(){return (m1.error?`<div class="notice"><p>新版资料读取失败：${esc(m1.error)}。请刷新后再操作。</p><button class="secondary-button" id="m1-refresh">重试读取</button></div>`:'')+(m1Pending()?'<div class="notice"><p>一笔业务操作结果未确认，原始内容和请求编号已保留。</p><button class="secondary-button" id="m1-retry">重试原请求</button></div>':'')}
function quantityByUnit(rows,amount){
 const totals=new Map();
 for(const row of rows){const value=amount(row);if(value==null)continue;const unit=sku(row.sku_id).unit||'未定义单位';totals.set(unit,(totals.get(unit)||0)+Number(value))}
 return [...totals].map(([unit,value])=>`<span class="unit-quantity">${num(value)}<small>${esc(unit)}</small></span>`).join('')||'<span class="muted">暂无已确认数据</span>';
}
async function m1Load(){
 m1.error='';
 try{
  m1.runtime=await api('/v2/runtime');
  const r=m1.runtime,label=({development:'开发',dev:'开发',test:'测试',validation:'验证',regression:'回归',simulation:'模拟',demo:'演示'})[r.runtime_mode]||r.runtime_mode;
  $('#runtime-badge').textContent=`模拟环境 · ${label}`;$('#runtime-mode').textContent=`模拟${label}环境`;$('#runtime-run').textContent=`运行标识 ${r.run_id}`;
  const results=await Promise.allSettled([m1GetCatalog('skus'),m1GetCatalog('warehouses'),state.user.role==='cashier'?Promise.resolve():m1GetCatalog('suppliers'),state.user.role==='cashier'?Promise.resolve():m1GetCatalog('mappings'),m1GetInventory()]);
  const failure=results.find(r=>r.status==='rejected');if(failure)throw failure.reason;
 }catch(e){m1.error=e.message}
}
const m1CatalogPaths={skus:'skus',warehouses:'warehouses',suppliers:'suppliers',mappings:'external-mappings'};
async function m1GetCatalog(kind,offset=0){m1[kind]=await api(`/v2/catalog/${m1CatalogPaths[kind]}?limit=50&offset=${offset}`)}
async function m1GetInventory(offset=0){
 const p=new URLSearchParams({limit:'50',offset:String(offset)});if(state.warehouse)p.set('warehouse_id',state.warehouse);if(m1.inventorySku)p.set('sku_id',m1.inventorySku);
 m1.assortments=await api('/v2/catalog/assortments?'+p);
}
async function m1GetEvents(offset=0){
 const p=new URLSearchParams({limit:'50',offset:String(offset)});if(m1.eventWarehouse)p.set('warehouse_id',m1.eventWarehouse);if(m1.eventSku)p.set('sku_id',m1.eventSku);
 m1.events=await api('/v2/events?'+p);m1.eventsLoaded=true;
}
function m1SkuOptions(all=false){return (all?'<option value="">全部商品</option>':'')+state.meta.skus.map(s=>`<option value="${esc(s.id)}">${esc(s.code)} · ${esc(s.name)} · ${esc(s.unit)}</option>`).join('')}
function m1Pager(kind,page){return `<div class="table-foot"><span>共 ${num(page.total)} 条${page.total?` · 第 ${page.offset+1}—${Math.min(page.offset+page.items.length,page.total)} 条`:''}</span><div class="action-buttons"><button class="text-button" data-m1-page="${kind}" data-offset="${Math.max(0,page.offset-page.limit)}" ${page.offset===0?'disabled':''}>上一页</button><button class="text-button" data-m1-page="${kind}" data-offset="${page.offset+page.limit}" ${page.offset+page.items.length>=page.total?'disabled':''}>下一页</button></div></div>`}
function m1InventoryTable(){return `<div class="table-wrap"><table><thead><tr><th>商品 / 仓库</th><th>经营状态</th><th>库存证据</th><th class="num">良品 / 待验 / 残次</th><th class="num">可分配 / 未承诺</th><th>补货范围</th>${state.user.role==='admin'?'<th>管理</th>':''}</tr></thead><tbody>${m1.assortments.items.map(r=>{
 const known=r.inventory_known===true,legacy=state.stocks.find(b=>b.warehouse_id===r.warehouse_id&&b.sku_id===r.sku_id),active=r.active!==false&&r.sku_active!==false;
 return `<tr><td>${productCell(r.sku_id)}<small class="muted">${esc(wh(r.warehouse_id).name)} · ${esc(sku(r.sku_id).unit)}</small></td><td><span class="pill ${active?'':'gray'}">${active?'经营中':'非经营 / 已停用'}</span></td><td><span class="pill ${known?'':'warn'}">${known?(r.inventory_state==='confirmed_zero'?'已确认零库存':'已确认'):'库存未知'}</span><br><small class="muted">${esc(({local_ledger:'历史期初账本',manual_snapshot:'人工核实快照',trusted_opening:'已确认期初'})[r.known_source]||'尚无可信库存证据')}</small></td><td class="num">${known?`${num(r.g)} / ${num(r.q)} / ${num(r.d)}`:'— / — / —'}</td><td class="num">${known?`${num(r.available)} / ${num(r.unallocated)}`:'未知 / 未知'}${known&&legacy?`<br><small class="muted">物理保留 ${num(legacy.b)}</small>`:''}</td><td>${r.eligible_for_replenishment?'参与规则计算':!active?'非经营，不参与':!known?'未知，待核实':r.replenishment?'等待适用条件':'未启用补货'}</td>${state.user.role==='admin'?`<td><button class="text-button" data-assortment-edit="${esc(r.warehouse_id)}" data-sku="${esc(r.sku_id)}">经营设置</button><button class="text-button" data-stock-confirm="${esc(r.warehouse_id)}" data-sku="${esc(r.sku_id)}">核实库存</button></td>`:''}</tr>`;
 }).join('')||`<tr><td colspan="7" class="empty">此范围尚未建立商品与仓库经营关系。不能据此认定库存为零。</td></tr>`}</tbody></table></div>`}
function m1InventoryPage(){return heading('库存总览','数量按商品基础单位显示；无证据为未知，经过可信记录确认后才显示零库存。',state.user.role==='admin'?'<button class="primary" id="assortment-new">＋ 设置经营范围</button>':'')+`<div class="notice neutral"><p>可分配 = 良品 − 订单占用 − 调拨占用 − 冻结 − 物理保留。未承诺还须扣除渠道额度。物理保留会扣减当前可用；计划安全库存属于补货策略参数。</p></div><section class="card"><div class="toolbar"><select id="m1-inventory-warehouse" aria-label="库存仓库" class="warehouse-select">${warehouseOptions(true)}</select><select id="m1-inventory-sku" aria-label="库存商品">${m1SkuOptions(true)}</select><button class="secondary-button" id="m1-inventory-refresh">查询</button></div>${m1InventoryTable()}${m1Pager('assortments',m1.assortments)}</section>`}
function m1CatalogPage(){
 const admin=state.user.role==='admin',tabs=[['products','商品资料'],['warehouses','仓库档案'],...(['admin','warehouse'].includes(state.user.role)?[['suppliers','供应商'],['mappings','来源映射']]:[])],tab=state.catalogTab;
 const kind=tab==='products'?'skus':tab,page=m1[kind]||m1.skus;
 const actions=admin?`<div class="action-buttons">${tab==='products'?'<button class="primary" id="new-sku">＋ 新建商品</button>':tab==='warehouses'?'<button class="primary" id="new-warehouse">＋ 新建仓库</button>':`<button class="primary" id="m1-create-${tab}">＋ 新建${tab==='suppliers'?'供应商':'来源映射'}</button>`}</div>`:'';
 let head='',rows='';
 if(tab==='products'){head='<th>商品</th><th>基础单位 / 条码</th><th>经营状态</th><th>管理</th>';rows=page.items.map(s=>`<tr><td>${productCell(s.id)}</td><td>${esc(s.unit)} / ${esc(s.barcode)||'—'}</td><td>${s.active===false?'已停用':'启用'}</td><td>${admin?`<button class="text-button" data-sku-toggle="${esc(s.id)}">${s.active===false?'启用':'停用'}</button>`:'只读'}</td></tr>`).join('')}
 else if(tab==='warehouses'){head='<th>仓库</th><th>国家 / 时区</th><th>用途</th><th>数据责任</th><th>管理</th>';rows=page.items.map(w=>`<tr><td>${esc(w.name)}<br><small>${esc(w.code)}</small></td><td>${esc(w.country)} / ${esc(w.timezone)}</td><td>${esc(m1Purpose(w.purpose))}</td><td>${w.authority==='local'?'本系统账本':'人工参考 · 只读'}</td><td>${admin?`<button class="text-button" data-warehouse-edit="${esc(w.id)}">设置用途</button>`:'只读'}</td></tr>`).join('')}
 else if(tab==='suppliers'){head='<th>供应商</th><th>默认交期</th><th>供货商品</th><th>状态</th><th>管理</th>';rows=page.items.map(s=>`<tr><td>${esc(s.name)}<br><small>${esc(s.code)}</small></td><td>${num(s.default_lead_days)} 天</td><td>${(s.sku_ids||[]).map(id=>esc(sku(id).code)).join('、')||'尚未关联'}</td><td>${s.active===false?'停用':'启用'}</td><td>${admin?`<button class="text-button" data-supplier-edit="${esc(s.id)}">编辑</button>`:'只读'}</td></tr>`).join('')}
 else{head='<th>来源主体 / 外部编码</th><th>内部商品</th><th>状态</th><th>管理</th>';rows=page.items.map(m=>`<tr><td>${esc(m.connection_account)}<br><small>${esc(m.source_code)}</small></td><td>${productCell(m.sku_id)}</td><td>${m.active===false?'停用':'启用'}</td><td>${admin?`<button class="text-button" data-mapping-edit="${esc(m.id)}">编辑</button>`:'只读'}</td></tr>`).join('')}
 return heading('基础资料','商品、仓库用途、供应商和来源编码由管理员维护。经营范围在库存总览设置。',actions)+`<section class="card"><div class="card-head"><div class="tabs">${tabs.map(([key,label])=>`<button data-tab="${key}" class="${tab===key?'active':''}">${label}</button>`).join('')}</div></div><div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows||'<tr><td colspan="5" class="empty">暂无资料</td></tr>'}</tbody></table></div>${m1Pager(kind,page)}</section>`;
}
function m1Purpose(value){return ({general:'综合',storage:'存储',fulfillment:'履约',transit:'中转',returns:'退货'})[value]||value||'综合'}
function m1EventsPage(){return heading('业务事件','分别记录业务发生、接收与登记时间；未知业务时间保留为空，尚不提供瓶颈结论。')+`<section class="card"><div class="toolbar"><select id="m1-event-warehouse" class="warehouse-select" aria-label="事件仓库">${warehouseOptions(true)}</select><select id="m1-event-sku" aria-label="事件商品">${m1SkuOptions(true)}</select><button class="secondary-button" id="m1-events-refresh">查询</button></div><div class="table-wrap"><table><thead><tr><th>业务对象 / 事件</th><th>商品 / 仓库</th><th>实际发生时间</th><th>接收 / 登记时间</th><th>数量 / 证据</th></tr></thead><tbody>${m1.events.items.map(e=>`<tr><td>${esc(e.event_type==='inventory.posted'?labelKind(e.payload?.kind||'库存记账'):e.event_type)}<br><small>${esc(e.object_type==='stock_document'?'库存单据':e.object_type)} · ${esc(e.object_id)}</small></td><td>${e.sku_id?esc(sku(e.sku_id).name):'—'}<br><small>${e.warehouse_id?esc(wh(e.warehouse_id).name):'—'}</small></td><td>${e.occurred_at?date(e.occurred_at):'<span class="pill warn">未知，未回填</span>'}<br><small>${esc(({unknown:'缺少实际时间',reported:'人工提供时点',observed:'现场记录',verified:'已核实'})[e.time_quality]||'')}</small></td><td>${date(e.received_at)}<br><small>${date(e.registered_at)}</small></td><td>${e.quantity==null?'—':num(e.quantity)} ${esc(e.unit||'')}<br><small>${esc(e.business_reference||e.source_system||'')}</small></td></tr>`).join('')||`<tr><td colspan="5" class="empty">${m1.eventsLoaded?'当前范围暂无事件。历史登记时间不代表业务实际发生时间。':'点击“查询”读取当前授权范围的业务事件。'}</td></tr>`}</tbody></table></div>${m1Pager('events',m1.events)}<div class="table-foot"><span>时间显示采用浏览器时区；原始时点由服务端保留</span></div></section>`}
function m1Modal(title,fields,write){phaseModal(title,fields,write);$('#editor-form button[type=submit]').textContent='确认保存'}
function m1SupplierEdit(s=null){
 m1Modal(s?'编辑供应商':'新建供应商',`<label>供应商编码<input name="code" maxlength="120" required value="${esc(s?.code||'')}"></label><label>供应商名称<input name="name" maxlength="120" required value="${esc(s?.name||'')}"></label><label>默认交期（天）<input name="default_lead_days" type="number" min="0" max="3650" step="1" required value="${s?.default_lead_days??0}"></label><label>状态<select name="active"><option value="true">启用</option><option value="false" ${s?.active===false?'selected':''}>停用</option></select></label><fieldset><legend>关联供货商品</legend><div class="m1-sku-options">${state.meta.skus.map(k=>`<label class="check-line"><input type="checkbox" name="sku_ids" value="${esc(k.id)}" ${s?.sku_ids?.includes(k.id)?'checked':''}>${esc(k.code)} · ${esc(k.name)}</label>`).join('')}</div></fieldset>`,f=>{const data=new FormData(f),body={code:data.get('code'),name:data.get('name'),default_lead_days:Number(data.get('default_lead_days')),active:data.get('active')==='true',sku_ids:data.getAll('sku_ids')};if(s)body.expected_version=s.version;return m1Write('/v2/catalog/suppliers'+(s?'/'+s.id:''),body,s?'PUT':'POST')})
}
function m1MappingEdit(m=null){
 m1Modal(m?'编辑来源映射':'新建来源映射',`<label>来源主体 / 连接账号编码<input name="connection_account" maxlength="120" required value="${esc(m?.connection_account||'')}"></label><label>外部商品编码<input name="source_code" maxlength="120" required value="${esc(m?.source_code||'')}"></label><label>内部商品<select name="sku_id">${m1SkuOptions()}</select></label><label>状态<select name="active"><option value="true">启用</option><option value="false" ${m?.active===false?'selected':''}>停用</option></select></label>`,f=>{const body=Object.fromEntries(new FormData(f));body.active=body.active==='true';if(m)body.expected_version=m.version;return m1Write('/v2/catalog/external-mappings'+(m?'/'+m.id:''),body,m?'PUT':'POST')});if(m)$('#editor-form select[name=sku_id]').value=m.sku_id;
}
function m1AssortmentEdit(r=null){
 m1Modal('设置商品与仓库经营范围',`<label>仓库<select name="warehouse_id" ${r?'disabled':''}>${warehouseOptions()}</select></label><label>商品<select name="sku_id" ${r?'disabled':''}>${m1SkuOptions()}</select></label><label class="check-line"><input name="active" type="checkbox" ${r?.active!==false?'checked':''}>经营此商品</label><label class="check-line"><input name="replenishment" type="checkbox" ${r?.replenishment?'checked':''}>纳入补货计算范围</label><p class="muted">建立经营关系不会产生库存；未确认库存仍为未知。</p>`,async f=>{const data=new FormData(f),wid=r?.warehouse_id||data.get('warehouse_id'),sid=r?.sku_id||data.get('sku_id');const existing=r||m1.assortments.items.find(x=>x.warehouse_id===wid&&x.sku_id===sid);return m1Write(`/v2/catalog/assortments/${encodeURIComponent(wid)}/${encodeURIComponent(sid)}`,{active:data.has('active'),replenishment:data.has('replenishment'),expected_version:existing?.version||0},'PUT')});if(r){$('#editor-form select[name=warehouse_id]').value=r.warehouse_id;$('#editor-form select[name=sku_id]').value=r.sku_id}
}
function m1ConfirmStock(r){
 m1Modal('核实库存证据',`<p>${esc(sku(r.sku_id).name)} · ${esc(wh(r.warehouse_id).name)}</p><p class="muted">填写实际盘核结果，仅在与账面一致时确认“已知”。此操作不调整库存；不一致时请走审批调整。</p><label>盘核凭证<input name="reference" maxlength="120" required></label><div class="quantities">${['g','q','d'].map((k,i)=>`<label>${['良品','待验','残次'][i]}<input name="${k}" type="number" min="0" step="1" required placeholder="请输入实际核实数量"></label>`).join('')}</div>`,f=>{const data=new FormData(f);return m1Write(`/v2/catalog/assortments/${encodeURIComponent(r.warehouse_id)}/${encodeURIComponent(r.sku_id)}/confirm`,{expected_version:r.version,expected_balance_version:r.balance_version||0,reference:data.get('reference'),observed:Object.fromEntries(['g','q','d'].map(k=>[k,Number(data.get(k))]))})})
}
async function m1ReadAndRender(work){if(m1.loading)return;m1.loading=true;try{await work();render()}catch(e){toast(e.message)}finally{m1.loading=false}}
function m1Bind(){
 if($('#m1-refresh'))$('#m1-refresh').onclick=()=>m1ReadAndRender(m1Load);
 if($('#m1-retry'))$('#m1-retry').onclick=async()=>{try{await m1Write(null,null,null,m1Pending());await load();render();toast('原请求已确认，未重复执行')}catch(e){toast(e.message);render()}};
 if($('#m1-inventory-warehouse')){$('#m1-inventory-warehouse').value=state.warehouse;$('#m1-inventory-sku').value=m1.inventorySku;$('#m1-inventory-refresh').onclick=()=>{state.warehouse=$('#m1-inventory-warehouse').value;m1.inventorySku=$('#m1-inventory-sku').value;m1ReadAndRender(()=>m1GetInventory())}}
 if($('#m1-event-warehouse')){$('#m1-event-warehouse').value=m1.eventWarehouse;$('#m1-event-sku').value=m1.eventSku;$('#m1-events-refresh').onclick=()=>{m1.eventWarehouse=$('#m1-event-warehouse').value;m1.eventSku=$('#m1-event-sku').value;m1ReadAndRender(()=>m1GetEvents())}}
 document.querySelectorAll('[data-m1-page]').forEach(b=>b.onclick=()=>m1ReadAndRender(()=>{const offset=Number(b.dataset.offset),kind=b.dataset.m1Page;return kind==='assortments'?m1GetInventory(offset):kind==='events'?m1GetEvents(offset):m1GetCatalog(kind,offset)}));
 if($('#m1-create-suppliers'))$('#m1-create-suppliers').onclick=()=>m1SupplierEdit();
 if($('#m1-create-mappings'))$('#m1-create-mappings').onclick=()=>m1MappingEdit();
 if($('#assortment-new'))$('#assortment-new').onclick=()=>m1AssortmentEdit();
 document.querySelectorAll('[data-sku-toggle]').forEach(b=>b.onclick=()=>{const s=m1.skus.items.find(x=>x.id===b.dataset.skuToggle);m1Modal(s.active===false?'启用商品':'停用商品',`<p>${esc(s.name)} · ${esc(s.code)}</p><p class="muted">停用后保留历史库存与单据，退出补货范围。</p>`,()=>m1Write('/v2/catalog/skus/'+s.id,{active:s.active===false,expected_version:s.version},'PUT'))});
 document.querySelectorAll('[data-warehouse-edit]').forEach(b=>b.onclick=()=>{const w=m1.warehouses.items.find(x=>x.id===b.dataset.warehouseEdit);m1Modal('设置仓库用途',`<label>${esc(w.name)}<select name="purpose">${['general','storage','fulfillment','transit','returns'].map(p=>`<option value="${p}" ${w.purpose===p?'selected':''}>${m1Purpose(p)}</option>`).join('')}</select></label>`,f=>m1Write('/v2/catalog/warehouses/'+w.id,{purpose:f.elements.purpose.value,expected_version:w.version},'PUT'))});
 document.querySelectorAll('[data-supplier-edit]').forEach(b=>b.onclick=()=>m1SupplierEdit(m1.suppliers.items.find(x=>x.id===b.dataset.supplierEdit)));
 document.querySelectorAll('[data-mapping-edit]').forEach(b=>b.onclick=()=>m1MappingEdit(m1.mappings.items.find(x=>x.id===b.dataset.mappingEdit)));
 document.querySelectorAll('[data-assortment-edit]').forEach(b=>b.onclick=()=>m1AssortmentEdit(m1.assortments.items.find(x=>x.warehouse_id===b.dataset.assortmentEdit&&x.sku_id===b.dataset.sku)));
 document.querySelectorAll('[data-stock-confirm]').forEach(b=>b.onclick=()=>m1ConfirmStock(m1.assortments.items.find(x=>x.warehouse_id===b.dataset.stockConfirm&&x.sku_id===b.dataset.sku)));
}
