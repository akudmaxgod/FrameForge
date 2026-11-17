import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
const code=await readFile(new URL('../integrations/flow-v1/frameforge-flow.user.js',import.meta.url),'utf8');
function fixture(initial=null,projectId="p",flowSettings={}) {
 const nodes=[], events={}, calls=[], sent=[]; const values=new Map([["ff-flow-v1-state",initial]]); let stored=initial;
 const peer={postMessage(m){sent.push(m);}};
 const context={URL,console,setTimeout,clearTimeout,setInterval:()=>0,crypto:{randomUUID:()=>String(sent.length+1)},location:{pathname:'/project/flow/tool/tool'},
 GM_getValue:(key,fallback)=>values.has(key)?values.get(key):fallback,GM_setValue:(key,v)=>{stored=JSON.parse(JSON.stringify(v));values.set(key,stored);},
 GM_xmlhttpRequest:o=>{calls.push(o);o.onload({status:200,responseText:JSON.stringify(o.url.endsWith('/current')?{project_id:projectId}:o.url.endsWith('/settings')?flowSettings:o.url.endsWith('/prompts')?{characters:[],locations:[],scenes:[{scene_id:'s',prompt:'scene'}]}:{})});},
 window:{addEventListener:(name,fn)=>events[name]=fn},document:{body:{append(){}},createElement:()=>{const n={style:{},append(){},addEventListener:(name,fn)=>n[name]=fn};nodes.push(n);return n;}}};
 vm.runInNewContext(code,context);
 const message=(data,origin='https://app.scf.usercontent.goog',source=peer)=>events.message({origin,source,data:{channel:'frameforge.bridge.v1',...data}});
 const tick=()=>new Promise(r=>setImmediate(r));
 return {nodes,calls,sent,message,tick,values,bridge:context.FFBridgeQueue,setProject:id=>projectId=id,get stored(){return stored;}};
}
test('Omni requests identify the model to the hosted adapter and retain six seconds',()=>{
 const f=fixture();
 const original={kind:'scene_video',model:'gemini-omni-1.1-flash',duration_seconds:6,aspect_ratio:'9:16',requestId:'existing',idempotency_key:'same'};
 const result=f.bridge.videoBridgeJob(original);
 assert.equal(result.modelDisplayName,'Omni 1.1 Flash');assert.equal(result.duration_seconds,6);assert.equal(result.aspectRatio,'9:16');
 assert.equal(result.requestId,'existing');assert.equal(result.idempotency_key,'same');assert.equal(original.modelDisplayName,undefined);
});
test('rejects foreign ready, verifies ping and current project',async()=>{
 const f=fixture();f.message({type:'ready'},'https://evil.test');await f.nodes[3].click();assert.equal(f.sent.length,0);
 f.message({type:'ready'});const check=f.nodes[3].click();await f.tick();assert.equal(f.sent[0].type,'ping');f.message({type:'pong',requestId:f.sent[0].requestId});await check;assert.ok(f.calls[0].url.endsWith('/current'));
});
test('pause keeps single runner and drains result into exact scene',async()=>{
 const f=fixture();f.message({type:'ready'});const run=f.nodes[2].click();await f.tick();assert.equal(f.sent.length,1);
 f.nodes[4].click();await f.nodes[2].click();assert.equal(f.sent.length,1);
 f.message({type:'result',requestId:f.sent[0].requestId,result:{mediaId:'m',mimeType:'image/png',base64:'AA=='}});await run;
 assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,1);assert.ok(f.calls.some(c=>c.url.endsWith('/scenes/s/upload')));assert.deepEqual(f.stored.done,['scenes:s']);assert.equal(f.stored.job,null);
});
test('unconfirmed generation is not resubmitted after restart',async()=>{
 const f=fixture({projectId:'p',flowProject:'flow',refs:{},done:[],job:{requestId:'old'},result:null});f.message({type:'ready'});await f.nodes[2].click();assert.equal(f.sent.length,0);assert.match(f.nodes[1].textContent,/не подтверждена/);
});
test('edited stale scene is regenerated even when its id remains in local done history',async()=>{
 const f=fixture({projectId:'p',flowProject:'flow',refs:{},done:['scenes:s'],job:null,result:null});
 f.message({type:'ready'});const run=f.nodes[2].click();await f.tick();
 assert.equal(f.sent.length,1);assert.equal(f.sent[0].id,'s');
 f.message({type:'result',requestId:f.sent[0].requestId,result:{mediaId:'fresh',mimeType:'image/png',base64:'AA=='}});
 await run;assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/scenes/s/upload')).length,1);
});
test('result from an unrelated origin cannot upload into FF',async()=>{
 const f=fixture();f.message({type:'ready'});const run=f.nodes[2].click();await f.tick();
 const result={type:'result',requestId:f.sent[0].requestId,result:{mediaId:'m',mimeType:'image/png',base64:'AA=='}};
 f.message(result,'https://evil.test');await f.tick();assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,0);
 f.nodes[4].click();f.message(result);await run;assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,1);
});

