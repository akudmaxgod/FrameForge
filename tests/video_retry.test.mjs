import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// Test the served bridge. Unconfirmed submissions require recovery rather
// than the removed helper's automatic retries and additional credit charges.
const source=readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js',import.meta.url),'utf8');
function fixture(initial=null,jobs=null){
 const nodes=[],events={},sent=[],uploads=[],values=new Map();
 if(initial)values.set('ff-flow-v1-state',initial);
 const peer={postMessage(job){sent.push(job);}};let saved=null,heartbeat=()=>{},command=null;
 const context={URL,setTimeout,clearTimeout,setInterval:fn=>{heartbeat=fn;return 0;},console:{log(){},warn(){},error(){}},
  crypto:{randomUUID:()=>`request-${sent.length+1}`},location:{pathname:'/project/flow/tool/tool'},
  GM_getValue:(key,fallback)=>values.has(key)?values.get(key):fallback,
  GM_setValue:(key,value)=>{saved=JSON.parse(JSON.stringify(value));values.set(key,saved);},
  GM_xmlhttpRequest:options=>{
   let response={};
   if(options.url.endsWith('/current'))response={project_id:'p'};
   if(options.url.endsWith('/heartbeat'))response={command};
   if(options.url.endsWith('/prompts'))response={profile_type:'forgotten_cities',scenes:[],reference_jobs:[]};
   if(options.url.endsWith('/animation/jobs'))response={plan_mode:'manual',jobs:jobs||[{
    kind:'scene_video',scene_id:'s',position:1,scene_revision:2,source_image_asset_id:'original',
    source_image_sha256:'original-hash',firstFrameImageMediaId:'flow-original',model:'gemini-omni-1.1-flash',
    duration_seconds:6,prompt:'Preserve the approved source',idempotency_key:'same-job'}]};
   if(options.method==='POST'&&(options.url.endsWith('/video')||options.url.endsWith('/animation/progress')))uploads.push(options);
   options.onload({status:200,responseText:JSON.stringify(response)});
  },
  window:{addEventListener:(name,fn)=>events[name]=fn},
  document:{body:{append(){}},createElement:()=>{const node={style:{},append(){},addEventListener:(name,fn)=>node[name]=fn};nodes.push(node);return node;}}
 };
 vm.runInNewContext(source,context);
 const message=data=>events.message({origin:'https://app.scf.usercontent.goog',source:peer,data:{channel:'frameforge.bridge.v1',...data}});
 const tick=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
 message({type:'ready'});
 return {nodes,sent,uploads,message,tick,startScoped:async ids=>{command={id:'scoped',action:'start',project_id:'p',scene_ids:ids};await heartbeat();command=null;await tick();},get saved(){return saved;}};
}
test('ambiguous video failures retain the exact request without automatic model changes or repeated charges',async()=>{
 for(const cause of ['INTERNAL','POLICY','Timeout']){
  const f=fixture();const run=f.nodes[2].click();await f.tick();assert.equal(f.sent.length,1);
  const job=f.sent[0];assert.equal(job.kind,'scene_video');assert.equal(job.modelDisplayName,'Omni 1.1 Flash');
  assert.equal(job.firstFrameImageMediaId,'flow-original');assert.equal(job.idempotency_key,'same-job');
  f.message({type:'error',requestId:job.requestId,error:`Video failed: ${cause}`,cause});
  await run;await f.tick();assert.equal(f.sent.length,1);assert.equal(f.uploads.length,0);
  assert.equal(f.saved.job.requestId,job.requestId);assert.equal(f.saved.job.source_image_sha256,'original-hash');
  assert.match(f.nodes[1].textContent,new RegExp(cause));
 }
});

