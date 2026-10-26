/* Real queue UI, restart recovery, retry history and late-response privacy. */
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [base,fixtureFile,output]=process.argv.slice(2);
if(!base||new URL(base).hostname!=='127.0.0.1')throw Error('Local disposable workbench required');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync(fixtureFile,'utf8')),credentials=JSON.parse(fs.readFileSync(fixture.login_file,'utf8'));
 fs.mkdirSync(output,{recursive:true});const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'}),page=await context.newPage();
 const steps=[],errors=[];let step='login';page.on('pageerror',e=>errors.push(String(e)));
 const login=async username=>{await page.getByLabel('账号',{exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill(credentials.password);await page.getByRole('button',{name:/进入工作台/}).click();await page.locator('#app-view').waitFor({state:'visible'})};
 const get=async route=>{const r=await context.request.get(base+route);assert(r.ok(),await r.text());return r.json()};
 const queue=()=>page.locator('section.card').filter({has:page.getByRole('heading',{name:'分析任务',exact:true})});
 try{
  await page.goto(base);await login('tenant-0-admin');assert.equal((await get('/health')).version,'0.10.1');
  const session=await get('/api/v1/session'),meta=await get('/api/v1/bootstrap'),warehouseUsername='queue-warehouse-'+Date.now();
  const created=await context.request.post(base+'/api/v1/users',{headers:{'X-CSRF-Token':session.csrf,'Idempotency-Key':'queue-browser-'+crypto.randomUUID()},data:{username:warehouseUsername,name:'队列验收仓管',role:'warehouse',warehouse_ids:[meta.warehouses[0].id],password:credentials.password}});assert(created.ok(),await created.text());
  await page.locator('.sidebar').getByRole('button',{name:/流程复盘$/}).click();
  step='startup';const recovered=await get('/api/v2/reviews/jobs/'+fixture.queued_job);assert.equal(recovered.status,'completed');assert.equal(recovered.attempts,1);
  const health=await get('/api/v2/reviews/worker-status');assert(health.enabled&&health.alive);assert(!('reports_observed' in health));steps.push('startup-drains-persisted-queue');
  step='reload';await page.locator(`[data-m6=retry-job][data-id="${fixture.failed_job}"]`).waitFor();await page.reload();await page.locator('#app-view').waitFor({state:'visible'});
  await page.locator(`[data-m6=retry-job][data-id="${fixture.failed_job}"]`).waitFor();steps.push('failed-job-survives-reload');
  step='retry';await page.locator(`[data-m6=retry-job][data-id="${fixture.failed_job}"]`).click();const dialog=page.getByRole('dialog');await dialog.locator('[name=reason]').fill('浏览器核实故障已消除，再次分析');
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/retry'));await dialog.getByRole('button',{name:'确认提交',exact:true}).click();assert.equal((await response).status(),202);await dialog.waitFor({state:'hidden'});
  await page.waitForFunction(id=>m6.jobs.items.some(x=>x.id===id&&x.status==='completed'),fixture.failed_job,{timeout:30000});
  const job=await get('/api/v2/reviews/jobs/'+fixture.failed_job);assert.equal(job.attempts,4);assert(job.report_id);
  assert.equal(await page.locator(`[data-m6=retry-job][data-id="${fixture.failed_job}"]`).count(),0);steps.push('manual-retry-publishes-single-report');
  step='history';await page.locator(`[data-m6=job-history][data-id="${fixture.failed_job}"]`).click();await page.getByRole('heading',{name:'任务处理记录',exact:true}).waitFor();
  const history=await get(`/api/v2/reviews/jobs/${fixture.failed_job}/history?limit=100`);assert.equal(history.items.filter(x=>x.event==='retry_requested').length,1);assert.equal(history.items.filter(x=>x.event==='completed').length,1);
  await page.getByText(/浏览器核实故障已消除/).waitFor();await page.getByRole('heading',{name:'任务处理记录',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'desktop-history.png')});steps.push('durable-attempt-history');
  step='mobile';await page.setViewportSize({width:390,height:844});await queue().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'mobile-queue.png')});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  await page.getByRole('heading',{name:'任务处理记录',exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'mobile-history.png')});steps.push('mobile-queue-and-history');await page.setViewportSize({width:1440,height:1000});
  step='late-response';let first=true,release,ready;const waiting=new Promise(r=>ready=r);
  await page.route('**/api/v2/reviews/jobs?limit=20',async route=>{if(!first)return route.continue();first=false;const response=await route.fetch();await new Promise(resolve=>{release=resolve;ready()});await route.fulfill({response})});
  await page.evaluate(()=>{window.pendingOldLoad=m6Load()});let deadline;
  await Promise.race([waiting,new Promise((_,reject)=>{deadline=setTimeout(()=>reject(Error('Old queue response was not intercepted')),30000)})]).finally(()=>clearTimeout(deadline));
  await page.getByRole('button',{name:'退出',exact:true}).click();await login(warehouseUsername);release();await page.evaluate(()=>window.pendingOldLoad);
  assert(await page.evaluate(id=>!m6.jobs.items.some(x=>x.id===id)&&m6.history===null,fixture.failed_job));
  assert.equal((await context.request.get(`${base}/api/v2/reviews/jobs/${fixture.failed_job}/history`)).status(),404);
  assert.equal((await context.request.get(`${base}/api/v2/reviews/worker-status`)).status(),403);steps.push('owner-scope-and-late-response-isolation');
  const digest=data=>require('node:crypto').createHash('sha256').update(data).digest('hex');
  const asset=await context.request.get(base+'/static/m6.js?v=0.10.1');assert(asset.ok());const asset_sha256=digest(await asset.body());assert.equal(asset_sha256,digest(fs.readFileSync(path.join(__dirname,'../app/static/m6.js'))));
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:true,steps,pageErrors:errors,base,run_id:fixture.run_id,asset_sha256},null,2));console.log(JSON.stringify({passed:true,steps}));
 }catch(error){await page.screenshot({path:path.join(output,'failure.png')});fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:false,step,error:String(error),steps,pageErrors:errors},null,2));throw error}
 finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
