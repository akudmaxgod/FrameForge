import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {chromium} from 'playwright-core';
import {smartMotion} from '../imagegen/static/smart-motion.mjs';
import {beginAnimationRun} from '../imagegen/static/animation-completion.mjs';
const root=new URL('../',import.meta.url).pathname.replace(/^\/(.:)/,'$1'),python=root+'/.venv/Scripts/python.exe';
test('smart camera rules agree in browser and server',()=>{
  for(const scene of [{shot:'Крупный план лица'},{shot:'Wide establishing shot'},{beat:'Он замечает ключ'},{visible_people:3},{}, {shot:'Деталь письма',beat:'Он покидает дом'}]){
    const result=JSON.parse(execFileSync(python,['-c','import json,sys; from imagegen.animation import smart_ken_burns_motion; print(json.dumps(smart_ken_burns_motion(json.load(sys.stdin),3),ensure_ascii=False))'],{cwd:root,input:JSON.stringify(scene),encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'}}));
    assert.deepEqual(smartMotion(scene,3),result);
  }
});
function fixture(){return new Promise((resolve,reject)=>{
  const child=spawn(python,['tests/ui_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});let output='';const timeout=setTimeout(()=>{child.kill();reject(Error(output));},20000);
  child.stdout.on('data',data=>{output+=data;const m=output.match(/READY (\S+) (\S+)/);if(m){clearTimeout(timeout);resolve({child,url:m[1],pid:m[2]});}});child.stderr.on('data',data=>output+=data);
});}

test('completed clip shows measured eight seconds while retaining the six second request',{timeout:60000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{
    const response=await route.fetch(),p=await response.json();
    p.scenes[0].video_asset_id='duration-preview';p.scenes[0].video_metadata={duration_seconds:8,width:1280,height:720};
    p.scenes[0].data.animation={selected_engine:'omni_flash',status:'completed',duration_seconds:6,omni:{duration_seconds:6,resolution:'720p'}};
    await route.fulfill({json:p});
  });
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);
  await page.getByTestId('scene-anim-badge-1').waitFor();
  assert.match(await page.getByTestId('scene-anim-badge-1').innerText(),/8 сек/);
  assert.equal(await page.getByTestId('animation-duration-hint-1').innerText(),'Запрошено 6 сек. · Flow вернул 8 сек.');
});
test('local clips do not inherit a Flow duration warning from old Veo settings',{timeout:60000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{
    const response=await route.fetch(),p=await response.json();
    p.scenes[0].video_asset_id='local-duration-preview';p.scenes[0].video_metadata={duration_seconds:6};
    p.scenes[0].data.animation={selected_engine:'ken_burns',status:'completed',duration_seconds:6,ken_burns_preset:'gentle_pan_left',veo:{duration_seconds:5}};
    await route.fulfill({json:p});
  });
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('scene-anim-badge-1').waitFor();
  assert.match(await page.getByTestId('scene-anim-badge-1').innerText(),/Спокойный проход влево/);
  assert.equal(await page.getByTestId('animation-duration-hint-1').count(),0);
});
test('completion notification waits for cloud, survives reload, and stays until dismissed',{timeout:60000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const source=await (await fetch(f.url+'/api/projects/'+f.pid)).json(),run=beginAnimationRun(null,source.scenes.slice(0,2));
  let stage=0;
  const page=await browser.newPage();
  await page.addInitScript(({pid,run})=>{if(!localStorage.getItem('frameforge-animation-run:'+pid))localStorage.setItem('frameforge-animation-run:'+pid,JSON.stringify(run));},{pid:f.pid,run});
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{
    const p=structuredClone(source);
    p.scenes[0].data.animation={selected_engine:'omni_flash',status:'queued_omni'};
    p.scenes[1].data.animation={selected_engine:'ken_burns',status:'completed'};
    p.scenes[1].video_asset_id='local-finished';
    if(stage){p.scenes[0].video_asset_id='cloud-finished';p.scenes[0].data.animation.status='completed';}
    await route.fulfill({json:p});
  });
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('scene-anim-badge-1').waitFor();
  assert.equal(await page.getByTestId('animation-completion').count(),0);
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('scene-anim-badge-1').waitFor();
  assert.equal(await page.getByTestId('animation-completion').count(),0);
  // The project picker still has focus. Completion must keep polling anyway.
  stage=1;await page.getByTestId('animation-completion').waitFor({timeout:12000});
  assert.match(await page.getByTestId('animation-completion').innerText(),/Анимации готовы.*Готово 2 из 2/s);
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('animation-completion').waitFor();
  assert.match(await page.getByTestId('animation-completion').innerText(),/Анимации готовы/);
  await page.getByTestId('animation-completion-dismiss').click();assert.equal(await page.getByTestId('animation-completion').count(),0);
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('scene-anim-badge-1').waitFor();
  assert.equal(await page.getByTestId('animation-completion').count(),0);
});
test('a failed clip produces an attention notification instead of all-ready',{timeout:30000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const source=await (await fetch(f.url+'/api/projects/'+f.pid)).json(),run=beginAnimationRun(null,source.scenes.slice(0,2)),page=await browser.newPage();
  await page.addInitScript(({pid,run})=>localStorage.setItem('frameforge-animation-run:'+pid,JSON.stringify(run)),{pid:f.pid,run});
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{
    const p=structuredClone(source);p.scenes[0].data.animation={selected_engine:'omni_flash',status:'failed',error:'Flow unavailable'};p.scenes[1].video_asset_id='local';
    await route.fulfill({json:p});
  });
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('animation-completion').waitFor();
  assert.match(await page.getByTestId('animation-completion').innerText(),/завершена с ошибками.*Готово 1 из 2.*кадры: 1/s);
  assert.doesNotMatch(await page.getByTestId('animation-completion').innerText(),/Анимации готовы/);
});
test('a stopped Flow bridge offers continuation without marking queued clips ready',{timeout:30000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const source=await (await fetch(f.url+'/api/projects/'+f.pid)).json(),page=await browser.newPage(),commands=[];
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{const p=structuredClone(source);p.scenes[0].data.animation={selected_engine:'omni_flash',status:'queued_omni'};await route.fulfill({json:p});});
  await page.route('**/api/settings',async route=>{const response=await route.fetch();const settings=await response.json();settings.image_backend='flow';await route.fulfill({json:settings});});
  await page.route('**/api/bridge/v1/status',route=>route.fulfill({json:{connected:true,running:false,project_id:f.pid,status:'Не удалось сохранить медиафайл в галерее'}}));
  await page.route('**/api/bridge/v1/command',async route=>{commands.push(route.request().postDataJSON());await route.fulfill({json:{ok:true}});});
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('animation-interrupted').waitFor({timeout:15000});
  assert.match(await page.getByTestId('animation-interrupted').innerText(),/приостановлена.*Готово 0 из 1/s);
  assert.equal(await page.getByTestId('animation-completion').count(),0);
  await page.getByTestId('animation-interrupted-resume').click();assert.deepEqual(commands[0].scene_ids,[source.scenes[0].id]);
});
test('cloud model picker keeps choices, persists Veo, and scopes Flow commands',{timeout:90000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('omni-select-1').waitFor();
  await page.getByTestId('animation-open-selection').click();assert.equal(await page.getByTestId('animation-model-toolbar').evaluate(n=>n===document.activeElement),true);assert.equal(await page.locator('.batch-popover').count(),0);
  await page.getByTestId('omni-select-1').check();await page.getByTestId('omni-select-3').check();
  await page.getByTestId('animation-model-sidebar').selectOption('veo_lite');
  assert.equal(await page.getByTestId('animation-model-toolbar').inputValue(),'veo_lite');
  assert.equal(await page.getByTestId('omni-select-1').isChecked(),true);assert.equal(await page.getByTestId('omni-select-3').isChecked(),true);
  assert.match(await page.getByTestId('animation-selection-summary').textContent(),/Veo 3.1 Lite: 2 · 2.5D: 2/);
  assert.match(await page.getByTestId('scene-anim-badge-1').textContent(),/Выбран Veo 3.1 Lite/);assert.match(await page.getByTestId('scene-anim-badge-2').textContent(),/2.5D/);
  await page.getByTestId('btn-animation-plan').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Выбор сохранён'));
  const saved=await (await fetch(f.url+'/api/projects/'+f.pid)).json();assert.deepEqual(saved.scenes.map(s=>s.data.animation.selected_engine),['veo_lite','ken_burns','veo_lite','ken_burns']);assert.equal(saved.animation_settings.selected_animation_engine,'veo_lite');
  // Loading the saved project on a fresh browser must reconstruct both choices.
  await page.evaluate(()=>localStorage.clear());await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('omni-select-1').waitFor();
  assert.equal(await page.getByTestId('animation-model-sidebar').inputValue(),'veo_lite');assert.equal(await page.getByTestId('omni-select-3').isChecked(),true);
  await page.getByTestId('scene-animation-disclosure-1').evaluate(n=>n.open=true);await page.getByTestId('veo-motion-1').selectOption('reaction');
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('движение Veo сохранено'));
  if(process.env.FRAMEFORGE_MODEL_SCREENSHOT){await page.getByTestId('scene-animation-disclosure-1').evaluate(n=>n.open=true);await page.locator('.animation-selection-bar').scrollIntoViewIfNeeded();await page.screenshot({path:process.env.FRAMEFORGE_MODEL_SCREENSHOT});}
  const generated=[],commands=[];
  await page.route('**/animation/generate',async route=>{const request=route.request().postDataJSON();generated.push(request);await route.fulfill({json:{scenes:request.scene_ids.map(id=>({scene_id:id,engine:[saved.scenes[0].id,saved.scenes[2].id].includes(id)?'veo_lite':'ken_burns',status:[saved.scenes[0].id,saved.scenes[2].id].includes(id)?'queued_veo':'completed'}))}});});
  await page.route('**/bridge/v1/command',async route=>{commands.push(route.request().postDataJSON());await route.fulfill({json:{command_id:'test-only'}});});
  await page.getByTestId('scene-animation-disclosure-1').evaluate(n=>n.open=true);await page.getByTestId('btn-animate-veo-1').click();
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Кадр 1: Veo 3.1 Lite отправлен'));
  assert.deepEqual(generated[0].scene_ids,[saved.scenes[0].id]);assert.deepEqual(commands[0].scene_ids,[saved.scenes[0].id]);
  await page.getByTestId('btn-animation-generate').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Готово 2.5D: 2'));
  assert.deepEqual(generated.slice(1).map(r=>r.scene_ids),[[saved.scenes[0].id,saved.scenes[2].id],[saved.scenes[1].id],[saved.scenes[3].id]]);assert.deepEqual(commands[1].scene_ids,[saved.scenes[0].id,saved.scenes[2].id]);
  await page.unroute('**/animation/generate');await page.route('**/animation/generate',route=>route.fulfill({json:{scenes:saved.scenes.map(s=>({scene_id:s.id,engine:s.data.animation.selected_engine,status:'failed',error:'Кадр не готов к Flow.'}))}}));
  await page.getByTestId('btn-animation-generate').click();await page.getByTestId('animation-completion').waitFor();assert.match(await page.getByTestId('animation-completion').innerText(),/завершена с ошибками.*Готово 0 из 4/s);assert.equal(commands.length,2);
  await page.getByTestId('animation-model-toolbar').selectOption('omni_flash');assert.equal(await page.getByTestId('animation-model-sidebar').inputValue(),'omni_flash');
  assert.equal(await page.getByTestId('omni-select-1').isChecked(),true);assert.equal(await page.getByTestId('omni-select-3').isChecked(),true);assert.deepEqual(errors,[]);
});
test('direct Omni choices persist, remaining frames use auto camera, local launch stays local',{timeout:90000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('omni-select-1').waitFor();
  assert.equal(await page.getByTestId('omni-select-1').isChecked(),false);assert.match(await page.getByTestId('scene-anim-badge-1').textContent(),/2.5D/);
  await page.getByTestId('omni-select-1').check();await page.getByTestId('omni-select-3').check();
  assert.match(await page.getByTestId('animation-selection-summary').textContent(),/Omni: 2 · 2.5D: 2/);
  await page.reload();await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('omni-select-1').waitFor();assert.equal(await page.getByTestId('omni-select-1').isChecked(),true);
  await page.getByTestId('btn-animation-plan').click();await page.waitForFunction(()=>document.querySelector('#notice')?.textContent.includes('Выбор сохранён'));
  const project=await (await fetch(f.url+'/api/projects/'+f.pid)).json();assert.deepEqual(project.scenes.map(s=>s.data.animation.selected_engine),['omni_flash','ken_burns','omni_flash','ken_burns']);
  await page.getByTestId('scene-animation-disclosure-2').evaluate(n=>n.open=true);assert.equal(await page.getByTestId('ken-burns-motion-2').inputValue(),'auto');
  await page.getByTestId('ken-burns-motion-2').selectOption('gentle_pull_back');await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('движение 2.5D сохранено'));
  await page.getByTestId('scene-animation-disclosure-2').evaluate(n=>n.open=true);await page.getByTestId('ken-burns-motion-2').selectOption('auto');await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('движение 2.5D сохранено'));
  if(process.env.FRAMEFORGE_UI_SCREENSHOT){await page.getByTestId('scene-animation-disclosure-2').evaluate(n=>n.open=true);await page.locator('.scene-card').nth(1).locator('.scene-animation-control').screenshot({path:process.env.FRAMEFORGE_UI_SCREENSHOT});}
  const bridgeCommands=[];page.on('request',r=>{if(r.url().endsWith('/bridge/v1/command'))bridgeCommands.push(r.url());});
  await page.getByTestId('scene-animation-disclosure-2').evaluate(n=>n.open=true);await page.getByTestId('btn-animate-kb-2').click();
  await page.getByTestId('animation-completion').waitFor({timeout:45000});
  assert.match(await page.getByTestId('animation-completion').innerText(),/Анимации готовы.*Готово 1 из 1/s);
  const after=await (await fetch(f.url+'/api/projects/'+f.pid)).json();assert.ok(after.scenes[1].video_asset_id);assert.equal(after.scenes[0].video_asset_id,null);assert.equal(after.scenes[2].video_asset_id,null);assert.deepEqual(bridgeCommands,[]);
  assert.equal(await page.getByTestId('omni-select-2').isDisabled(),true);assert.deepEqual(errors,[]);
  const video=page.locator('.scene-video').first();assert.equal(await video.getAttribute('preload'),'none');assert.equal(await video.evaluate(v=>v.autoplay),false);
  await video.evaluate(v=>{window.selectionPreview=v;return v.play();});await page.waitForFunction(()=>window.selectionPreview.currentTime>0);
  await page.locator('[data-view=queue]').click();assert.equal(await page.evaluate(()=>window.selectionPreview.paused&&!window.selectionPreview.getAttribute('src')),true);
});
test('visible one-frame trial queues only that frame and leaves bulk drafts and other revisions intact',{timeout:60000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());
  const page=await browser.newPage({viewport:{width:1500,height:1050}}),commands=[],saves=[],generated=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(r.url().endsWith('/animation/selection'))saves.push(r.postDataJSON());if(r.url().endsWith('/animation/generate'))generated.push(r.postDataJSON());});
  await page.route('**/bridge/v1/command',async route=>{commands.push(route.request().postDataJSON());await route.fulfill({json:{command_id:'test-only'}});});
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('animation-test-cloud-2').waitFor();
  await page.getByTestId('omni-select-1').check();await page.getByTestId('omni-select-3').check();
  assert.equal(await page.getByTestId('omni-select-2').isChecked(),false);
  const before=await (await fetch(f.url+'/api/projects/'+f.pid)).json();
  assert.equal(await page.getByTestId('scene-animation-disclosure-2').evaluate(n=>n.open),false);
  if(process.env.FRAMEFORGE_TRIAL_SCREENSHOT){const control=page.locator('.scene-card').nth(1).locator('.scene-animation-control');await control.evaluate(n=>n.scrollIntoView({block:'center'}));await control.screenshot({path:process.env.FRAMEFORGE_TRIAL_SCREENSHOT});}
  await page.getByTestId('animation-test-cloud-2').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Кадр 2: Omni отправлен'));
  const after=await (await fetch(f.url+'/api/projects/'+f.pid)).json();
  assert.deepEqual(saves,[]);assert.deepEqual(generated,[{scene_ids:[before.scenes[1].id]}]);assert.deepEqual(commands.map(x=>x.scene_ids),[[before.scenes[1].id]]);
  assert.equal(after.scenes[1].data.animation.status,'queued_omni');
  for(const i of [0,2,3])assert.deepEqual(after.scenes[i],before.scenes[i]);
  assert.equal(await page.getByTestId('omni-select-1').isChecked(),true);assert.equal(await page.getByTestId('omni-select-3').isChecked(),true);
  assert.equal(await page.getByTestId('omni-select-2').isDisabled(),true);assert.deepEqual(errors,[]);
});
test('one-frame trial explains approval requirement before sending anything',{timeout:30000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage();
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{const response=await route.fetch();const p=await response.json();p.channel_profile={...(p.channel_profile||{}),profile_type:'forgotten_cities'};p.scenes=p.scenes.map(s=>({...s,still_approval:{status:'pending'}}));await route.fulfill({json:p});});
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('animation-test-cloud-1').waitFor();
  assert.equal(await page.getByTestId('animation-test-cloud-1').isDisabled(),true);assert.equal(await page.getByTestId('animation-test-local-1').isDisabled(),true);
  assert.match(await page.locator('.scene-card').first().locator('.animation-single-hint').textContent(),/Утвердить/);
});
test('sparse Omni selection and bulk reset remain usable with 232 frames',{timeout:60000},async t=>{
  const f=await fixture();t.after(()=>f.child.kill());const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});t.after(()=>browser.close());const page=await browser.newPage();
  await page.route(f.url+'/api/projects/'+f.pid,async route=>{const response=await route.fetch();const p=await response.json();p.scenes=Array.from({length:232},(_,i)=>({...p.scenes[1],id:'long-'+i,position:i+1,video_asset_id:null}));await route.fulfill({json:p});});
  await page.goto(f.url);await page.locator('#project-select').selectOption(f.pid);await page.getByTestId('omni-select-232').waitFor();
  assert.equal(await page.locator('[data-testid^="omni-select-"]').count(),232);await page.getByTestId('omni-select-1').check();await page.getByTestId('omni-select-232').check();
  assert.match(await page.getByTestId('animation-selection-summary').textContent(),/Omni: 2 · 2.5D: 230/);
  await page.getByTestId('animation-select-none').click();assert.match(await page.getByTestId('animation-selection-summary').textContent(),/Omni: 0 · 2.5D: 232/);
  await page.getByTestId('animation-select-all').click();assert.match(await page.getByTestId('animation-selection-summary').textContent(),/Omni: 232 · 2.5D: 0/);
});