test('switching projects retains the earlier unfinished result',async()=>{
 const old={projectId:'old',flowProject:'flow',refs:{},done:[],job:{kind:'scenes',id:'old-scene',requestId:'old-job'},result:{mediaId:'old-media',mimeType:'image/png',base64:'AA=='}};
 const f=fixture(old,'new');f.message({type:'ready'});
 const run=f.nodes[2].click();await f.tick();
 assert.equal(f.sent.length,1);
 assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,0);
 f.nodes[4].click();f.message({type:'result',requestId:f.sent[0].requestId,result:{mediaId:'new-media',mimeType:'image/png',base64:'AA=='}});await run;
 assert.equal(f.values.get('ff-flow-v1-state').result.mediaId,'old-media');
 assert.deepEqual(f.values.get('ff-flow-v1-state:new:flow').done,['scenes:s']);
});

test('props queue processes in order (characters -> locations -> props -> scenes) and attaches mediaId only to scenes with active prop', async () => {
 const customPrompts = {
  characters: [{id:'c1', name:'Hero', prompt:'Hero portrait', locked:false}],
  locations: [{id:'l1', name:'House', prompt:'House environment', locked:false}],
  props: [{id:'p1', name:'Pot', prompt:'Pot item', locked:false}],
  scenes: [
   {scene_id:'s1', position:1, prompt:'Hero in house without pot', flow_prompt:'Hero in house without pot', has_image:false, references:[{id:'c1', type:'character', name:'Hero'}, {id:'l1', type:'location', name:'House'}]},
   {scene_id:'s2', position:2, prompt:'Hero in house wearing pot', flow_prompt:'Hero in house wearing pot', has_image:false, references:[{id:'c1', type:'character', name:'Hero'}, {id:'l1', type:'location', name:'House'}, {id:'p1', type:'prop', name:'Pot'}]}
  ]
 };

 const nodes=[], events={}, calls=[], sent=[]; const values=new Map(); let stored=null;
 const peer={postMessage(m){sent.push(m);}};
 const context={
  URL,console,setTimeout,clearTimeout,setInterval:()=>0,crypto:{randomUUID:()=>String(sent.length+1)},location:{pathname:'/project/flow/tool/tool'},
  GM_getValue:(key,fallback)=>values.has(key)?values.get(key):fallback,
  GM_setValue:(key,v)=>{stored=JSON.parse(JSON.stringify(v));values.set(key,stored);},
  GM_xmlhttpRequest:o=>{
   calls.push(o);
   let data={};
   if(o.url.endsWith('/current')) data={project_id:'proj-props'};
   else if(o.url.endsWith('/prompts')) data=customPrompts;
   else if(o.url.endsWith('/upload')) data={status:'ok'};
   o.onload({status:200, responseText:JSON.stringify(data)});
  },
  window:{addEventListener:(name,fn)=>events[name]=fn},
  document:{body:{append(){}},createElement:()=>{const n={style:{},append(){},addEventListener:(name,fn)=>n[name]=fn};nodes.push(n);return n;}}
 };
 vm.runInNewContext(code,context);
 const message=(data,origin='https://app.scf.usercontent.goog',source=peer)=>events.message({origin,source,data:{channel:'frameforge.bridge.v1',...data}});
 const tick=async(n=5)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};
 const waitJob=async(idx)=>{
  for(let i=0;i<50;i++){
   if(sent.length>idx)return sent[idx];
   await tick(2);
  }
  throw new Error(`Timeout waiting for job ${idx}, sent count: ${sent.length}`);
 };

 message({type:'ready'});
 const runPromise = nodes[2].click();

 // 1. Character generation
 const job0 = await waitJob(0);
 assert.equal(job0.kind, 'characters');
 assert.equal(job0.id, 'c1');
 assert.equal(job0.aspectRatio, '1:1');
 message({type:'result', requestId:job0.requestId, result:{mediaId:'char-media-1', mimeType:'image/png', base64:'AA=='}});

 // 2. Location generation
 const job1 = await waitJob(1);
 assert.equal(job1.kind, 'locations');
 assert.equal(job1.id, 'l1');
 assert.equal(job1.aspectRatio, '16:9');
 message({type:'result', requestId:job1.requestId, result:{mediaId:'loc-media-1', mimeType:'image/png', base64:'BB=='}});

 // 3. Prop generation (1:1 isolated object)
 const job2 = await waitJob(2);
 assert.equal(job2.kind, 'props');
 assert.equal(job2.id, 'p1');
 assert.equal(job2.aspectRatio, '1:1');
 message({type:'result', requestId:job2.requestId, result:{mediaId:'prop-media-1', mimeType:'image/png', base64:'CC=='}});

 // 4. Scene 1 (without prop)
 const job3 = await waitJob(3);
 assert.equal(job3.kind, 'scenes');
 assert.equal(job3.id, 's1');
 // Direct full-size portrait references take priority; location context is
 // encoded in the scene prompt and is not allowed to shrink the character.
 assert.deepEqual(Array.from(job3.referenceImageMediaIds), ['char-media-1']);
 message({type:'result', requestId:job3.requestId, result:{mediaId:'scene-media-1', mimeType:'image/png', base64:'DD=='}});

 // 5. Scene 2 (with prop attached)
 const job4 = await waitJob(4);
 assert.equal(job4.kind, 'scenes');
 assert.equal(job4.id, 's2');
 assert.deepEqual(Array.from(job4.referenceImageMediaIds), ['char-media-1']);
 assert.equal(job4.modelDisplayName, '🍌 Nano Banana 2');
 message({type:'result', requestId:job4.requestId, result:{mediaId:'scene-media-2', mimeType:'image/png', base64:'EE=='}});

 await runPromise;
 await tick(2);
 const stateObj = values.get('ff-flow-v1-state:proj-props:flow');
 assert.deepEqual(Array.from(stateObj.done), ['characters:c1', 'locations:l1', 'props:p1', 'scenes:s1', 'scenes:s2']);
 assert.equal(stateObj.job, null);
});



