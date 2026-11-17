import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto,createHash} from 'node:crypto';
const imageBytes=new Uint8Array([137,80,78,71]);
const imageSha=createHash('sha256').update(imageBytes).digest('hex');
import {readFileSync} from 'node:fs';

const source=readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js',import.meta.url),'utf8');
const video=(id='s1',requestId='old')=>({kind:'scene_video',id,scene_id:id,position:1,requestId,scene_revision:2,
 source_image_asset_id:'image-'+id,source_image_sha256:imageSha,idempotency_key:'key-'+id,model:'gemini-omni-1.1-flash'});
const job=id=>({...video(id,'new-'+id),engine:'omni_flash',firstFrameImageMediaId:'media-'+id,prompt:'Animate supplied image',duration_seconds:6});
function fixture(initial={},jobs=[],prepare=null,reply=null,clock=setTimeout,recovery={}){
 const events={},calls=[],sent=[],nodes=[],store=new Map(),uploaded=new Set();
 store.set('ff-flow-v1-state:p:flow',{projectId:'p',flowProject:'flow',refs:{},done:[],job:null,result:null,...initial});
 const message=m=>events.message({source:peer,origin:'https://app.scf.usercontent.goog',data:{channel:'frameforge.bridge.v1',...m}});
 const peer={postMessage(m){sent.push(m);setImmediate(()=>{
   if(m.type==='import_source_image'){
    message({type:'source_image_imported',requestId:m.requestId,mediaId:'fresh-'+m.scene_id,
     source_image_asset_id:m.source_image_asset_id,source_image_sha256:m.source_image_sha256});
   }
   if(m.type==='scene_video'){
    if(reply)reply(m,message);
    else message({type:'result',requestId:m.requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'clip-'+m.scene_id}});
   }
 });}};
 const context={URL,Proxy,Buffer,console:{log(){},warn(){},error(){}},setTimeout:clock,clearTimeout,setInterval:()=>0,crypto:{subtle:webcrypto.subtle,randomUUID:()=>Math.random().toString(36)},
  location:{pathname:'/project/flow',origin:'https://flow.google.com'},
  GM_getValue:(key,fallback)=>store.get(key)??fallback,
  GM_setValue:(key,value)=>store.set(key,JSON.parse(JSON.stringify(value))),
  GM_xmlhttpRequest:options=>{
   if(options.url.includes('/api/assets/')){options.onload({status:200,response:imageBytes.buffer,responseHeaders:'Content-Type: image/png'});return;}
   const data=options.data?JSON.parse(options.data):{};calls.push({url:options.url,data,method:options.method});
   let response={};
   if(options.url.endsWith('/current'))response={project_id:'p'};
   else if(options.url.endsWith('/settings'))response={};
   else if(options.url.endsWith('/animation/prepare-direction'))response=prepare?prepare(data):{};
   else if(options.url.endsWith('/animation/recovery'))response=recovery;
   else if(options.url.endsWith('/animation/jobs'))response={jobs:jobs.filter(j=>!uploaded.has(j.scene_id)),plan_mode:'selective'};
   else if(options.url.endsWith('/video'))uploaded.add(new URL(options.url).pathname.split('/').at(-2));
   else if(options.url.includes('/prompts'))throw Error('Animation-only run must not read image prompts');
   options.onload({status:200,responseText:JSON.stringify(response)});
  },
  window:{addEventListener:(name,fn)=>events[name]=fn},
  document:{body:{append(){}},createElement(){const n={style:{},append(){},addEventListener:(name,fn)=>n[name]=fn};nodes.push(n);return n;}}
 };
 context.globalThis=context;
 vm.runInNewContext(source.replace('globalThis.FFBridgeQueue = {','globalThis.FFBridgeQueue = {run,'),context);
 message({type:'ready'});
 return {bridge:context.FFBridgeQueue,calls,sentVideo:()=>sent.filter(m=>m.type==='scene_video'),sent,nodes,store,message,state:()=>store.get('ff-flow-v1-state:p:flow')};
}

const legacy=id=>({...job(id),prompt_contract:'legacy-motion/1',prompt:'Old repeated story bytes'});
const prepared={prompt_contract:'image-motion/3',prompt:'Continue only the visible action. Context once.',
 idempotency_key:'new-bound-key',scene_revision:3,upgraded:true};

