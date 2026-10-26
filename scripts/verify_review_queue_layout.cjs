/* Read-only visual inspection of the completed queue fixture. */
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [base,fixtureFile,output]=process.argv.slice(2);
if(!base||new URL(base).hostname!=='127.0.0.1')throw Error('Local workbench required');
(async()=>{
 const fixture=JSON.parse(fs.readFileSync(fixtureFile,'utf8')),credentials=JSON.parse(fs.readFileSync(fixture.login_file,'utf8'));
 fs.mkdirSync(output,{recursive:true});const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},timezoneId:'Asia/Shanghai'}),page=await context.newPage();
  await page.goto(base);await page.getByLabel('账号',{exact:true}).fill('tenant-0-admin');await page.getByLabel('密码',{exact:true}).fill(credentials.password);
  await page.getByRole('button',{name:/进入工作台/}).click();await page.locator('#app-view').waitFor({state:'visible'});
  await page.locator('.sidebar').getByRole('button',{name:/流程复盘$/}).click();await page.locator(`[data-m6=job-history][data-id="${fixture.failed_job}"]`).click();
  const history=page.locator('section.card').filter({has:page.getByRole('heading',{name:'任务处理记录',exact:true})});
  const queue=page.locator('section.card').filter({has:page.getByRole('heading',{name:'分析任务',exact:true})});
  await history.waitFor();await page.locator('#toast').waitFor({state:'hidden'});await page.evaluate(()=>document.fonts.ready);
  await history.evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:path.join(output,'desktop-history.png')});
  await page.setViewportSize({width:390,height:844});await queue.evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:path.join(output,'mobile-queue.png')});
  const wrap=queue.locator('.table-wrap');const scroll=await wrap.evaluate(el=>{el.scrollLeft=el.scrollWidth;return {scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,left:el.scrollLeft}});
  const button=queue.locator('[data-m6=job-history]').first();const box=await button.boundingBox();assert(box&&box.x>=0&&box.x+box.width<=390);
  await page.screenshot({path:path.join(output,'mobile-queue-actions.png')});await button.click();await page.locator('#toast').waitFor({state:'hidden'});
  await page.locator(`[data-m6=job-history][data-id="${fixture.failed_job}"]`).click();await page.locator('#toast').waitFor({state:'hidden'});
  await history.evaluate(el=>el.scrollIntoView({block:'start'}));await page.screenshot({path:path.join(output,'mobile-history.png')});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({passed:true,viewport:{width:390,height:844},horizontalTableScroll:scroll,actionsReachable:true,screenshots:['desktop-history.png','mobile-queue.png','mobile-queue-actions.png','mobile-history.png']},null,2));
  console.log(JSON.stringify({passed:true,actionsReachable:true}));
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
