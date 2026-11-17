import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const code=fs.readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js',import.meta.url),'utf8');
const ui=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
function bridge(){
 const context={URL,console,setTimeout,clearTimeout,setInterval:()=>0,crypto:{randomUUID:()=> 'request-1'},location:{pathname:'/project/flow/tool/tool'},
  GM_getValue:(_,fallback)=>fallback,GM_setValue:()=>{},GM_xmlhttpRequest:o=>o.onload({status:200,responseText:JSON.stringify(o.url.endsWith('/current')?{project_id:'p'}:{})}),
  window:{addEventListener:()=>{}},document:{body:{append:()=>{}},createElement:()=>({style:{},append:()=>{},addEventListener:()=>{}})}};
 vm.runInNewContext(code,context);return context.FFBridgeQueue;
}
test('scoped portrait ignores old done cache, claims exact job and generates once',async()=>{
 const b=bridge(),calls=[],state={done:['characters:c'],refs:{'characters:c':'old-media'},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 await b.processBridgeItem({id:'c',name:'Doctor',prompt:'One solo marker portrait',locked:false,casting_generation_id:'g-new'},'characters',{
  project_id:'p',state,save:()=>{},tell:()=>{},api:async(method,path,data)=>{calls.push({method,path,data});return {project_id:'p',ok:true};},
  generateImageFn:async job=>{calls.push({generate:job.casting_generation_id});return {base64:'AA==',mediaId:'new',mimeType:'image/png'};},
  uploadSavedFn:async current=>{calls.push({upload:current.job.casting_generation_id});current.job=null;current.result=null;}
 });
 assert.equal(calls.filter(c=>c.generate).length,1);assert.equal(calls.find(c=>c.generate).generate,'g-new');
 assert.equal(calls.find(c=>c.path?.endsWith('/cast-start')).data.generation_id,'g-new');
 assert.equal(calls.find(c=>c.upload).upload,'g-new');
});
test('scoped queue never generates other characters or scenes',async()=>{
 const manifest={characters:[{id:'c',casting_generation_id:'g'},{id:'other'}],scenes:[{scene_id:'s'}],manual_characters:[]};const calls=[];
 const context=vm.createContext({processBridgeItem:async(i,kind)=>{calls.push([i.id,kind]);return {status:'succeeded'};},Set});
 vm.runInContext(code.slice(code.indexOf('async function processBridgeQueue('),code.indexOf('async function run(')),context);
 await context.processBridgeQueue({generationIds:['g'],project_id:'p',api:async()=>manifest,tell:()=>{}});
 assert.deepEqual(calls,[['c','characters']]);
});
test('UI creates one request and dispatches only returned generation ID',async()=>{
 const calls=[],context=vm.createContext({current:'p',currentSettings:{image_backend:'flow'},picked:new Map(),refresh:async()=>{},showNotice:()=>{},
 api:async(path,body)=>{calls.push({path,body});return path==='/bridge/v1/status'?{connected:true,character_casting:true,running:false}:{generation_ids:['g'],state:'queued'};}});
 vm.runInContext(ui.slice(ui.indexOf('async function regenerateCharacter('),ui.indexOf('function subjectGender(')),context);
 await context.regenerateCharacter({id:'c'});
 assert.equal(calls.find(c=>c.path.endsWith('/cast')).body.count,1);
 assert.deepEqual(Array.from(calls.find(c=>c.path==='/bridge/v1/command').body.generation_ids),['g']);
});
test('old bridge or busy bridge cannot add a fresh waiting job',async()=>{
 for(const status of [{connected:true,character_casting:false},{connected:true,character_casting:true,running:true}]){
  const calls=[],context=vm.createContext({currentSettings:{image_backend:'flow'},api:async path=>{calls.push(path);return status;}});
  vm.runInContext(ui.slice(ui.indexOf('async function regenerateCharacter('),ui.indexOf('function subjectGender(')),context);
  await assert.rejects(context.regenerateCharacter({id:'c'}));assert.equal(calls.length,1);
 }
});
test('two-person portrait rejection clears failed cached result for the next explicit retry',async()=>{
 const b=bridge(),state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 await assert.rejects(b.processBridgeItem({id:'c',prompt:'One portrait',casting_generation_id:'g'},'characters',{
  project_id:'p',state,save:()=>{},tell:()=>{},api:async()=>({project_id:'p',ok:true}),generateImageFn:async()=>({base64:'AA==',mediaId:'new',mimeType:'image/png'}),
  uploadSavedFn:async()=>{throw Object.assign(new Error('Two people'),{code:'PORTRAIT_REJECTED'});}}));
 assert.equal(state.job,null);assert.equal(state.result,null);
});
test('completed scene upload failure cannot block a separate portrait, and its image is retained',()=>{
 const b=bridge(),job={kind:'scenes',id:'old-scene',scene_revision:2},result={base64:'kept',mediaId:'old'};
 const state={job,result};
 assert.equal(b.isolateCompletedSceneForPortrait(state,['new-portrait'],'Face check rejected'),true);
 assert.equal(state.job,null);assert.equal(state.result,null);
 assert.equal(state.sceneTasks['old-scene:2'].result,result);
 assert.equal(state.sceneTasks['old-scene:2'].job,job);
});
test('an unresolved request with no result is never discarded for a portrait',()=>{
 const b=bridge(),job={kind:'scenes',id:'in-flight'},state={job,result:null};
 assert.equal(b.isolateCompletedSceneForPortrait(state,['new'],'Timeout'),false);assert.equal(state.job,job);
});

test('long portrait prompt reaches SDK intact, including final identity constraint',async()=>{
 const b=bridge(),prompt='One portrait. '+ 'x'.repeat(3500)+' EXACTLY ONE PERSON END',sent=[];
 const state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 await b.processBridgeItem({id:'c',prompt,casting_generation_id:'g'},'characters',{project_id:'p',state,save:()=>{},tell:()=>{},api:async()=>({project_id:'p'}),
 generateImageFn:async job=>{sent.push(job.prompt);return {base64:'AA==',mediaId:'m',mimeType:'image/png'};},uploadSavedFn:async current=>{current.job=null;current.result=null;}});
 assert.equal(sent[0],prompt);
});
test('oversized prompt fails before SDK instead of losing its tail',async()=>{
 const b=bridge(),state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};let generated=false;
 await assert.rejects(b.processBridgeItem({id:'c',prompt:'x'.repeat(4001)},'characters',{project_id:'p',state,save:()=>{},tell:()=>{},api:async()=>({project_id:'p'}),
 generateImageFn:async()=>{generated=true;}}),/4000/);
 assert.equal(generated,false);
});

