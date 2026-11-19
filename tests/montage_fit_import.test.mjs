import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
test('imported prompts select semantic fitting and hide invalid old phrase previews',{timeout:20000},async t=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://frameforge.test/**',async route=>{
  const name=new URL(route.request().url()).pathname;
  if(name==='/')return route.fulfill({contentType:'text/html',body:'<main></main>'});
  const file=path.resolve(root,'imagegen/static','.'+name);assert.ok(file.startsWith(path.join(root,'imagegen/static')+path.sep));
  await route.fulfill({contentType:'text/javascript',body:fs.readFileSync(file)});
 });
 await page.goto('http://frameforge.test/');
 await page.evaluate(async()=>{
  const {mountVoiceFit}=await import('/montage-fit-controls.mjs');window.requests=[];
  const bad={id:'old',status:'ready',timing_mode:'phrases',proposal:{can_apply:false,conflicts:[{code:'unbound_quote',clip_id:'a'}],warnings:[],changes:[{old_start_ms:1235000,old_end_ms:1240000,new_start_ms:0,new_end_ms:1452982.857}],timeline:{duration_ms:1452982.857}}};
  localStorage.setItem('frameforge-voice-fit:imported','old');
  window.fit=mountVoiceFit(document.querySelector('main'),{id:'imported',importedPrompts:true,flush:async()=>{},pause:()=>{},getState:()=>({revision:1}),notice:()=>{},api:async(url,body)=>{
   if(body){window.requests.push(body);return {id:'new',status:'failed',timing_mode:body.timing_mode};}
   return url.endsWith('/old')?bad:{id:'new',status:'failed',timing_mode:'semantic'};
  }});
 });
 await page.getByText(/Подгонка не рассчитана:/).waitFor();
 assert.equal(await page.locator('table').count(),0);assert.equal(await page.getByRole('button',{name:'Применить подгонку',exact:true}).count(),0);
 assert.equal((await page.locator('main').innerText()).includes('1452.98'),false);
 await page.getByRole('button',{name:'Сопоставить сцены с озвучкой',exact:true}).click();
 await page.waitForFunction(()=>window.requests.length===1);assert.equal(await page.evaluate(()=>window.requests[0].timing_mode),'semantic');
 await page.evaluate(()=>window.fit.dispose());assert.deepEqual(errors,[]);
});