test('Veo 3.1 Lite animation pipeline: jobs -> model veo-3.1-lite -> video/mp4 -> upload -> idempotent', async () => {
 const sceneId = 'scene-veo-1';
 const idempotencyKey = 'veo-idem-key-123';
 const scenePrompt = 'Cinematic drone shot of heroine';
 const veoPrompt = 'Use supplied references exactly. Action: heroine looks around. Camera: slow cinematic push-in.';

 const customPrompts = {
  characters: [],
  locations: [],
  props: [],
  scenes: [
   {scene_id: sceneId, position: 1, prompt: scenePrompt, flow_prompt: scenePrompt, has_image: false, references: []}
  ]
 };

 const animJobsResponse = {
  project_id: 'proj-veo',
  plan_mode: 'economy',
  jobs: [
   {
    kind: 'scene_video',
    scene_id: sceneId,
    position: 1,
    model: 'veo-3.1-lite',
    prompt: veoPrompt,
    duration_seconds: 5,
    aspect_ratio: '16:9',
    referenceImageMediaIds: ['flow-media-scene-1'],
    idempotency_key: idempotencyKey
   }
  ]
 };

 let serverSceneAnimationStatus = 'queued_veo';
 let serverVideoAssetId = null;

 const nodes = [], events = {}, calls = [], sent = [];
 const values = new Map();
 let stored = null;
 const peer = { postMessage(m) { sent.push(m); } };

 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, v) => { stored = JSON.parse(JSON.stringify(v)); values.set(key, stored); },
  GM_xmlhttpRequest: o => {
   calls.push(o);
   let data = {};
   if (o.url.endsWith('/current')) {
    data = { project_id: 'proj-veo' };
   } else if (o.url.endsWith('/prompts')) {
    if (serverVideoAssetId || calls.some(c => c.url.includes('/api/scenes/' + sceneId + '/upload'))) {
     customPrompts.scenes[0].has_image = true;
    }
    data = customPrompts;
   } else if (o.url.endsWith('/animation/jobs')) {
    if (serverSceneAnimationStatus === 'completed') {
     data = { project_id: 'proj-veo', plan_mode: 'economy', jobs: [] };
    } else {
     data = animJobsResponse;
    }
   } else if (o.url.includes('/api/scenes/' + sceneId + '/upload')) {
    data = { status: 'ok' };
   } else if (o.url.includes('/api/scenes/' + sceneId + '/video')) {
    const payload = JSON.parse(o.data);
    serverVideoAssetId = 'asset-video-veo-1';
    serverSceneAnimationStatus = 'completed';
    data = {
     scene_id: sceneId,
     video_asset_id: serverVideoAssetId,
     url: '/api/assets/' + serverVideoAssetId,
     animation: {
      status: 'completed',
      video_asset_id: serverVideoAssetId,
      credit_cost: 10,
      flow_media_id: payload.flow_media_id,
      idempotency_key: payload.idempotency_key
     }
    };
   }
   o.onload({ status: 200, responseText: JSON.stringify(data) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };

 vm.runInNewContext(code, context);
 const message = (data, origin = 'https://app.scf.usercontent.goog', source = peer) =>
  events.message({ origin, source, data: { channel: 'frameforge.bridge.v1', ...data } });

 const tick = async (n = 5) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };
 const waitJob = async (idx) => {
  for (let i = 0; i < 50; i++) {
   if (sent.length > idx) return sent[idx];
   await tick(2);
  }
  throw new Error('Timeout waiting for job ' + idx + ', sent count: ' + sent.length);
 };

 message({ type: 'ready' });
 const runPromise = nodes[2].click();

 // 1. Scene image generation
 const imgJob = await waitJob(0);
 assert.equal(imgJob.kind, 'scenes');
 assert.equal(imgJob.id, sceneId);
 message({
  type: 'result',
  requestId: imgJob.requestId,
  result: { mediaId: 'flow-scene-image-media-id-1', mimeType: 'image/png', base64: 'AA==' }
 });

 // 2. Veo 3.1 Lite video generation
 const veoJob = await waitJob(1);
 assert.equal(veoJob.kind, 'scene_video');
 assert.equal(veoJob.scene_id, sceneId);
 assert.equal(veoJob.model, 'veo-3.1-lite');
 assert.equal(veoJob.modelDisplayName, 'Veo 3.1 - Lite');
 assert.equal(veoJob.prompt, veoPrompt);
 assert.equal(veoJob.duration_seconds, 5);
 assert.equal(veoJob.aspect_ratio, '16:9');
 assert.equal(veoJob.aspectRatio, '16:9');
 // The server supplied the authoritative current Flow media ID for the frame.
 assert.equal(veoJob.firstFrameImageMediaId, 'flow-media-scene-1');
 assert.equal(veoJob.idempotency_key, idempotencyKey);

 // Flow adapter returns video/mp4
 message({
  type: 'scene_video_result',
  requestId: veoJob.requestId,
  flow_media_id: 'flow-veo-video-media-id-1',
  mimeType: 'video/mp4',
  base64: 'AAAA'
 });

 await runPromise;

 // Verify upload was called with correct data
 const videoUploadCall = calls.find(c => c.url.endsWith('/api/scenes/' + sceneId + '/video'));
 assert.ok(videoUploadCall, 'Video upload call was not made');
 const videoUploadPayload = JSON.parse(videoUploadCall.data);
 assert.equal(videoUploadPayload.filename, 'video.mp4');
 assert.equal(videoUploadPayload.flow_media_id, 'flow-veo-video-media-id-1');
 assert.equal(videoUploadPayload.idempotency_key, idempotencyKey);
 assert.ok(videoUploadPayload.video.startsWith('data:video/mp4;base64,AAAA'));

 // Verify local state in Tampermonkey
 const stateObj = values.get('ff-flow-v1-state:proj-veo:flow');
 assert.ok(stateObj.done.includes('scenes:' + sceneId));
 assert.ok(stateObj.done.includes('scene_video:' + sceneId + ':' + idempotencyKey));
 assert.equal(stateObj.refs['scene_video:' + sceneId], 'flow-veo-video-media-id-1');
 assert.equal(stateObj.job, null);
 assert.equal(serverSceneAnimationStatus, 'completed');
 assert.equal(serverVideoAssetId, 'asset-video-veo-1');

 // 3. Re-run / reload / heartbeat: must NOT trigger any new generations
 const sentBefore = sent.length;
 await nodes[2].click();
 await tick(10);
 assert.equal(sent.length, sentBefore, 'Second run must not send any new jobs');
});

