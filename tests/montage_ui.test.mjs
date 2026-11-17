import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {chromium} from 'playwright-core';
import {normalize,opacity} from '../imagegen/static/montage-timeline.mjs';
const root=new URL('../',import.meta.url).pathname.replace(/^\/(.:)/,'$1');
const python=root+'/.venv/Scripts/python.exe';
test('Python and browser use identical timeline and transition opacity',()=>{
  const document={clips:[{edit_clip_id:'a',trim_in_ms:250,trim_out_ms:1250},{edit_clip_id:'b',trim_in_ms:0,trim_out_ms:100},{edit_clip_id:'c',trim_in_ms:0,trim_out_ms:900}],transitions:[{left:'a',right:'b',kind:'dissolve',duration_ms:500},{left:'b',right:'c',kind:'black',duration_ms:500}],audio_tracks:[]};
  const result=JSON.parse(execFileSync(python,['-c','import sys,json; from imagegen.editing import normalize; print(json.dumps(normalize(json.load(sys.stdin))))'],{cwd:root,input:JSON.stringify(document)}));
  assert.deepEqual(normalize(document),result);
  assert.equal(opacity(result.clips[2],result.clips[2].start_ms),0);
});
function fixture(){return new Promise((resolve,reject)=>{
  const process=spawn(python,['tests/montage_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});let output='';
  const timeout=setTimeout(()=>{process.kill();reject(Error(output));},20000);
  process.stdout.on('data',data=>{output+=data;const match=output.match(/READY (\S+) (\S+) (.+)\r?\n/);if(match){clearTimeout(timeout);resolve({process,url:match[1],pid:match[2],voice:match[3].trim()});}});
  process.stderr.on('data',data=>output+=data);process.on('exit',code=>{clearTimeout(timeout);if(code)reject(Error(output));});
});}
test('montage edits, preview, audio, save, export and tab cleanup', {timeout:90000},async t=>{
  const f=await fixture();t.after(()=>f.process.kill());
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1500,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.locator('[data-view=export]').click();
  await page.getByRole('button',{name:'Добавить готовые сцены',exact:true}).click();await page.getByText('Список сцен',{exact:true}).click();await page.locator('.montage-clip').first().waitFor();
  assert.equal(await page.locator('.montage-stage video').count(),2);assert.equal(await page.locator('.montage-clip').count(),3);
  await page.locator('.montage-clip').first().click();await page.getByLabel('Начало в исходнике, с',{exact:true}).fill('0.25');await page.getByLabel('Начало в исходнике, с',{exact:true}).dispatchEvent('change');
  await page.getByRole('button',{name:'↶ Отменить',exact:true}).click();assert.equal(await page.getByLabel('Начало в исходнике, с',{exact:true}).inputValue(),'0');
  await page.getByRole('button',{name:'↷ Повторить',exact:true}).click();
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.montage-save')?.textContent.startsWith('Сохранено'));
  // Inspector bindings must continue to edit the live document after saving.
  await page.getByLabel('Начало в исходнике, с',{exact:true}).fill('0.2');await page.getByLabel('Начало в исходнике, с',{exact:true}).dispatchEvent('change');
  await page.getByRole('button',{name:'↶ Отменить',exact:true}).click();assert.equal(await page.getByLabel('Начало в исходнике, с',{exact:true}).inputValue(),'0.25');
  await page.getByRole('button',{name:'▶ Воспроизвести',exact:true}).click();await page.waitForFunction(()=>Number(document.querySelector('input[aria-label="Время монтажа"]').value)>150);
  await page.getByRole('button',{name:'Ⅱ Пауза',exact:true}).click();
  assert.equal(await page.locator('.montage-stage video').evaluateAll(v=>v.every(m=>m.paused)),true);
  await page.getByLabel('Загрузить озвучку').setInputFiles(f.voice);await page.locator('.montage-track').first().getByText('Длительность файла:',{exact:false}).waitFor();
  await page.getByLabel('Громкость',{exact:true}).first().fill('0.5');await page.getByLabel('Громкость',{exact:true}).first().dispatchEvent('change');
  await page.getByLabel('Время монтажа',{exact:true}).evaluate(input=>{input.value=500;input.dispatchEvent(new Event('input'));});
  await page.waitForFunction(()=>Math.abs(document.querySelector('.montage-stage audio').currentTime-.5)<.05);
  assert.equal(await page.locator('.montage-stage audio').evaluateAll(a=>a.every(m=>m.paused)),true);
  await page.getByRole('button',{name:'Экспортировать MP4',exact:true}).click();await page.getByRole('link',{name:/Скачать MP4 · версия/}).waitFor({timeout:45000});
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.locator('[data-view=export]').click();await page.getByText('Список сцен',{exact:true}).click();await page.locator('.montage-clip').first().click();
  assert.equal(await page.getByLabel('Начало в исходнике, с',{exact:true}).inputValue(),'0.25');
  await page.evaluate(async pid=>{const session=await (await fetch('/api/session')).json();const edit=await (await fetch(`/api/projects/${pid}/edit`)).json();edit.document.clips[0].volume=.8;await fetch(`/api/projects/${pid}/edit`,{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':session.token},body:JSON.stringify({expected_revision:edit.revision,document:edit.document})});},f.pid);
  await page.getByLabel('Начало в исходнике, с',{exact:true}).fill('0.3');await page.getByLabel('Начало в исходнике, с',{exact:true}).dispatchEvent('change');
  await page.waitForFunction(()=>document.querySelector('.montage-save').textContent.includes('другой вкладке'));
  assert.equal(await page.evaluate(pid=>JSON.parse(localStorage.getItem('frameforge-edit:'+pid)).document.clips[0].trim_in_ms,f.pid),300);
  await page.getByRole('button',{name:'Перечитать с сервера',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.montage-save').textContent.startsWith('Сохранено'));
  assert.equal(await page.getByLabel('Начало в исходнике, с',{exact:true}).inputValue(),'0.25');
  assert.equal(await page.evaluate(pid=>JSON.parse(localStorage.getItem('frameforge-edit:'+pid+':recovery')).clips[0].trim_in_ms,f.pid),300);
  await page.evaluate(()=>window.testMontageMedia=[...document.querySelectorAll('.montage-stage video,.montage-stage audio')]);
  await page.locator('[data-view=queue]').click();assert.equal(await page.locator('.montage-stage video').count(),0);
  assert.equal(await page.evaluate(()=>window.testMontageMedia.every(m=>m.paused&&!m.getAttribute('src'))),true);
  assert.deepEqual(errors,[]);
});
test('232 clips render a bounded window and two video elements',{timeout:45000},async t=>{
  const f=await fixture();t.after(()=>f.process.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage();await page.goto(f.url);
  await page.evaluate(async pid=>{
    const project=await (await fetch('/api/projects/'+pid)).json(),prototype=project.scenes[0];
    const document={schema_version:1,clips:Array.from({length:232},(_,i)=>({edit_clip_id:'large-'+i,scene_id:prototype.id,source_video_asset_id:prototype.video_asset_id,trim_in_ms:0,trim_out_ms:1000,source_duration_ms:1000,volume:1,muted:true})),transitions:[],audio_tracks:[]};
    const {mountMontage}=await import('/montage-editor.mjs');const host=window.document.createElement('div');host.id='scale-test';window.document.body.append(host);window.scaleEditor=await mountMontage(host,{project:{...project,id:'scale-fixture'},api:async()=>({revision:1,document,sources:{}}),downloadZip:()=>{},notice:()=>{}});
  },f.pid);
  assert.equal(await page.locator('#scale-test .montage-clip').count(),32);assert.equal(await page.locator('#scale-test video').count(),2);
  assert.ok(await page.locator('#scale-test .timeline-block-video').count()<232);
  await page.locator('#scale-test .timeline-scroll').evaluate(n=>n.scrollLeft=n.scrollWidth);
  await page.locator('#scale-test .timeline-block-video[data-item="large-231"]').waitFor();
  assert.ok(await page.locator('#scale-test .timeline-block-video').count()<232);
  await page.setViewportSize({width:800,height:1000});
  assert.equal(await page.locator('#scale-test .timeline-lane').count(),3);
  const sizes=await page.locator('#scale-test .montage-timeline').evaluate(n=>({client:n.clientWidth,scroll:n.scrollWidth}));assert.ok(sizes.scroll<=sizes.client+2);
  await page.locator('#scale-test').getByText('Список сцен',{exact:true}).click();
  for(let i=0;i<7;i++)await page.locator('#scale-test').getByRole('button',{name:'Лента ›',exact:true}).click();
  assert.equal(await page.locator('#scale-test .montage-clip').count(),8);assert.equal(await page.locator('#scale-test video').count(),2);
  await page.evaluate(()=>window.scaleEditor.dispose());
});

