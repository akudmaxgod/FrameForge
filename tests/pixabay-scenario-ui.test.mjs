import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';

test('Pixabay scenario button launches bounded plan and renders durable job results', {timeout:30000}, async t=>{
 const browser=await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 t.after(()=>browser.close());
 const page=await browser.newPage();
 await page.route('**/app.js?*',r=>r.fulfill({contentType:'text/javascript',body:''}));
 await page.goto('http://127.0.0.1:8765');
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.evaluate(async()=>{
  const {openMaterialSearchModal}=await import('/static/material-search.mjs?v=pixabay-test');
  window.testCalls=[];
  const req={id:'r1',quote:'Глаз и зрение',enabled:true,origin_clip_ids:['c1'],effective_queries:['eye']};
  const plan={plan_id:'plan1',requirements:[req],total_queries:1,segments_count:1};
  const api=async(path,body)=>{
   window.testCalls.push({path,body});
   if(path.endsWith('/providers'))return {providers:[]};
   if(path.endsWith('/pixabay-latest'))return {job:null,plan:null};
   if(path.endsWith('/plans'))return plan;
   if(path.endsWith('/jobs'))return {id:'job1',state:'queued'};
   if(path.endsWith('/jobs/job1'))return {id:'job1',state:'completed',progress:1};
   if(path.endsWith('/candidates'))return {candidates:[{id:'cand1',requirement_id:'r1',semantic_status:'needs_review',candidate:{title:'Human eye',description:'eye',provider:'pixabay',media_type:'photo'}}]};
   throw Error(path);
  };
  await openMaterialSearchModal({projectId:'project1',api,notice:(text)=>{window.testNotice=text}});
 });
 await page.getByRole('button',{name:'📋 По сценарию',exact:true}).click();
 await page.getByRole('button',{name:'✨ Подобрать вставки Pixabay по сценарию',exact:true}).click();
 await page.getByText('Human eye',{exact:true}).waitFor();
 const calls=await page.evaluate(()=>window.testCalls);
 const planCall=calls.find(c=>c.path.endsWith('/plans'));
 assert.equal(planCall.body.options.pixabay_scenario,true);
 assert.equal(planCall.body.options.pixabay_media_type,'both');
 assert.ok(calls.some(c=>c.path.endsWith('/jobs/job1')));
 assert.equal(await page.getByRole('button',{name:'✓ Применить к сцене',exact:true}).count(),1);
 assert.deepEqual(errors,[]);
});
