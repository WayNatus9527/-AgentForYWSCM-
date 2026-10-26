/* Optional Playwright smoke test. Use only a disposable local development run. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const [base,loginFile,output]=process.argv.slice(2);
if(!base||new URL(base).hostname!=='127.0.0.1'||!loginFile||!output)throw new Error('Expected local URL, generated login JSON and output directory');
const credentials=JSON.parse(fs.readFileSync(loginFile,'utf8'));
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'});
 const page=await context.newPage();const errors=[];const steps=[];
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('response',r=>{const pathname=new URL(r.url()).pathname;if(pathname==='/api/v1/session'&&r.status()===401&&r.request().method()==='GET')return;if(pathname.startsWith('/api/')&&r.status()>=400)errors.push(`${r.status()} ${pathname}`)});
 const stamp=Date.now().toString();let currentStep='login';
 const navigate=async name=>{await page.locator('.sidebar').getByRole('button',{name:new RegExp(name+'$')}).click();await page.waitForTimeout(80)};
 const dialog=()=>page.getByRole('dialog');
 const submit=async route=>{const wait=route?page.waitForResponse(r=>r.request().method()==='POST'&&new URL(r.url()).pathname.endsWith(route)):null;await dialog().getByRole('button',{name:'确认提交',exact:true}).click();const response=wait?await wait:null;if(response){assert(response.ok(),await response.text())}await dialog().waitFor({state:'hidden'});await page.waitForTimeout(100);return response?response.json():null};
 const login=async username=>{await page.getByRole('textbox',{name:'账号',exact:true}).fill(username);await page.getByLabel('密码',{exact:true}).fill(credentials.password);await page.getByRole('button',{name:/进入工作台/}).click();await page.locator('#app-view').waitFor({state:'visible'})};
 try{
  await page.goto(base);await login('tenant-0-admin');
  const session=await (await context.request.get(base+'/api/v1/session')).json();
  const meta=await (await context.request.get(base+'/api/v1/bootstrap')).json();
  const warehouse=meta.warehouses.find(w=>w.authority==='local'),sku=meta.skus[0];
  const stocks=await (await context.request.get(base+'/api/v1/inventory')).json();
  const before=stocks.find(x=>x.warehouse_id===warehouse.id&&x.sku_id===sku.id);
  currentStep='connection';await navigate('模拟协同与对账');await page.getByRole('button',{name:'＋ 模拟连接',exact:true}).click();
  await dialog().getByLabel('来源账号编码').fill('browser-'+stamp);await dialog().getByLabel('连接显示名称').fill('浏览器验收渠道 '+stamp);
  await dialog().getByRole('checkbox',{name:warehouse.name,exact:true}).check();
  const account=await submit('/connections');steps.push('connection-created');
  currentStep='import-order';await navigate('订单履约');await page.getByRole('button',{name:'导入模拟订单',exact:true}).click();
  await dialog().getByLabel('来源账号').selectOption(account.id);const sourceOrder='BROWSER-'+stamp;
  await dialog().getByLabel('来源订单号',{exact:true}).fill(sourceOrder);await dialog().getByLabel('来源行号',{exact:true}).fill('B1');
  await dialog().locator('[name=sku_id]').selectOption(sku.id);await dialog().getByLabel('订购数量').fill('6');
  await submit('/events');await navigate('模拟协同与对账');
  await page.getByRole('row').filter({hasText:sourceOrder}).getByRole('button',{name:'处理',exact:true}).click();await page.waitForTimeout(250);
  await navigate('订单履约');const card=()=>page.locator('section.card.form-card').filter({has:page.getByRole('heading',{name:new RegExp(sourceOrder)})});
  const action=async(name,route,fill)=>{await card().getByRole('button',{name,exact:true}).click();if(await dialog().locator('[name=reason]').count())await dialog().locator('[name=reason]').fill('浏览器模拟验收');if(fill)await fill(dialog());return submit(route)};
  currentStep='confirm-allocate';await action('确认订单','/confirm');await action('分配库存','/allocate',d=>d.getByLabel('分配数量').fill('6'));
  for(const [name,route] of [['开始拣货','/start'],['完成拣货','/complete'],['开始复核','/start'],['完成复核','/complete']]){currentStep=name;await action(name,route)}
  currentStep='partial-shipment';const shipped=await action('确认实物交接','/ship',async d=>{await d.getByLabel('本次交接凭证号').fill('SHIP-'+stamp);await d.getByLabel('实际交接数量').fill('2');await d.locator('[name=occurred_at]').fill('2026-10-25T08:00')});
  await action('取消未发数量','/cancel',d=>d.getByLabel('取消未发数量',{exact:true}).fill('4'));steps.push('pick-review-partial-ship-cancel');
  currentStep='channel-receipt';await action('登记渠道完成回执','/events',async d=>{await d.getByLabel('来源回执编号').fill('COMPLETE-'+stamp);await d.locator('[name=occurred_at]').fill('2026-10-25T09:00')});
  await navigate('模拟协同与对账');await page.getByRole('row').filter({hasText:'COMPLETE-'+stamp}).getByRole('button',{name:'处理',exact:true}).click();await page.waitForTimeout(200);
  await navigate('订单履约');await action('登记运输签收回执','/events',async d=>{await d.getByLabel('来源回执编号').fill('DELIVERED-'+stamp);await d.locator('[name=occurred_at]').fill('2026-10-25T10:00')});
  await navigate('模拟协同与对账');await page.getByRole('row').filter({hasText:'DELIVERED-'+stamp}).getByRole('button',{name:'处理',exact:true}).click();await page.waitForTimeout(200);
  currentStep='outbox';for(let n=0;n<8;n++){if(await page.getByRole('row').filter({hasText:shipped.outbox_id}).getByText('已确认成功',{exact:true}).count())break;await page.getByRole('button',{name:'处理下一项',exact:true}).click();await page.waitForTimeout(150)}
  assert.equal(await page.getByRole('row').filter({hasText:shipped.outbox_id}).getByText('已确认成功',{exact:true}).count(),1);steps.push('receipts-and-outbox');
  currentStep='snapshot';await page.getByRole('button',{name:'录入模拟快照',exact:true}).click();await dialog().getByLabel('来源账号').selectOption(account.id);
  const snapshotName='SNAP-'+stamp;await dialog().getByLabel('来源快照号').fill(snapshotName);await dialog().locator('[name=warehouse_id]').selectOption(warehouse.id);await dialog().locator('[name=sku_id]').selectOption(sku.id);
  for(const [key,n] of Object.entries({g:before.g-2,q:before.q,d:before.d}))await dialog().locator(`[name=${key}]`).fill(String(n));
  await dialog().locator('[name=covered_watermark]').fill('0');await dialog().locator('[name=snapshot_at]').fill('2026-10-25T10:00');await submit('/snapshots');
  const compare=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/compare')&&r.request().method()==='POST');
  await page.locator('.m4-batch').filter({hasText:snapshotName}).getByRole('button',{name:'执行对账',exact:true}).click();const compared=await (await compare).json();assert.equal(compared.status,'matched');steps.push('watermark-reconciliation');
  await page.waitForTimeout(150);await page.screenshot({path:path.join(output,'collaboration-desktop.png'),fullPage:true});
  await navigate('订单履约');await page.screenshot({path:path.join(output,'orders-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'orders-mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Order page overflows mobile viewport');
  await navigate('模拟协同与对账');await page.screenshot({path:path.join(output,'collaboration-mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Collaboration page overflows mobile viewport');steps.push('desktop-and-mobile-layout');
  currentStep='role-views';const members=[];
  for(const role of ['operations','analyst']){
   const response=await context.request.post(base+'/api/v1/users',{headers:{'X-CSRF-Token':session.csrf,'Idempotency-Key':'browser-'+role+'-'+stamp},data:{username:role+'-'+stamp,name:'浏览器'+role,role,warehouse_ids:[warehouse.id],password:credentials.password}});assert(response.ok());members.push(await response.json());
  }
  const edited=await context.request.post(base+'/api/v2/connections/'+account.id,{headers:{'X-CSRF-Token':session.csrf,'Idempotency-Key':'members-'+stamp},data:{expected_version:1,member_ids:members.map(x=>x.id),active:true}});assert(edited.ok());
  for(const user of members){await page.getByRole('button',{name:'退出',exact:true}).click();await login(user.username);await navigate('订单履约');assert.equal(await card().count(),1);assert.equal(await card().getByRole('button',{name:'确认实物交接',exact:true}).count(),0);if(user.role==='analyst')assert.equal(await page.getByRole('button',{name:'＋ 新建订单',exact:true}).count(),0);assert.equal(await page.locator('.sidebar [hidden]').evaluateAll(nodes=>nodes.filter(n=>getComputedStyle(n).display!=='none').length),0);await page.screenshot({path:path.join(output,user.role+'-mobile.png'),fullPage:true})}
  steps.push('operations-and-analyst-views');assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({status:'passed',steps,errors,source_order_id:sourceOrder,viewport_sizes:[[1440,1000],[390,844]]},null,2));console.log(JSON.stringify({status:'passed',steps,errors}));
 }catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({status:'failed',step:currentStep,error:String(e),errors,steps},null,2));throw e}
 finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
