/* Read-only mobile inspection against a populated M6 workbench. */
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [base,loginFile,output]=process.argv.slice(2);
if(!base||new URL(base).hostname!=='127.0.0.1')throw Error('Local workbench required');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:390,height:844},timezoneId:'Asia/Shanghai'}),page=await context.newPage();
  const credentials=JSON.parse(fs.readFileSync(loginFile,'utf8'));fs.mkdirSync(output,{recursive:true});
  await page.goto(base);await page.getByLabel('账号',{exact:true}).fill('tenant-0-admin');await page.getByLabel('密码',{exact:true}).fill(credentials.password);
  await page.getByRole('button',{name:/进入工作台/}).click();await page.locator('#app-view').waitFor({state:'visible'});
  await page.locator('.sidebar').getByRole('button',{name:/流程复盘$/}).click();await page.locator('[data-m6=select]').first().click();
  await page.getByRole('heading',{name:/质检等待.*有证据的限制/}).waitFor();
  await page.evaluate(async()=>{await document.fonts.ready;await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))});
  const dimensions=await page.evaluate(()=>({width:innerWidth,documentWidth:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,mainCount:document.querySelectorAll('main').length}));
  assert(dimensions.documentWidth<=dimensions.width+2);assert.equal(await page.getByRole('heading',{name:'流程复盘',exact:true}).count(),1);
  await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(output,'mobile-top.png'),fullPage:false});
  const constraint=page.locator('article.card').filter({has:page.getByRole('heading',{name:/质检等待.*有证据的限制/})});await constraint.scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(output,'mobile-constraint.png'),fullPage:false});
  await page.locator('#m6-agent').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'mobile-agent.png'),fullPage:false});
  fs.writeFileSync(path.join(output,'layout-report.json'),JSON.stringify({passed:true,dimensions,screenshots:['mobile-top.png','mobile-constraint.png','mobile-agent.png']},null,2));console.log(JSON.stringify({passed:true,dimensions}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
