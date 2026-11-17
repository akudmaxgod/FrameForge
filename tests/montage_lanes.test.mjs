import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {chromium} from 'playwright-core';
import {trimTimelineItem,reorderTimelineClip,timelineExtent} from '../imagegen/static/montage-lanes.mjs';
import {normalize} from '../imagegen/static/montage-timeline.mjs';
const root=new URL('../',import.meta.url).pathname.replace(/^\/(.:)/,'$1'),python=root+'/.venv/Scripts/python.exe';

test('audio edge edits preserve timing of remaining samples and clamp to source',()=>{
  const doc={clips:[],audio_tracks:[{role:'voice',offset_ms:2000,trim_in_ms:500,trim_out_ms:9500,source_duration_ms:10000}]};
  trimTimelineItem(doc,{kind:'voice',id:'voice',edge:'start'},1500);
  assert.deepEqual(doc.audio_tracks[0],{role:'voice',offset_ms:3500,trim_in_ms:2000,trim_out_ms:9500,source_duration_ms:10000});
  assert.equal(timelineExtent(doc),11000);
  trimTimelineItem(doc,{kind:'voice',id:'voice',edge:'start'},-9000);
  assert.equal(doc.audio_tracks[0].trim_in_ms,0);assert.equal(doc.audio_tracks[0].offset_ms,1500);
  trimTimelineItem(doc,{kind:'voice',id:'voice',edge:'end'},9000);assert.equal(doc.audio_tracks[0].trim_out_ms,10000);
  trimTimelineItem(doc,{kind:'voice',id:'voice',edge:'start'},90000);assert.equal(doc.audio_tracks[0].trim_out_ms-doc.audio_tracks[0].trim_in_ms,10);
  doc.audio_tracks[0].enabled=false;assert.equal(timelineExtent(doc),0);
});
test('video edits ripple following clips, retain one frame, and never alter sources',()=>{
  const doc={clips:['a','b','c'].map(id=>({edit_clip_id:id,source_video_asset_id:id,trim_in_ms:0,trim_out_ms:6000,source_duration_ms:6000})),transitions:[],audio_tracks:[]};
  trimTimelineItem(doc,{kind:'video',id:'a',edge:'end'},-2000);
  assert.equal(normalize(doc).duration_ms,16000);assert.equal(normalize(doc).clips[1].start_ms,4000);
  reorderTimelineClip(doc,'a','c');assert.deepEqual(doc.clips.map(c=>c.edit_clip_id),['b','a','c']);
  reorderTimelineClip(doc,'a');assert.deepEqual(doc.clips.map(c=>c.edit_clip_id),['b','c','a']);
  trimTimelineItem(doc,{kind:'video',id:'a',edge:'start'},9000);assert.ok(Math.abs(doc.clips[2].trim_out_ms-doc.clips[2].trim_in_ms-1000/24)<.001);
  assert.deepEqual(doc.clips.map(c=>c.source_video_asset_id),['b','c','a']);
});
function fixture(){return new Promise((resolve,reject)=>{
  const process=spawn(python,['tests/montage_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});let output='';const timeout=setTimeout(()=>{process.kill();reject(Error(output));},20000);
  process.stdout.on('data',data=>{output+=data;const m=output.match(/READY (\S+) (\S+) (.+)\r?\n/);if(m){clearTimeout(timeout);resolve({process,url:m[1],pid:m[2],voice:m[3].trim()});}});process.stderr.on('data',data=>output+=data);process.on('exit',code=>{clearTimeout(timeout);if(code)reject(Error(output));});
});}
async function drag(page,locator,dx){await locator.scrollIntoViewIfNeeded();const box=await locator.boundingBox();assert.ok(box);await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+dx,box.y+box.height/2,{steps:12});await page.mouse.up();}
async function saved(page){await page.waitForFunction(()=>document.querySelector('.montage-save')?.textContent.startsWith('Сохранено'));}
async function readDoc(page,pid){await saved(page);return page.evaluate(async id=>(await (await fetch(`/api/projects/${id}/edit`)).json()).document,pid);}
test('three lanes drag, crop, ruler, undo, persistence and real MP4 audio', {timeout:120000},async t=>{
  const f=await fixture();t.after(()=>f.process.kill());const dir=await mkdtemp(join(tmpdir(),'ff-lanes-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage({viewport:{width:1700,height:1200}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.locator('[data-view=export]').click();await page.getByRole('button',{name:'Добавить готовые сцены',exact:true}).click();
  await page.locator('.timeline-block-video').first().waitFor();assert.equal(await page.locator('.timeline-lane').count(),3);
  await page.getByLabel('Масштаб таймлайна').evaluate(input=>{input.value=100;input.dispatchEvent(new Event('input'));});await page.getByLabel('Привязка',{exact:true}).uncheck();
  const original=await readDoc(page,f.pid),first=original.clips[0].edit_clip_id;
  await drag(page,page.locator(`.timeline-block-video[data-item="${first}"]`),480);
  let doc=await readDoc(page,f.pid);assert.deepEqual(doc.clips.map(c=>c.edit_clip_id),[original.clips[1].edit_clip_id,original.clips[2].edit_clip_id,first]);
  await drag(page,page.locator('.timeline-block-video').first().locator('.timeline-trim-end'),-120);
  doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.clips[0].trim_out_ms-500)<5);
  await page.getByRole('button',{name:'↶ Отменить',exact:true}).click();await saved(page);doc=await readDoc(page,f.pid);assert.equal(doc.clips[0].trim_out_ms,1000);
  await page.getByRole('button',{name:'↷ Повторить',exact:true}).click();await saved(page);
  const voiceChooser=page.waitForEvent('filechooser');await page.locator('.timeline-lane-voice').getByRole('button',{name:'Загрузить озвучку',exact:true}).click();await (await voiceChooser).setFiles(f.voice);await page.locator('.timeline-block-voice').waitFor();
  await drag(page,page.locator('.timeline-block-voice'),180);await saved(page);
  await drag(page,page.locator('.timeline-block-voice .timeline-trim-start'),60);doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.audio_tracks[0].offset_ms-1000)<5,JSON.stringify(doc.audio_tracks));assert.ok(Math.abs(doc.audio_tracks[0].trim_in_ms-250)<5,JSON.stringify(doc.audio_tracks));
  const musicChooser=page.waitForEvent('filechooser');await page.locator('.timeline-label-music').getByRole('button',{name:'+ Файл',exact:true}).click();await (await musicChooser).setFiles(f.voice);await page.locator('.timeline-block-music').waitFor();await drag(page,page.locator('.timeline-block-music'),60);doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.audio_tracks.find(a=>a.role==='music').offset_ms-250)<5);
  await page.locator('[data-audio-role=voice]').getByLabel('Громкость',{exact:true}).fill('.5');await page.locator('[data-audio-role=voice]').getByLabel('Громкость',{exact:true}).dispatchEvent('change');await saved(page);
  // Escape cancels a gesture without creating a montage revision.
  const beforeCancel=await readDoc(page,f.pid);await page.locator('.timeline-block-voice').scrollIntoViewIfNeeded();const voiceBox=await page.locator('.timeline-block-voice').boundingBox();await page.mouse.move(voiceBox.x+voiceBox.width/2,voiceBox.y+20);await page.mouse.down();await page.mouse.move(voiceBox.x+voiceBox.width/2+100,voiceBox.y+20,{steps:8});await page.keyboard.press('Escape');await page.mouse.up();assert.deepEqual(await readDoc(page,f.pid),beforeCancel);
  await page.getByLabel('Привязка',{exact:true}).check();await drag(page,page.locator('.timeline-block-voice'),118);doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.audio_tracks.find(a=>a.role==='voice').offset_ms-1500)<5);
  await page.getByRole('button',{name:'↶ Отменить',exact:true}).click();await saved(page);await page.getByLabel('Привязка',{exact:true}).uncheck();
  const endHandle=page.locator('.timeline-block-video').first().locator('.timeline-trim-end');await endHandle.focus();await page.keyboard.press('ArrowRight');doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.clips[0].trim_out_ms-1000/24-500)<5);
  await page.getByRole('button',{name:'↶ Отменить',exact:true}).click();await saved(page);
  const ruler=await page.locator('.timeline-ruler').boundingBox();await page.mouse.click(ruler.x+288,ruler.y+8);
  await page.waitForFunction(()=>Math.abs(Number(document.querySelector('[aria-label="Время монтажа"]').value)-1200)<10);
  await page.getByRole('button',{name:'Экспортировать MP4',exact:true}).click();await page.getByRole('link',{name:/Скачать MP4 · версия/}).waitFor({timeout:45000});
  const url=await page.getByRole('link',{name:/Скачать MP4 · версия/}).getAttribute('href');const response=await page.request.get(f.url+url);assert.equal(response.status(),200);const file=join(dir,'result.mp4');await writeFile(file,await response.body());
  const verification=JSON.parse(execFileSync(python,['-c',"import av,numpy as np,json,sys; m=av.open(sys.argv[1]); v=m.streams.video[0]; duration=float(v.duration*v.time_base); s=np.concatenate([f.to_ndarray() for f in m.decode(audio=0)],axis=1); print(json.dumps({'duration':duration,'silence':float(np.max(np.abs(s[:,1000:6000]))),'music':float(np.mean(np.abs(s[:,18000:24000]))),'voice':float(np.mean(np.abs(s[:,51000:54000])))}))",file],{cwd:root}));
  assert.ok(Math.abs(verification.duration-2.5)<1/24);assert.ok(verification.silence<.01);assert.ok(verification.music>.01);assert.ok(verification.voice>verification.music*1.5);
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.locator('[data-view=export]').click();await page.locator('.timeline-block-voice').waitFor();doc=await readDoc(page,f.pid);assert.ok(Math.abs(doc.audio_tracks.find(a=>a.role==='voice').trim_in_ms-250)<5);assert.equal(doc.clips[2].edit_clip_id,first);
  await page.locator('[data-audio-role=voice]').getByLabel('Старт на шкале, с',{exact:true}).fill('5');await page.locator('[data-audio-role=voice]').getByLabel('Старт на шкале, с',{exact:true}).dispatchEvent('change');await saved(page);await page.locator('.timeline-duration-warning').getByText(/Озвучка длиннее/).waitFor();
  await page.getByLabel('Время монтажа',{exact:true}).evaluate(input=>{input.value=5200;input.dispatchEvent(new Event('input'));});await page.waitForFunction(()=>Math.abs(document.querySelector('.montage-stage audio').currentTime-.45)<.02);
  assert.equal(await page.locator('.montage-stage video').evaluateAll(v=>v.every(m=>m.style.visibility==='hidden')),true);
  await page.locator('[data-view=queue]').click();assert.equal(await page.locator('.timeline-canvas').count(),0);assert.deepEqual(errors,[]);
});


