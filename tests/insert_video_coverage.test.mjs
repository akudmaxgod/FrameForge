import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import {chromium} from 'playwright-core';
const modules=new Map(['media-library-panel.mjs','media-coverage-controls.mjs'].map(n=>[n,fs.readFileSync(new URL('../imagegen/static/'+n,import.meta.url))]));
test('video coverage requires explicit opt-in and blocked proposals show their actual reason',{timeout:30000},async t=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage();
 await page.route('**/app.js?*',r=>r.fulfill({contentType:'text/javascript',body:''}));
 await page.route('**/*.mjs*',r=>{const name=new URL(r.request().url()).pathname.split('/').at(-1);return modules.has(name)?r.fulfill({contentType:'text/javascript',body:modules.get(name)}):r.continue();});await page.goto('http://127.0.0.1:8765');
 await page.evaluate(async()=>{
  localStorage.clear();const {openMediaMatchModal}=await import('/media-library-panel.mjs?test=coverage');window.calls=[];
  await openMediaMatchModal({projectId:'fixture',notice:()=>{},api:async(path,options)=>{window.calls.push({path,options});return {total_proposals:0,proposals:[],message:'PROTECTED_ALL: видео защищено'};}});
 });
 const toggle=page.getByRole('checkbox',{name:'Разрешить вставки поверх готового видео',exact:true});assert.equal(await toggle.isChecked(),false);
 const calculate=page.getByRole('button',{name:'🔄 Рассчитать предложения вставок',exact:true});await calculate.click();await page.getByText('PROTECTED_ALL: видео защищено',{exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>window.calls.at(-1).options.body.coverage.cover_animated_sources),undefined);
 await toggle.check();await calculate.click();await page.waitForFunction(()=>window.calls.filter(c=>c.options?.method==='POST').length===2);
 assert.equal(await page.evaluate(()=>window.calls.at(-1).options.body.coverage.cover_animated_sources),true);
 assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('frameforge-insert-coverage:fixture')).cover_animated_sources),true);
 assert.equal(await page.evaluate(()=>window.calls.some(c=>c.path.endsWith('/apply'))),false);
});
