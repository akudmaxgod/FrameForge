import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import fs from 'node:fs';
const moduleSource=fs.readFileSync(new URL('../imagegen/static/material-search.mjs',import.meta.url));

test('Pixabay scenario button launches bounded plan and renders durable job results', {timeout:30000}, async t=>{
 const browser=await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 t.after(()=>browser.close());
 const page=await browser.newPage();
 await page.route('**/app.js?*',r=>r.fulfill({contentType:'text/javascript',body:''}));
 await page.route('**/static/material-search.mjs*',r=>r.fulfill({contentType:'text/javascript',body:moduleSource}));
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
 assert.equal(planCall.body.options.discovery_only,true);
 assert.ok(calls.some(c=>c.path.endsWith('/jobs/job1')));
 assert.equal(await page.getByRole('button',{name:'✓ Применить к сцене',exact:true}).count(),1);
 assert.deepEqual(errors,[]);
});

test('search errors stay visible and animated candidates offer library import',{timeout:30000},async t=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage();
 await page.route('**/app.js?*',r=>r.fulfill({contentType:'text/javascript',body:''}));await page.route('**/static/material-search.mjs*',r=>r.fulfill({contentType:'text/javascript',body:moduleSource}));await page.goto('http://127.0.0.1:8765');
 await page.evaluate(async()=>{
  const {openMaterialSearchModal}=await import('/static/material-search.mjs?v=protected-test');window.failPlan=true;window.testCalls=[];
  const plan={plan_id:'p',requirements:[{id:'r',enabled:true,quote:'Остров',origin_clip_ids:['c'],effective_queries:['island']}],options:{pixabay_scenario:true,protected_clip_ids:['c']}};
  await openMaterialSearchModal({projectId:'test',notice:()=>{},api:async(path,body)=>{
   window.testCalls.push({path,body});if(path.endsWith('/providers'))return {providers:[]};if(path.endsWith('/pixabay-latest'))return {};
   if(path.endsWith('/plans')){if(window.failPlan)throw Error('TEST_SEARCH_ERROR');return plan;}
   if(path.endsWith('/jobs'))return {id:'j'};if(path.endsWith('/jobs/j'))return {id:'j',state:'completed',progress:1};
   if(path.endsWith('/candidates'))return {candidates:[{id:'i',requirement_id:'r',candidate:{title:'Island footage'}}]};throw Error(path);
  }});
 });
 await page.getByRole('button',{name:'📋 По сценарию',exact:true}).click();const search=page.getByRole('button',{name:'✨ Подобрать вставки Pixabay по сценарию',exact:true});await search.click();await page.locator('[role=status]').filter({hasText:'TEST_SEARCH_ERROR'}).waitFor();
 await page.evaluate(()=>window.failPlan=false);await search.click();await page.getByText('Island footage',{exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'✓ Применить к сцене',exact:true}).count(),0);assert.equal(await page.getByRole('button',{name:'Добавить в медиатеку',exact:true}).count(),1);
 assert.equal(await page.locator('.material-search-plan-section details').evaluate(n=>n.open),false);assert.equal(await page.evaluate(()=>window.testCalls.some(c=>c.path.endsWith('/apply'))),false);
});
