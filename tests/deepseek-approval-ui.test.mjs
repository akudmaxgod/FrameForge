import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {chromium} from 'playwright-core';

test('shows the countdown and sends approval only after a button click', async () => {
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage();
  const moduleSource=readFileSync(new URL('../imagegen/static/deepseek-approval.mjs',import.meta.url),'utf8');
  await page.route('http://frameforge.test/',route=>route.fulfill({contentType:'text/html',body:'<body></body>'}));
  await page.route('http://frameforge.test/approval.mjs',route=>route.fulfill({contentType:'text/javascript',body:moduleSource}));
  await page.goto('http://frameforge.test/');
  await page.evaluate(async()=>{
   const {watchPaidPassApprovals}=await import('/approval.mjs');
   window.submissions=[];
   window.pending={id:'expiring',label:'Сцены 17–32',expires_at:Date.now()/1000+2.5};
   window.stop=watchPaidPassApprovals(async(path,body)=>{
    if(body){window.submissions.push({path,body});window.pending=null;return {ok:true};}
    return {requests:window.pending?[window.pending]:[]};
   });
  });
  const paid=page.getByRole('button',{name:'Запустить DeepSeek · платно'});
  await page.getByTestId('deepseek-countdown').waitFor();
  await page.waitForFunction(()=>document.querySelector('[role=alertdialog] button')?.disabled===true);
  assert.deepEqual(await page.evaluate(()=>window.submissions),[]);
  await page.evaluate(()=>window.pending={id:'approved',label:'Сцены 17–32',expires_at:Date.now()/1000+60});
  await page.waitForFunction(()=>document.querySelector('[role=alertdialog] button')?.disabled===false);
  await paid.click();
  assert.deepEqual(await page.evaluate(()=>window.submissions),[{path:'/deepseek/approvals/approved',body:{decision:'deepseek'}}]);
  await page.evaluate(()=>window.stop());
 } finally {await browser.close();}
});