test('Economy mode enforces max 5 Veo jobs in userscript', async () => {
 const customPrompts = { characters: [], locations: [], props: [], scenes: [] };
 const animJobsResponse = {
  project_id: 'proj-econ-overflow',
  plan_mode: 'economy',
  jobs: [
   { kind: 'scene_video', scene_id: 's1', position: 1, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k1' },
   { kind: 'scene_video', scene_id: 's2', position: 2, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k2' },
   { kind: 'scene_video', scene_id: 's3', position: 3, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k3' },
   { kind: 'scene_video', scene_id: 's4', position: 4, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k4' },
   { kind: 'scene_video', scene_id: 's5', position: 5, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k5' },
   { kind: 'scene_video', scene_id: 's6', position: 6, model: 'veo-3.1-lite', prompt: 'p', idempotency_key: 'k6' }
  ]
 };

 const nodes = [], events = {}, calls = [], sent = [];
 const values = new Map();
 const peer = { postMessage(m) { sent.push(m); } };

 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, v) => values.set(key, v),
  GM_xmlhttpRequest: o => {
   calls.push(o);
   let data = {};
   if (o.url.endsWith('/current')) data = { project_id: 'proj-econ-overflow' };
   else if (o.url.endsWith('/prompts')) data = customPrompts;
   else if (o.url.endsWith('/animation/jobs')) data = animJobsResponse;
   o.onload({ status: 200, responseText: JSON.stringify(data) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };

 vm.runInNewContext(code, context);
 events.message({ origin: 'https://app.scf.usercontent.goog', source: peer, data: { channel: 'frameforge.bridge.v1', type: 'ready' } });
 await nodes[2].click();
 assert.match(nodes[1].textContent, /Превышен лимит 5 Veo jobs/);
});


test('Selective Veo card flow handles progress and error messages safely', async () => {
 const sceneId = 'scene-sel-1';
 const idempotencyKey = 'idem-prog-err-1';

 const customPrompts = {
  characters: [],
  locations: [],
  props: [],
  scenes: [
   { scene_id: sceneId, position: 1, prompt: 'Scene 1', flow_prompt: 'Scene 1', has_image: true, references: [] }
  ]
 };

 const animJobsResponse = {
  project_id: 'proj-prog-err',
  plan_mode: 'manual',
  jobs: [
   {
    kind: 'scene_video',
    scene_id: sceneId,
    position: 1,
    scene_revision: 2,
    source_image_asset_id: 'src-asset-1',
    model: 'veo-3.1-lite',
    prompt: 'Veo micro-motion prompt',
    duration_seconds: 5,
    aspect_ratio: '16:9',
    referenceImageMediaIds: ['flow-media-scene-1'],
    idempotency_key: idempotencyKey
   }
  ]
 };

 const nodes = [], events = {}, calls = [], sent = [];
 const values = new Map([['ff-flow-v1-state', { projectId: 'proj-prog-err', flowProject: 'flow', refs: { ['scenes:' + sceneId]: 'flow-media-scene-1' }, done: ['scenes:' + sceneId], job: null, result: null }]]);
 const peer = { postMessage(m) { sent.push(m); } };

 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, v) => values.set(key, v),
  GM_xmlhttpRequest: o => {
   calls.push(o);
   let data = {};
   if (o.url.endsWith('/current')) data = { project_id: 'proj-prog-err' };
   else if (o.url.endsWith('/prompts')) data = customPrompts;
   else if (o.url.endsWith('/animation/jobs')) data = animJobsResponse;
   o.onload({ status: 200, responseText: JSON.stringify(data) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };

 vm.runInNewContext(code, context);
 const message = (data, origin = 'https://app.scf.usercontent.goog', source = peer) =>
  events.message({ origin, source, data: { channel: 'frameforge.bridge.v1', ...data } });

 const tick = async (n = 3) => { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); };

 message({ type: 'ready' });
 const runPromise = nodes[2].click();
 await tick(5);

 assert.equal(sent.length, 1);
 const job = sent[0];
 assert.equal(job.kind, 'scene_video');
 assert.equal(job.scene_id, sceneId);

 // Progress notification
 message({ type: 'progress', requestId: job.requestId, percent: 50, stage: 'Generating video frames...' });
 await tick(2);

 // Error notification
 message({ type: 'error', requestId: job.requestId, error: 'Veo generation safety policy violation' });
 await runPromise;

 assert.match(nodes[1].textContent, /Veo generation safety policy violation/);
});
test('uses the Flow image model explicitly selected in FrameForge settings',async()=>{
 const f=fixture(null,'p',{flow_image_model:'🍌 Nano Banana 2 Lite'});
 f.message({type:'ready'});const run=f.nodes[2].click();await f.tick();
 assert.equal(f.sent[0].modelDisplayName,'🍌 Nano Banana 2 Lite');
 f.nodes[4].click();f.message({type:'result',requestId:f.sent[0].requestId,result:{mediaId:'m',mimeType:'image/png',base64:'AA=='}});
 await run;
});

test('ERR_GEN_FAILED on Pro retries the identical job once on Nano Banana 2',async()=>{
 const f=fixture(null,'p',{flow_image_model:'🍌 Nano Banana Pro'});
 f.message({type:'ready'});
 const run=f.nodes[2].click();
 await f.tick();
 const first=f.sent[0];
 f.message({
  type:'error', requestId:first.requestId, name:'Error',
  error:{message:'Expected object response with media fields',code:'ERR_GEN_FAILED'},
  code:'ERR_GEN_FAILED', cause:'INTERNAL'
 });
 await f.tick();
 const fallback=f.sent[1];
 assert.equal(fallback.modelDisplayName,'🍌 Nano Banana 2');
 assert.equal(fallback.prompt,first.prompt);
 assert.deepEqual(fallback.referenceImageMediaIds,first.referenceImageMediaIds);
 assert.notEqual(fallback.requestId,first.requestId);
 f.message({type:'result',requestId:fallback.requestId,result:{mediaId:'fallback-media',mimeType:'image/png',base64:'AA=='}});
 await run;
 const saved=[...f.values.values()].find(value=>value?.diagnostics?.some(d=>d.phase==='image_model_fallback'));
 assert.ok(saved);
 assert.equal(saved.diagnostics.at(-1).modelDisplayName,'🍌 Nano Banana 2');
 assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,1);
});

test('an SDK rejection mislabeled as a result retries once, records the failed frame and completes the queue',async()=>{
 const f=fixture(null,'p',{flow_image_model:'🍌 Nano Banana 2'});
 f.message({type:'ready'});
 const run=f.nodes[2].click();
 await f.tick();
 const job=f.sent[0];
 f.message({
  type:'result', requestId:job.requestId,
  result:{error:{name:'Error',message:'ikFlow.generate.image()',code:'ERR_GEN_FAILED'}}
 });
 await f.tick();
 assert.equal(f.sent.length,2);
 assert.notEqual(f.sent[1].requestId,job.requestId);
 f.message({type:'error',requestId:f.sent[1].requestId,name:'Error',error:'ikFlow.generate.image()',code:'ERR_GEN_FAILED'});
 await run;
 assert.equal(f.calls.filter(c=>c.method==='POST'&&c.url.endsWith('/upload')).length,0);
 const saved=[...f.values.values()].find(value=>value?.failedScenes);
 assert.equal(saved.failedScenes['scenes:s'].code,'ERR_GEN_FAILED');
 assert.match(f.nodes[1].textContent,/Очередь завершена/);
});

test('existing FrameForge scene is imported once and becomes Veo first-frame media', async () => {
 const sceneId = 'existing-static-scene';
 const nodes = [], events = {}, calls = [], sent = [];
 const values = new Map();
 const peer = { postMessage(m) { sent.push(m); } };
 const prompts = { characters: [], locations: [], props: [], scenes: [{
  scene_id: sceneId, position: 1, prompt: 'Existing lighthouse frame',
  flow_prompt: 'Existing lighthouse frame', has_image: true,
  image_asset_id: 'asset-real-png', image_url: '/api/assets/asset-real-png', references: []
 }] };
 const dummySha = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
 const animationJobs = { project_id: 'project-existing', plan_mode: 'manual', jobs: [{
  kind: 'scene_video', scene_id: sceneId, position: 1, scene_revision: 3,
  source_image_asset_id: 'asset-real-png', source_image_url: '/api/assets/asset-real-png',
  source_image_sha256: dummySha,
  model: 'veo-3.1-lite', prompt: 'Animate supplied lighthouse frame',
  duration_seconds: 5, aspect_ratio: '16:9', idempotency_key: 'idem-existing'
 }] };
 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  btoa: value => Buffer.from(value, 'binary').toString('base64'),
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, value) => values.set(key, JSON.parse(JSON.stringify(value))),
  GM_xmlhttpRequest: request => {
   calls.push(request);
   if (request.url.endsWith('/api/assets/asset-real-png')) {
    request.onload({ status: 200, response: new Uint8Array([137, 80, 78, 71]).buffer, responseHeaders: 'content-type: image/png\r\n' });
    return;
   }
   const body = request.url.endsWith('/current') ? { project_id: 'project-existing' }
    : request.url.endsWith('/prompts') ? prompts
    : request.url.endsWith('/animation/jobs') ? animationJobs : {};
   request.onload({ status: 200, responseText: JSON.stringify(body) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };
 vm.runInNewContext(code, context);
 const message = data => events.message({ origin: 'https://app.scf.usercontent.goog', source: peer, data: { channel: 'frameforge.bridge.v1', ...data } });
 const tick = () => new Promise(resolve => setImmediate(resolve));
 message({ type: 'ready' });
 const run = nodes[2].click();
 for (let i = 0; i < 10 && sent.length === 0; i++) await tick();
 const importMsg = sent[0];
 assert.equal(importMsg.type, 'import_source_image');
 assert.equal(importMsg.source_image_asset_id, 'asset-real-png');
 assert.equal(importMsg.source_image_sha256, dummySha);
 assert.match(importMsg.base64, /^iVBOR/);

 // Bridge responds with source_image_imported
 message({
  type: 'source_image_imported',
  requestId: importMsg.requestId,
  mediaId: 'flow-imported-png-media-id',
  source_image_asset_id: 'asset-real-png',
  source_image_sha256: dummySha
 });
 for (let i = 0; i < 10 && sent.length < 2; i++) await tick();

 const veoJob = sent[1];
 assert.equal(veoJob.kind, 'scene_video');
 assert.equal(veoJob.firstFrameImageMediaId, 'flow-imported-png-media-id');
 message({ type: 'result', requestId: veoJob.requestId, result: { mediaId: 'flow-veo-video', mimeType: 'video/mp4', base64: 'AAAA' } });
 await run;
 const stored = values.get('ff-flow-v1-state:project-existing:flow');
 assert.ok(stored.done.includes(`scene_video:${sceneId}:idem-existing`));
 assert.equal(calls.filter(c => c.url.endsWith(`/api/scenes/${sceneId}/upload`)).length, 0);
 assert.equal(calls.filter(c => c.url.endsWith(`/api/scenes/${sceneId}/video`)).length, 1);
});

test('locked character reference is automatically imported into Flow on empty state/reload', async () => {
 const nodes = [], events = {}, calls = [], sent = []; const values = new Map();
 const peer = { postMessage(m) { sent.push(m); } };
 const prompts = {
  characters: [{ id: 'char_daniel', name: 'Дэниел', locked: true, portrait: 'asset_daniel_png', portrait_url: '/api/assets/asset_daniel_png' }],
  locations: [], props: [],
  scenes: [{
   scene_id: 'scene_daniel_1', position: 1, prompt: 'Daniel on trail', flow_prompt: 'Daniel on trail', has_image: false,
   references: [{ id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'asset_daniel_png', url: '/api/assets/asset_daniel_png' }]
  }]
 };
 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  btoa: value => Buffer.from(value, 'binary').toString('base64'),
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow_test_proj/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, value) => values.set(key, JSON.parse(JSON.stringify(value))),
  GM_xmlhttpRequest: request => {
   calls.push(request);
   if (request.url.endsWith('/api/assets/asset_daniel_png')) {
    request.onload({ status: 200, response: new Uint8Array([137, 80, 78, 71]).buffer, responseHeaders: 'content-type: image/png\r\n' });
    return;
   }
   const body = request.url.endsWith('/current') ? { project_id: 'proj_daniel' }
    : request.url.endsWith('/prompts') ? prompts : {};
   request.onload({ status: 200, responseText: JSON.stringify(body) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };
 vm.runInNewContext(code, context);
 const message = data => events.message({ origin: 'https://app.scf.usercontent.goog', source: peer, data: { channel: 'frameforge.bridge.v1', ...data } });
 const tick = () => new Promise(resolve => setImmediate(resolve));
 message({ type: 'ready' });
 const run = nodes[2].click();
 for (let i = 0; i < 10 && sent.length === 0; i++) await tick();

 // 1. Bridge must automatically import Daniel's portrait into Flow before scene generation
 const importMsg = sent[0];
 assert.equal(importMsg.type, 'import_source_image');
 assert.equal(importMsg.source_image_asset_id, 'asset_daniel_png');
 assert.match(importMsg.base64, /^iVBOR/);

 message({
  type: 'source_image_imported',
  requestId: importMsg.requestId,
  mediaId: 'flow_daniel_media_id',
  source_image_asset_id: 'asset_daniel_png'
 });
 for (let i = 0; i < 10 && sent.length < 2; i++) await tick();

 // 2. Scene generation proceeds using the recovered mediaId
 const sceneJob = sent[1];
 assert.equal(sceneJob.kind, 'scenes');
 assert.equal(sceneJob.id, 'scene_daniel_1');
 assert.deepEqual(Array.from(sceneJob.referenceImageMediaIds), ['flow_daniel_media_id']);
 message({ type: 'result', requestId: sceneJob.requestId, result: { mediaId: 'flow_scene_res_1', mimeType: 'image/png', base64: 'AAAA' } });
 await run;

 const stored = values.get('ff-flow-v1-state:proj_daniel:flow_test_proj');
 assert.equal(stored.refs['characters:char_daniel'], 'flow_daniel_media_id');
 assert.ok(stored.done.includes('scenes:scene_daniel_1'));
});

test('pending result from Flow is recovered and uploaded on startup without re-generation', async () => {
 const nodes = [], events = {}, calls = [], sent = [];
 const values = new Map();
 const pendingState = {
  projectId: 'proj_crash_test',
  flowProject: 'flow_crash_test',
  refs: {},
  done: [],
  job: {
   kind: 'scenes',
   id: 'scene_crash_1',
   scene_id: 'scene_crash_1',
   requestId: 'req-pending-1',
   prompt: 'Daniel in the clearing'
  },
  result: {
   mediaId: 'flow_media_clearing_1',
   mimeType: 'image/png',
   base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
  }
 };
 values.set('ff-flow-v1-state:proj_crash_test:flow_crash_test', pendingState);

 const peer = { postMessage(m) { sent.push(m); } };
 const prompts = {
  characters: [], locations: [], props: [],
  scenes: [{ scene_id: 'scene_crash_1', position: 1, prompt: 'Daniel in the clearing', flow_prompt: 'Daniel in the clearing', has_image: false, references: [] }]
 };
 const context = {
  URL, console, setTimeout, clearTimeout, setInterval: () => 0,
  btoa: value => Buffer.from(value, 'binary').toString('base64'),
  crypto: { randomUUID: () => String(sent.length + 1) },
  location: { pathname: '/project/flow_crash_test/tool/tool' },
  GM_getValue: (key, fallback) => values.has(key) ? values.get(key) : fallback,
  GM_setValue: (key, value) => values.set(key, JSON.parse(JSON.stringify(value))),
  GM_xmlhttpRequest: request => {
   calls.push(request);
   const body = request.url.endsWith('/current') ? { project_id: 'proj_crash_test' }
    : request.url.endsWith('/prompts') ? (calls.some(c => c.url.endsWith('/upload')) ? { ...prompts, scenes: prompts.scenes.map(s => ({ ...s, has_image: true })) } : prompts)
    : request.url.endsWith('/upload') ? { status: 'ok', asset_id: 'ast_scenes_scene_crash_1' } : {};
   request.onload({ status: 200, responseText: JSON.stringify(body) });
  },
  window: { addEventListener: (name, fn) => events[name] = fn },
  document: { body: { append() {} }, createElement: () => { const n = { style: {}, append() {}, addEventListener: (name, fn) => n[name] = fn }; nodes.push(n); return n; } }
 };
 vm.runInNewContext(code, context);
 const message = data => events.message({ origin: 'https://app.scf.usercontent.goog', source: peer, data: { channel: 'frameforge.bridge.v1', ...data } });
 const tick = () => new Promise(resolve => setImmediate(resolve));
 message({ type: 'ready' });
 const run = nodes[2].click();
 await tick();
 await run;

 // Upload must have been called with valid filename
 const uploadCalls = calls.filter(c => c.method === 'POST' && c.url.endsWith('/scenes/scene_crash_1/upload'));
 assert.equal(uploadCalls.length, 1);
 const uploadPayload = JSON.parse(uploadCalls[0].data);
 assert.equal(uploadPayload.flow_media_id, 'flow_media_clearing_1');
 assert.equal(uploadPayload.filename, 'scenes_scene_crash_1.png');

 // No new generations must have been sent to Flow!
 assert.equal(sent.length, 0);

 const finalState = values.get('ff-flow-v1-state:proj_crash_test:flow_crash_test');
 assert.equal(finalState.job, null);
 assert.equal(finalState.result, null);
 assert.ok(finalState.done.includes('scenes:scene_crash_1'));
});



