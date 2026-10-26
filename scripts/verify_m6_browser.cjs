/* Real browser review workflow on a disposable generated M6 development run. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [base,loginFile,output]=process.argv.slice(2);
if(!base||new URL(base).hostname!=='127.0.0.1')throw Error('Expected local disposable workbench');
const credentials=JSON.parse(fs.readFileSync(loginFile,'utf8'));fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'}),page=await context.newPage();
 const errors=[],steps=[];let step='login';page.on('pageerror',e=>errors.push(String(e)));
 const dialog=()=>page.getByRole('dialog');
 const get=async route=>{const r=await context.request.get(base+route);assert(r.ok(),await r.text());return r.json()};
 const post=async(route,data)=>{const current=await get('/api/v1/session');const r=await context.request.post(base+route,{headers:{'X-CSRF-Token':current.csrf,'Idempotency-Key':'browser-'+crypto.randomUUID()},data});assert(r.ok(),await r.text());return r.json()};
 const navigate=async name=>{await page.locator('.sidebar').getByRole('button',{name:new RegExp(name+'$')}).click()};
 const login=async username=>{await page.getByLabel('账号',{exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill(credentials.password);await page.getByRole('button',{name:/进入工作台/}).click();await page.locator('#app-view').waitFor({state:'visible'})};
 const fill=async values=>{for(const [k,v] of Object.entries(values))await dialog().locator(`[name=${k}]`).fill(String(v))};
 const submit=async suffix=>{const waiting=page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname.endsWith(suffix));await dialog().getByRole('button',{name:'确认提交',exact:true}).click();const r=await waiting;assert(r.ok(),await r.text());await dialog().waitFor({state:'hidden'});return r.json()};
 try{
  await page.goto(base);await login('tenant-0-admin');assert.equal((await get('/health')).stage,'M6');
  const meta=await get('/api/v1/bootstrap'),runtime=await get('/api/v2/runtime');const warehouse=meta.warehouses[0],sku=meta.skus[0];
  const initial=(await get('/api/v2/reviews/reports')).items[0];assert(initial);
  const newReport=async()=>{
   await page.getByRole('button',{name:'新建复盘',exact:true}).click();await dialog().locator('[name=warehouse_id]').selectOption(warehouse.id);await dialog().locator('[name=sku_id]').selectOption(sku.id);
   await fill({start_at:initial.request.start_at.slice(0,16),end_at:runtime.business_time.slice(0,16),min_samples:1});
   const job=await submit('/reviews/jobs');await page.waitForFunction(id=>m6.job?.id===id&&m6.job?.status==='completed',job.id,{timeout:30000});
   return get('/api/v2/reviews/reports/'+(await get('/api/v2/reviews/jobs/'+job.id)).report_id);
  };
  step='analysis';await navigate('流程复盘');const first=await newReport();assert.equal(first.summary.metrics.quality_wait.median_hours,48);
  const candidate=page.locator('article.card').filter({has:page.getByRole('heading',{name:/质检等待.*有证据的限制/})});await candidate.waitFor();
  await page.screenshot({path:path.join(output,'review-desktop.png'),fullPage:true});steps.push('background-report-quantiles-constraint');
  step='improvement';await candidate.getByRole('button',{name:'创建改善任务'}).click();await fill({reason:'浏览器核对质检班次后改善'});const task=await submit('/improve');assert(task.task_id);
  await candidate.getByRole('button',{name:'创建改善任务'}).click();await fill({reason:'再次查看改善任务'});const same=await submit('/improve');assert.equal(task.task_id,same.task_id);steps.push('idempotent-improvement-task');
  step='evidence';await candidate.getByRole('button',{name:'查看样本'}).click();await page.getByRole('heading',{name:/质检等待.*已完成/}).waitFor();
  assert(await page.getByText(/自然 48 h/).count());await page.locator('[data-m6=category][data-id=impact]').click();await page.getByText(/批次内关键路径/).waitFor();
  await page.locator('[data-m6=category][data-id=event]').click();const arrival=page.locator('article.card').filter({has:page.getByRole('heading',{name:'supply_batch.receive',exact:true})});await arrival.waitFor();
  step='correction';await arrival.getByRole('button',{name:'订正时间证据'}).click();await fill({occurred_at:'2026-10-06T00:00',reason:'浏览器核实签收凭证，追加时间订正'});await submit('/correct');
  assert.equal((await get('/api/v2/reviews/reports/'+first.id)).summary.metrics.quality_wait.median_hours,48);
  const second=await newReport();assert.equal(second.summary.metrics.quality_wait.median_hours,49);assert.notEqual(first.source_hash,second.source_hash);steps.push('append-only-time-correction');
  step='comparison';await page.getByRole('button',{name:'比较两个报告',exact:true}).click();await dialog().locator('[name=before]').selectOption(first.id);await dialog().locator('[name=after]').selectOption(second.id);
  await dialog().getByRole('button',{name:'确认提交',exact:true}).click();await dialog().waitFor({state:'hidden'});await page.getByRole('heading',{name:'报告比较',exact:true}).waitFor();assert(await page.getByText('指标与筛选口径一致').count());steps.push('before-after-comparison');
  step='agent-explanation';await page.getByRole('button',{name:'解释本报告',exact:true}).click();await page.getByText(/审计编号/).waitFor();
  const agent=page.locator('#m6-agent');await agent.locator('[name=selected_tool]').selectOption('track_purchase');await agent.getByRole('button',{name:'使用规则工具',exact:true}).click();
  await page.getByText('按实际业务登记分批发出、签收和检验；库存变化仍由对应业务流程执行',{exact:true}).waitFor();steps.push('deterministic-explanation-tracking');
  const supplier=(await get('/api/v2/catalog/suppliers?limit=100')).items.find(x=>x.code==='m6-vendor');
  const policy=await post('/api/v2/planning/policies',{warehouse_id:warehouse.id,sku_id:sku.id,supplier_id:supplier.id,window_days:3,coverage_days:1,lead_days:2,safety:0,minimum:1,pack:1});
  for(const day of ['2026-10-05','2026-10-06','2026-10-07'])await post('/api/v2/planning/demand',{warehouse_id:warehouse.id,sku_id:sku.id,day,quantity:100,source_reference:'browser-'+day});
  const purchases=(await get('/api/v2/supply-orders/purchase')).items.length;
  await page.reload();await page.locator('#app-view').waitFor({state:'visible'});await navigate('流程复盘');
  step='agent-plan';await page.locator('#m6-tool').selectOption('calculate_replenishment');await page.locator('#m6-identity').selectOption(policy.id);
  await page.locator('#m6-agent').getByRole('button',{name:'使用规则工具',exact:true}).click();await page.getByText('仅生成待确认方案，请在规则补货页核对并确认',{exact:true}).waitFor();
  assert.equal((await get('/api/v2/supply-orders/purchase')).items.length,purchases);const plans=(await get('/api/v2/planning/plans')).items;assert(plans.some(x=>x.policy_id===policy.id&&!x.decision));steps.push('agent-proposal-keeps-human-confirmation');
  step='mobile';await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'review-mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));steps.push('mobile-no-overflow');await page.setViewportSize({width:1440,height:1000});
  step='roles';const analyst=await post('/api/v1/users',{username:'m6-analyst-'+Date.now(),name:'复盘分析员',role:'analyst',warehouse_ids:[warehouse.id],password:credentials.password});
  const account=(await get('/api/v2/connections')).items[0];await post('/api/v2/connections/'+account.id,{expected_version:account.version,active:true,member_ids:[...account.member_ids,analyst.id]});
  await page.getByRole('button',{name:'退出',exact:true}).click();await login(analyst.username);await navigate('流程复盘');
  assert.equal(await page.locator('#m6-tool option[value=calculate_replenishment]').count(),0);
  await page.locator('[data-m6=select]').first().click();assert.equal(await page.getByRole('button',{name:'创建改善任务',exact:true}).count(),0);
  await navigate('Agent 助手');await page.getByRole('heading',{name:'复盘与计划助手',exact:true}).waitFor();steps.push('analyst-authorized-read-only-tools');
  await page.getByRole('button',{name:'退出',exact:true}).click();await login('tenant-0-cashier');await navigate('流程复盘');assert(await page.getByText('当前岗位没有流程复盘权限。',{exact:true}).count());steps.push('cashier-denied');
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:true,steps,pageErrors:errors,base},null,2));console.log(JSON.stringify({passed:true,steps,output}));
 }catch(error){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:false,step,error:String(error),steps,pageErrors:errors},null,2));throw error}
 finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