test('unsent legacy request is upgraded before generation and upload uses new binding',async()=>{
 const f=fixture({},[legacy('s1')],()=>prepared);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 const prepare=f.calls.find(c=>c.url.endsWith('/prepare-direction'));
 assert.equal(prepare.data.not_sent,true);assert.equal(prepare.data.idempotency_key,'key-s1');
 assert.equal(prepare.data.source_image_sha256,imageSha);
 assert.equal(f.sentVideo().length,1);assert.equal(f.sentVideo()[0].prompt,prepared.prompt);
 const upload=f.calls.find(c=>c.url.endsWith('/video'));
 assert.equal(upload.data.idempotency_key,prepared.idempotency_key);
 assert.equal(upload.data.expected_scene_revision,3);
 assert.equal(upload.data.source_image_asset_id,'image-s1');
 assert.ok(f.calls.indexOf(prepare)<f.calls.indexOf(upload));
});

test('missing upgrade confirmation stops before Flow rather than sending old narration',async()=>{
 const f=fixture({},[legacy('s1')],()=>({prompt_contract:'image-motion/3'}));
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,0);assert.ok(f.state().error);
 assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,0);
});

test('unknown legacy request is preserved and excluded from prompt migration',async()=>{
 const old={...video(),prompt:'Original submitted bytes'};
 const f=fixture({job:old,error:'timeout'},[legacy('s1'),legacy('s2')],()=>prepared);
 await f.bridge.run(['s1','s2'],null,false,null,'p',true);
 assert.deepEqual(f.calls.filter(c=>c.url.endsWith('/prepare-direction')).map(c=>c.data.scene_id),['s2']);
 assert.deepEqual(f.sentVideo().map(m=>m.scene_id),['s2']);
 assert.equal(f.state().videoTasks.old.job.prompt,old.prompt);
 assert.equal(f.state().videoTasks.old.job.idempotency_key,'key-s1');
});

test('new contract is sent unchanged and never invokes legacy migration',async()=>{
 const f=fixture({},[{...job('s1'),prompt_contract:'image-motion/3'}]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.ok(!f.calls.some(c=>c.url.endsWith('/prepare-direction')));
 assert.equal(f.sentVideo().length,1);assert.equal(f.sentVideo()[0].prompt,'Animate supplied image');
});

test('v2 queued narration is refreshed to the isolated motion contract',async()=>{
 const f=fixture({},[{...job('s1'),prompt_contract:'image-motion/2'}],()=>prepared);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.calls.filter(c=>c.url.endsWith('/prepare-direction')).length,1);
 assert.equal(f.sentVideo()[0].prompt,prepared.prompt);
});

test('generic remote SDK error parks exact request and continues other selected scenes',async()=>{
 const f=fixture({},[job('s1'),job('s2')],null,(m,send)=>{
  if(m.scene_id==='s1')send({type:'scene_video_error',requestId:m.requestId,error:{name:'Error',message:'Service unavailable',code:'FLOW_UNKNOWN_ERROR'}});
  else send({type:'result',requestId:m.requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'second'}});
 });
 await f.bridge.run(['s1','s2'],null,false,null,'p',true);
 assert.deepEqual(f.sentVideo().map(m=>m.scene_id),['s1','s2']);
 const failedRequest=f.sentVideo()[0],task=f.state().videoTasks[failedRequest.requestId];
 assert.equal(task.job.idempotency_key,'key-s1');assert.equal(task.job.prompt,'Animate supplied image');
 assert.match(task.error,/Исход видеозапроса не подтверждён/);
 const progress=f.calls.filter(c=>c.url.endsWith('/animation/progress')&&c.data.error);
 assert.equal(progress.length,1);assert.equal(progress[0].data.status,'generating_omni');
 assert.match(progress[0].data.error,/Кадр 1/);
 assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,1);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().filter(m=>m.scene_id==='s1').length,1);
 f.message({type:'scene_video_result',requestId:failedRequest.requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'late'}});
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,2);
 assert.equal(f.sentVideo().filter(m=>m.scene_id==='s1').length,1);
});

test('transport timeout stops before next request and retains late-result binding',async()=>{
 const clock=(fn,ms)=>setTimeout(fn,ms===240000?0:ms);
 const f=fixture({},[job('s1'),job('s2')],null,()=>{},clock);
 await f.bridge.run(['s1','s2'],null,false,null,'p',true);
 assert.deepEqual(f.sentVideo().map(m=>m.scene_id),['s1']);
 assert.equal(f.state().job.idempotency_key,'key-s1');
 assert.match(f.state().error,/Кадр 1/);
 f.message({type:'scene_video_result',requestId:f.sentVideo()[0].requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'late'}});
 assert.equal(f.state().result.mediaId,'late');
});