test('ordinary character queue uses one tracked cast and server canonical portrait prompt',async()=>{
 const b=bridge(),calls=[],sent=[],state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 await b.processBridgeItem({id:'c',prompt:'Stale classroom scene'},'characters',{project_id:'p',state,save:()=>{},tell:()=>{},
 api:async(method,path,body)=>{calls.push({path,body});return path.endsWith('/cast')?{generation_ids:['g']}:path.endsWith('/cast-start')?{prompt:'SOLO fresh portrait exactly one person'}:{project_id:'p'};},
 generateImageFn:async job=>{sent.push({prompt:job.prompt,generation:job.casting_generation_id});return {base64:'AA==',mediaId:'m',mimeType:'image/png'};},
 uploadSavedFn:async current=>{current.job=null;current.result=null;}});
 assert.equal(calls.find(c=>c.path.endsWith('/cast')).body.count,1);
 assert.equal(sent.length,1);assert.equal(sent[0].generation,'g');assert.equal(sent[0].prompt,'SOLO fresh portrait exactly one person');
});
test('rejected cached portrait upload is cleared centrally, including after reload',async()=>{
 const start=code.indexOf('async function uploadSaved('),end=code.indexOf('function handleSourceRejection(',start);
 const state={job:{kind:'characters',id:'c',requestId:'r',casting_generation_id:'g'},result:{mediaId:'m',mimeType:'image/png',base64:'AA=='},refs:{},done:[]};
 const context=vm.createContext({state,save:()=>{},console,Date,api:async()=>{throw Object.assign(new Error('Multiple people'),{code:'PORTRAIT_REJECTED'});}});
 vm.runInContext(code.slice(start,end),context);
 await assert.rejects(context.uploadSaved(state),/Multiple people/);
 assert.equal(state.job,null);assert.equal(state.result,null);assert.equal(state.rejectedPortraits.c.generation_id,'g');assert.equal(state.done.length,0);
});