test('definite SDK rejection records its bound failure and continues the next video once',async()=>{
 const job=id=>({kind:'scene_video',scene_id:id,position:id==='a'?105:118,scene_revision:2,
  source_image_asset_id:'source-'+id,source_image_sha256:'hash-'+id,firstFrameImageMediaId:'flow-'+id,
  model:'gemini-omni-1.1-flash',resolution:'720p',duration_seconds:6,prompt:'Camera moves gently.',idempotency_key:'job-'+id});
 const f=fixture(null,[job('a'),job('b')]);const run=f.nodes[2].click();await f.tick();
 assert.equal(f.sent.length,1);
 f.message({type:'error',requestId:f.sent[0].requestId,error:'Service rejected generation',code:'ERR_GEN_FAILED'});
 await f.tick();assert.equal(f.sent.length,2);assert.equal(f.sent[1].scene_id,'b');
 const failure=JSON.parse(f.uploads[0].data);
 assert.equal(failure.status,'failed');assert.equal(failure.expected_scene_revision,2);
 assert.equal(failure.idempotency_key,'job-a');assert.equal(failure.source_image_asset_id,'source-a');
 assert.equal(failure.source_image_sha256,'hash-a');
 f.message({type:'result',requestId:f.sent[1].requestId,result:{mediaId:'video-b',base64:'AA==',mimeType:'video/mp4'}});
 await run;await f.tick();assert.equal(f.uploads.length,2);assert.match(f.uploads[1].url,/scenes\/b\/video$/);
 assert.equal(f.saved.job,null);assert.equal(f.saved.failedVideoJobs['job-a'].code,'ERR_GEN_FAILED');
 assert.match(f.nodes[1].textContent,/Не созданы кадры: 105/);
});

test('a scoped video command never submits another waiting scene',async()=>{
 const job=id=>({kind:'scene_video',scene_id:id,position:id==='a'?105:118,scene_revision:2,
  source_image_asset_id:'source-'+id,source_image_sha256:'hash-'+id,firstFrameImageMediaId:'flow-'+id,
  model:'gemini-omni-1.1-flash',duration_seconds:6,prompt:'Gentle camera move.',idempotency_key:'job-'+id});
 const f=fixture(null,[job('a'),job('b')]);await f.tick();await f.startScoped(['b']);
 assert.equal(f.sent.length,1);assert.equal(f.sent[0].scene_id,'b');
 f.message({type:'error',requestId:f.sent[0].requestId,error:'Service unavailable: ERR_GEN_FAILED'});
 await f.tick();assert.equal(f.sent.length,1);assert.equal(f.uploads.length,1);
 assert.equal(f.saved.failedVideoJobs['job-b'].code,'ERR_GEN_FAILED');
 assert.equal(f.saved.failedVideoJobs['job-a'],undefined);
});

test('a pause at a definite rejection keeps recovery state and sends no later job',async()=>{
 const f=fixture();const run=f.nodes[2].click();await f.tick();f.nodes[4].click();
 f.message({type:'error',requestId:f.sent[0].requestId,error:'Service rejected generation',code:'ERR_GEN_FAILED'});
 await run;await f.tick();assert.equal(f.sent.length,1);assert.equal(f.uploads.length,0);
 assert.ok(f.saved.job);
});
test('a pause during a failed video request never starts another request',async()=>{
 const f=fixture();const run=f.nodes[2].click();await f.tick();f.nodes[4].click();
 f.message({type:'error',requestId:f.sent[0].requestId,error:'Video generation failed'});
 await run;await f.tick();assert.equal(f.sent.length,1);assert.equal(f.uploads.length,0);
});
test('an unconfirmed saved video is held after restart without another generation',async()=>{
 const f=fixture({projectId:'p',flowProject:'flow',bridgeStateVersion:2,refs:{},done:[],
  job:{kind:'scene_video',scene_id:'s',requestId:'uncertain',idempotency_key:'same-job'},result:null});
 await f.nodes[2].click();assert.equal(f.sent.length,0);assert.equal(f.uploads.length,0);
 assert.match(f.nodes[1].textContent,/не подтверждена/);
});