test('late generic reply retains evidence without reclassifying it as definite rejection',async()=>{
 const original=video();const f=fixture({job:original,error:'timeout'},[]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 f.message({type:'scene_video_error',requestId:'old',error:{message:'Unknown provider response',code:'FLOW_UNKNOWN_ERROR'}});
 assert.equal(f.state().videoTasks.old.lastResponse.code,'FLOW_UNKNOWN_ERROR');
 assert.equal(f.state().videoTasks.old.job.remoteFailure,undefined);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,0);
 assert.ok(!f.calls.some(c=>c.data.status==='failed'));
});

test('confirmed no-result releases only the old request and allows the new bound attempt',async()=>{
 const old=video();const next={...job('s1'),idempotency_key:'fresh-key',scene_revision:4};
 const f=fixture({job:old,error:'timeout'},[next],null,null,setTimeout,
  {scenes:[{scene_id:'s1',position:1,resolved_attempt_keys:['key-s1']}]});
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.state().videoTasks.old.job,null);
 assert.equal(f.state().videoTasks.old.resolvedAttempt.idempotency_key,'key-s1');
 assert.equal(f.sentVideo().length,1);assert.equal(f.calls.find(c=>c.url.endsWith('/video')).data.idempotency_key,'fresh-key');
});

test('existing imported video retires stale browser attempt without generating or uploading it',async()=>{
 const old=video();delete old.position;
 const f=fixture({job:old,error:'timeout'},[],null,null,setTimeout,
  {scenes:[{scene_id:'s1',position:37,video_asset_id:'imported',resolved_attempt_keys:[]}]});
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.state().videoTasks.old.resolvedAttempt.position,37);
 assert.equal(f.state().videoTasks.old.job,null);assert.equal(f.sentVideo().length,0);
 assert.ok(!f.calls.some(c=>c.url.endsWith('/video')));
});



test('new video ignores stale persisted media ID and imports exact current source first',async()=>{
 const initial={refs:{['frameforge_source:s1:'+imageSha]:'stale-media'}};
 const f=fixture(initial,[job('s1')]);await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sent[0].type,'import_source_image');
 assert.equal(f.sentVideo()[0].firstFrameImageMediaId,'fresh-s1');
 assert.notEqual(f.sentVideo()[0].firstFrameImageMediaId,'media-s1');
 assert.equal(f.sent[0].source_image_sha256,imageSha);
 assert.equal(f.sentVideo().length,1);
});

test('wrong source hash blocks import and video submission for every scene',async()=>{
 const f=fixture({},[{...job('s1'),source_image_sha256:'0'.repeat(64)}]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sent.length,0);assert.match(f.state().error,/SHA-256/);
 assert.ok(!f.calls.some(c=>c.url.endsWith('/video')));
});


test('legacy generic SDK response never becomes an outage or a moderation diagnosis',()=>{
 const f=fixture();const e=f.bridge.normalizeVideoFlowError({name:'Error',message:'Сервис создания видео недоступен. Повторите попытку.',code:'FLOW_UNKNOWN_ERROR',cause:'INTERNAL'});
 assert.equal(e.code,'FLOW_SDK_UNCLASSIFIED');assert.equal(e.codeOrigin,'adapter_fallback');assert.equal(e.cause,'');
 assert.equal(f.bridge.confirmedVideoFailure({flow:e,message:e.message}),false);
 assert.match(f.bridge.videoFailureReason({position:1},{flow:e}),/SDK не передал точную причину/);
});

test('explicit public-figure policy message is recognized for any scene',()=>{
 const f=fixture();const e=f.bridge.normalizeVideoFlowError({message:'Этот запрос может нарушать наши правила в отношении генерации изображений известных людей.'});
 assert.equal(e.code,'FLOW_POLICY_PUBLIC_FIGURE');assert.equal(e.codeOrigin,'provider_message');
 assert.equal(f.bridge.confirmedVideoFailure({flow:e,message:e.message}),true);
});

test('explicit policy rejection is recorded once and other selected videos continue',async()=>{
 const f=fixture({},[job('s1'),job('s2')],null,(m,send)=>{
  if(m.scene_id==='s1')send({type:'scene_video_error',requestId:m.requestId,error:{message:'Этот запрос может нарушать наши правила в отношении генерации изображений известных людей.'}});
  else send({type:'result',requestId:m.requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'second'}});
 });
 await f.bridge.run(['s1','s2'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,2);
 const refusal=f.calls.find(c=>c.url.endsWith('/animation/progress')&&c.data.status==='failed');
 assert.equal(refusal.data.idempotency_key,'key-s1');assert.match(refusal.data.error,/известных людей/);
 assert.ok(!f.state().videoTasks?.[f.sentVideo()[0].requestId]);
});