test('confirmed service rejection ends the scene attempt without automatic generation retry',async()=>{
 const b=bridge(),state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};let calls=0;
 await assert.rejects(b.processBridgeItem({scene_id:'s',position:2,prompt:'One scene'},'scenes',{
 project_id:'p',state,save:()=>{},tell:()=>{},api:async()=>({project_id:'p'}),
 generateImageFn:async()=>{calls++;throw Object.assign(new Error('Сервис создания изображений недоступен. Повторите попытку.'),{flow:{code:'FLOW_UNKNOWN_ERROR',remoteReply:true}});}}),/Flow отклонил/);
 assert.equal(calls,1);assert.equal(state.job,null);assert.equal(state.result,null);assert.equal(state.failedScenes['scenes:s'].status,'failed_remote');
});
test('legacy known provider rejection is reconciled once without submitting a new request',async()=>{
 const b=bridge(),task={job:{id:'s',position:2,requestId:'old-r'},result:null,error:'Flow generate: Сервис создания изображений недоступен. FLOW_UNKNOWN_ERROR'};
 const state={sceneTasks:{'s:1':task}};let uploads=0;
 await assert.rejects(b.recoverSceneTasks({state,save:()=>{},uploadSavedFn:async()=>uploads++}),/Старая попытка снята/);
 assert.equal(task.job,null);assert.equal(task.failedAttempt.requestId,'old-r');assert.equal(uploads,0);
 await b.recoverSceneTasks({state,save:()=>{},uploadSavedFn:async()=>uploads++});assert.equal(uploads,0);
});
test('genuine timeout and decode-unknown are retained rather than retried or cleared',async()=>{
 for(const reason of ['Flow не ответил за 4 минуты.','Flow image response has no transferable bytes; keys: result','NETWORK_ERROR']){
  const b=bridge(),job={id:'s',position:2,requestId:'r',submissionPhase:'sent'},task={job,result:null,error:reason};
  await assert.rejects(b.recoverSceneTasks({state:{sceneTasks:{'s:1':task}},save:()=>{},uploadSavedFn:async()=>{}}),/исход запроса Flow неизвестен/);
  assert.equal(task.job,job);
 }
});
test('definitely unsent preflight task does not become an unknown paid request',async()=>{
 const b=bridge(),task={job:{id:'s',position:2,requestId:'r',submissionPhase:'not_sent'},result:null,error:'unsupported profile'};
 await b.recoverSceneTasks({state:{sceneTasks:{'s:1':task}},save:()=>{},uploadSavedFn:async()=>{}});
 assert.equal(task.job,null);assert.equal(task.unsentAttempt.requestId,'r');
});
test('a saved image is uploaded first even if an old error message exists',async()=>{
 const b=bridge(),image={base64:'keep'},task={job:{id:'s',position:2,scene_revision:1,requestId:'r'},result:image,error:'Сервис создания изображений недоступен.'};
 let received=null;await b.recoverSceneTasks({state:{sceneTasks:{'s:1':task},done:[],refs:{}},save:()=>{},uploadSavedFn:async local=>{received=local.result;local.job=null;local.result=null;}});
 assert.equal(received,image);
});


