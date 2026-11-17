import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto,createHash} from 'node:crypto';
const imageBytes=new Uint8Array([137,80,78,71]);
const imageSha=createHash('sha256').update(imageBytes).digest('hex');
import {readFileSync} from 'node:fs';
import {animationLaunchPlan,pendingCloudAnimation} from '../imagegen/static/animation-launch.mjs';

const source=readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js',import.meta.url),'utf8');
const video=(id='s1',requestId='old')=>({kind:'scene_video',id,scene_id:id,position:1,requestId,scene_revision:2,
 source_image_asset_id:'image-'+id,source_image_sha256:imageSha,idempotency_key:'key-'+id,model:'gemini-omni-1.1-flash'});
const job=id=>({...video(id,'new-'+id),engine:'omni_flash',firstFrameImageMediaId:'media-'+id,prompt:'Animate supplied image',duration_seconds:6});
function fixture(initial={},jobs=[]){
 const events={},calls=[],sent=[],nodes=[],store=new Map();
 store.set('ff-flow-v1-state:p:flow',{projectId:'p',flowProject:'flow',refs:{},done:[],job:null,result:null,...initial});
 const message=m=>events.message({source:peer,origin:'https://app.scf.usercontent.goog',data:{channel:'frameforge.bridge.v1',...m}});
 const peer={postMessage(m){sent.push(m);setImmediate(()=>{
   if(m.type==='import_source_image')message({type:'source_image_imported',requestId:m.requestId,mediaId:'fresh-'+m.scene_id,source_image_asset_id:m.source_image_asset_id,source_image_sha256:m.source_image_sha256});
   if(m.type==='scene_video')message({type:'result',requestId:m.requestId,result:{base64:'AA==',mimeType:'video/mp4',mediaId:'clip-'+m.scene_id}});
 });}};
 const context={URL,Proxy,Buffer,console:{log(){},warn(){},error(){}},setTimeout,clearTimeout,setInterval:()=>0,crypto:{subtle:webcrypto.subtle,randomUUID:()=>Math.random().toString(36)},
  location:{pathname:'/project/flow',origin:'https://flow.google.com'},
  GM_getValue:(key,fallback)=>store.get(key)??fallback,
  GM_setValue:(key,value)=>store.set(key,JSON.parse(JSON.stringify(value))),
  GM_xmlhttpRequest:options=>{
   if(options.url.includes('/api/assets/')){options.onload({status:200,response:imageBytes.buffer,responseHeaders:'Content-Type: image/png'});return;}
   const data=options.data?JSON.parse(options.data):{};calls.push({url:options.url,data,method:options.method});
   let response={};
   if(options.url.endsWith('/current'))response={project_id:'p'};
   else if(options.url.endsWith('/settings'))response={};
   else if(options.url.endsWith('/animation/jobs'))response={jobs,plan_mode:'selective'};
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

test('launch partitions fresh, queued, generating and completed without treating queue as missing image',()=>{
 const scenes=[{id:'fresh'},{id:'q',data:{animation:{status:'queued_omni'}}},{id:'g',data:{animation:{status:'generating_veo'}}},
  {id:'done',video_asset_id:'video'},{id:'bad'}];
 const plan=animationLaunchPlan(scenes,scenes.map(s=>s.id),s=>s.id==='fresh');
 assert.deepEqual(plan.fresh.map(s=>s.id),['fresh']);assert.deepEqual(plan.resume.map(s=>s.id),['q','g']);
 assert.deepEqual(plan.unavailable.map(s=>s.id),['done','bad']);assert.equal(pendingCloudAnimation(scenes[3]),false);
});
test('animation-only run preserves unknown static work and submits only selected videos',async()=>{
 const old={kind:'scenes',id:'old-image',requestId:'image-request'};
 const f=fixture({job:old,error:'timeout'},[job('s1'),job('s2')]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,1);assert.equal(f.sentVideo()[0].scene_id,'s1');assert.equal(f.sentVideo()[0].type,'scene_video');
 assert.deepEqual(f.state().deferredImageWork.job,old);
 assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,1);
 assert.ok(!f.calls.some(c=>c.url.includes('/upload')||c.url.includes('/prompts')));
});
test('unknown video is never resubmitted and does not erase or block other queued scenes',async()=>{
 const old=video();const f=fixture({job:old,error:'Flow timeout'},[job('s1'),job('s2')]);
 await f.bridge.run(['s1','s2'],null,false,null,'p',true);
 assert.deepEqual(f.sentVideo().map(m=>m.scene_id),['s2']);assert.deepEqual(f.state().videoTasks.old.job,old);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,1);
});
test('saved video uploads before queue is fetched and is never generated again',async()=>{
 const f=fixture({job:video(),result:{mimeType:'video/mp4',base64:'AA==',mediaId:'saved'}},[]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,0);assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,1);
 assert.ok(f.calls.findIndex(c=>c.url.endsWith('/video'))<f.calls.findIndex(c=>c.url.endsWith('/animation/jobs')));
 assert.equal(f.state().videoTasks.old.job,null);
});
test('late exact video result remains recoverable after request was parked',async()=>{
 const f=fixture({job:video(),error:'timeout'},[]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 f.message({type:'scene_video_result',requestId:'foreign',result:{mimeType:'video/mp4',base64:'AA=='}});
 assert.equal(f.state().videoTasks.old.result,null);
 f.message({type:'scene_video_result',requestId:'old',result:{mimeType:'video/mp4',base64:'AA==',mediaId:'late'}});
 await f.bridge.run(['s1'],null,false,null,'p',true);
 assert.equal(f.sentVideo().length,0);assert.equal(f.calls.filter(c=>c.url.endsWith('/video')).length,1);
});
test('definite video rejection is acknowledged with original binding; timeout remains unknown',async()=>{
 const f=fixture({job:video(),error:'ERR_GEN_FAILED'},[]);
 await f.bridge.run(['s1'],null,false,null,'p',true);
 const progress=f.calls.find(c=>c.url.endsWith('/animation/progress'));
 assert.equal(progress.data.idempotency_key,'key-s1');assert.equal(progress.data.status,'failed');assert.equal(f.sentVideo().length,0);
});
test('a provably unsent video may be submitted; reset refuses parked unknown tasks',async()=>{
 const f=fixture({job:{...video(),submissionPhase:'not_sent'}},[job('s1')]);
 await f.bridge.run(['s1'],null,false,null,'p',true);assert.equal(f.sentVideo().length,1);
 const unknown=fixture({job:video(),error:'timeout'},[]);
 await unknown.bridge.run(['s1'],null,false,null,'p',true);
 await unknown.nodes[5].click();
 assert.ok(unknown.state().videoTasks.old.job);
});
