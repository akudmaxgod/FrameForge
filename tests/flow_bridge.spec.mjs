import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright-core';
const script = await readFile(new URL('../google_flow_autopilot.user.js', import.meta.url), 'utf8');
test('active project, exact prompt, pause drains upload and excludes existing scenes', {timeout:30000}, async () => {
 const browser = await chromium.launch({headless:true, executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try {
 const page = await browser.newPage();
 await page.setContent('<form style="margin-top:400px"><textarea></textarea><button type="submit" aria-label="Generate">Generate</button></form>');
 await page.evaluate(() => {
  window.calls=[];
  window.GM_xmlhttpRequest = options => {
   const path = new URL(options.url).pathname;
   calls.push({path, data:options.data});
   let result={};
   if(path.endsWith('/current')) result={project_id:'active'};
   else if(path.endsWith('/prompts')) result={characters:[],locations:[],scenes:[{scene_id:'old',has_image:true,prompt:'skip'},{scene_id:'new',prompt:'Exact full prompt'},{scene_id:'later',prompt:'later'}]};
   else if(path==='/api/projects/active') result={title:'Active project'};
   options.onload({status:200,responseText:JSON.stringify(result)});
  };
  document.querySelector('form').onsubmit = e => {
   e.preventDefault(); window.submitted=document.querySelector('textarea').value;
   document.getElementById('ff-btn-start').click();
   const canvas=document.createElement('canvas');canvas.width=512;canvas.height=512;
   canvas.getContext('2d').fillRect(0,0,512,512);
   const img=new Image();img.src=canvas.toDataURL();document.body.append(img);
  };
 });
 await page.addScriptTag({content:script});
 await page.locator('#ff-btn-start').click();
 await page.waitForFunction(() => document.getElementById('ff-status').textContent.startsWith('Пауза.'));
 assert.equal(await page.evaluate(()=>submitted),'Exact full prompt');
 const calls=await page.evaluate(()=>calls);
 assert.deepEqual(calls.filter(c=>c.path.endsWith('/upload')).map(c=>c.path),['/api/scenes/new/upload']);
 assert.ok(!calls.some(c=>c.path.includes('native_submit')||c.path==='/api/projects'));
 } finally {await browser.close();}
});
test('distributed userscript matches source',async()=>{
 assert.equal(await readFile(new URL('../imagegen/static/google_flow_autopilot.user.js',import.meta.url),'utf8'),script);
});