test('real nested adapter error reply preserves provider code and closes only the rejected request',async()=>{
 const events={},sent=[],peer={postMessage:m=>sent.push(m)},state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 const context={URL,console,setTimeout,clearTimeout,setInterval:()=>0,crypto:{randomUUID:()=> 'nested-request'},location:{pathname:'/project/flow/tool/tool'},
 GM_getValue:(_,fallback)=>fallback,GM_setValue:()=>{},GM_xmlhttpRequest:o=>o.onload({status:200,responseText:JSON.stringify(o.url.endsWith('/current')?{project_id:'p'}:{})}),
 window:{addEventListener:(name,fn)=>events[name]=fn},document:{body:{append:()=>{}},createElement:()=>({style:{},append:()=>{},addEventListener:()=>{}})}};
 vm.runInNewContext(code,context);
 const reply=data=>events.message({origin:'https://app.scf.usercontent.goog',source:peer,data:{channel:'frameforge.bridge.v1',...data}});
 reply({type:'ready'});
 const result=context.FFBridgeQueue.processBridgeItem({scene_id:'s',position:2,prompt:'Single scene'},'scenes',{project_id:'p',state,save:()=>{},tell:()=>{},api:async()=>({project_id:'p'})});
 const rejected=assert.rejects(result,/Flow отклонил/);
 await new Promise(r=>setImmediate(r));
 assert.equal(sent.length,1);
 reply({type:'error',requestId:'nested-request',error:{name:'Error',message:'Сервис создания изображений недоступен. Повторите попытку.',code:'FLOW_UNKNOWN_ERROR',cause:'INTERNAL'}});
 await rejected;
 assert.equal(state.failedScenes['scenes:s'].code,'FLOW_UNKNOWN_ERROR');assert.equal(state.job,null);assert.equal(sent.length,1);
});


test('service rejection releases its exact server reservation before clearing the local job',async()=>{
 const b=bridge(),state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'},calls=[];
 await assert.rejects(b.processBridgeItem({scene_id:'s',position:2,prompt:'scene',google_queue_enabled:true},'scenes',{
  project_id:'p',state,save:()=>{},tell:()=>{},api:async(m,path,data)=>{calls.push({path,data});if(path.endsWith('/failure'))assert.equal(state.job.requestId,data.request_id);return {allowed:true,project_id:'p'};},
  generateImageFn:async()=>{throw Object.assign(Error('Сервис создания изображений недоступен.'),{flow:{code:'FLOW_UNKNOWN_ERROR',remoteReply:true}});}
 }),/Flow отклонил/);
 const releases=calls.filter(c=>c.path.endsWith('/failure'));assert.equal(releases.length,1);assert.equal(releases[0].data.code,'FLOW_SERVICE_UNAVAILABLE');assert.equal(state.job,null);
 assert.equal(state.failedScenes['scenes:s'].admissionReleased,true);
});

test('failed server acknowledgment preserves rejected job and retries cleanup without generation',async()=>{
 const b=bridge(),state={done:[],refs:{},sdkFailures:{},sdkCircuits:{},flowImageModel:'🍌 Nano Banana 2'};
 await assert.rejects(b.processBridgeItem({scene_id:'s',position:2,prompt:'scene',google_queue_enabled:true},'scenes',{
  project_id:'p',state,save:()=>{},tell:()=>{},api:async(m,path)=>{if(path.endsWith('/failure'))throw Error('offline');return {allowed:true,project_id:'p'};},
  generateImageFn:async()=>{throw Object.assign(Error('Сервис создания изображений недоступен.'),{flow:{code:'FLOW_UNKNOWN_ERROR',remoteReply:true}});}
 }),/offline/);
 assert.ok(state.job);assert.ok(state.job.remoteFailure);
 const task={job:state.job,result:null,error:'offline'},root={sceneTasks:{'s:1':task}};let releases=0;
 await assert.rejects(b.recoverSceneTasks({state:root,save:()=>{},api:async()=>{releases++;return {ok:true};}}),/Старая попытка снята/);
 assert.equal(releases,1);assert.equal(task.job,null);assert.equal(task.failedAttempt.admissionReleased,true);
});

test('v79 retired failure is acknowledged once and migration never submits a replacement',async()=>{
 const b=bridge(),failure={requestId:'old',sceneId:'s',position:2,status:'failed_remote',reason:'Сервис создания изображений недоступен.'};
 // JSON persistence creates two distinct objects for the same request.
 const state={sceneTasks:{'s:1':{job:null,result:null,failedAttempt:{...failure}}},failedScenes:{'scenes:s':{...failure}}};let calls=0;
 const options={state,save:()=>{},api:async(m,path,data)=>{calls++;assert.equal(data.request_id,'old');assert.equal(path,'/api/bridge/v1/admission/failure');return {ok:true};}};
 await assert.rejects(b.recoverSceneTasks(options),/Старая попытка снята/);
 await b.recoverSceneTasks({...options,state:JSON.parse(JSON.stringify(state))});assert.equal(calls,1);
});

test('old failure acknowledgment is retried after connection failure and survives another reload',async()=>{
 const b=bridge(),failure={requestId:'old',sceneId:'s',position:2,status:'failed_remote',reason:'Сервис создания изображений недоступен.'},state={failedScenes:{s:failure}};
 await assert.rejects(b.recoverSceneTasks({state,save:()=>{},api:async()=>{throw Error('offline');}}),/offline/);
 assert.notEqual(failure.admissionReleased,true);
 const reloaded=JSON.parse(JSON.stringify(state));let calls=0;
 await assert.rejects(b.recoverSceneTasks({state:reloaded,save:()=>{},api:async()=>{calls++;}}),/Старая попытка снята/);
 assert.equal(calls,1);assert.equal(reloaded.failedScenes.s.admissionReleased,true);
});

test('recovery closes confirmed tasks and definitely unsent slots but retains all unknown requests',async()=>{
 const b=bridge(),state={sceneTasks:{
  'bad:1':{job:{id:'bad',requestId:'bad-r',position:2,admissionEnabled:true,submissionPhase:'sent'},error:'Сервис создания изображений недоступен.'},
  'unsent:1':{job:{id:'unsent',requestId:'unsent-r',admissionEnabled:true,submissionPhase:'not_sent'}},
  'unknown:1':{job:{id:'unknown',requestId:'unknown-r',admissionEnabled:true,submissionPhase:'sent'},error:'Timeout'}
 }},calls=[];
 await assert.rejects(b.recoverSceneTasks({state,save:()=>{},api:async(m,path,data)=>{calls.push(data);}}),/Старая попытка снята/);
 assert.deepEqual(calls.map(c=>c.code),['FLOW_SERVICE_UNAVAILABLE','NOT_SENT']);
 assert.equal(state.sceneTasks['unknown:1'].job.requestId,'unknown-r');
 await assert.rejects(b.recoverSceneTasks({state,save:()=>{},api:async()=>assert.fail('Must not close unknown request')}),/исход запроса Flow неизвестен/);
});

test('scoped recovery does not release another scene and saved images are never discarded',async()=>{
 const b=bridge(),state={failedScenes:{other:{requestId:'r',sceneId:'other',status:'failed_remote',reason:'Сервис создания изображений недоступен.'}}};
 await b.recoverSceneTasks({state,sceneIds:['wanted'],save:()=>{},api:async()=>assert.fail('Wrong scope')});
 const image={base64:'saved'},task={job:{id:'s',requestId:'r',scene_revision:1,admissionEnabled:true},result:image,error:'Сервис создания изображений недоступен.'};
 let uploaded=false;await b.recoverSceneTasks({state:{sceneTasks:{'s:1':task}},save:()=>{},api:async()=>assert.fail('Must upload saved image'),uploadSavedFn:async local=>{assert.equal(local.result,image);uploaded=true;local.job=null;local.result=null;}});
 assert.equal(uploaded,true);
});
