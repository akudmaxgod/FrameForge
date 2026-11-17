import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const jsCode=fs.readFileSync('imagegen/static/frameforge-flow-v1.user.js','utf8');
const old=fs.readFileSync('tests/test_bridge_queue.js','utf8');
const context=vm.createContext({vm,jsCode,Buffer,setTimeout,console:{log(){},warn(){},error(){}}});
vm.runInContext(old.slice(old.indexOf('function createUserscriptContext'),old.indexOf('async function runTests')),context);
function harness(){
 const bridge=context.createUserscriptContext(),state={projectId:'p',flowProject:'f',refs:{},done:[],flowImageModel:'🍌 Nano Banana 2'};
 const items=Array.from({length:18},(_,i)=>({id:'p'+i,name:'Атлас / '+i,revision:1,prompt:'Illustration '+i}));
 const started=[],saved=[],messages=[];let active=0,max=0;
 const options={project_id:'p',state,imageConcurrency:8,save(){JSON.stringify(state);},tell:m=>messages.push(m),
  api:async()=>({project_id:'p'}),generate:async()=>{throw Error('wrong generator');},
  generateImageFn:async(job,{state:owner})=>{started.push(job.id);owner.job.submissionPhase='sent';active++;max=Math.max(max,active);await new Promise(r=>setTimeout(r,2+(18-Number(job.id.slice(1)))%7));active--;return {mediaId:'media-'+job.id,base64:'bytes-'+job.id,mimeType:'image/png'};},
  uploadSavedFn:async owner=>{assert.equal(owner.result.mediaId,'media-'+owner.job.id);saved.push(owner.job.id);owner.done.push('props:'+owner.job.id);owner.refs['props:'+owner.job.id]=owner.result.mediaId;owner.job=null;owner.result=null;}};
 return {bridge,state,items,options,started,saved,messages,get max(){return max;}};
}
test('eight real item executions keep out-of-order results isolated and skip locked work',async()=>{
 const h=harness();h.items[0].locked=true;await h.bridge.processReferencePool(h.items,h.options);
 assert.equal(h.max,8);assert.equal(h.saved.length,17);assert.equal(new Set(h.saved).size,17);assert.ok(!h.started.includes('p0'));
 assert.ok(Object.values(h.state.referenceTasks).every(t=>!t.job&&!t.result));
});
test('pause drains all eight submitted jobs without starting another',async()=>{
 const h=harness();let paused=false;const generate=h.options.generateImageFn;
 h.options.isPaused=()=>paused;h.options.generateImageFn=async(...args)=>{const promise=generate(...args);if(h.started.length===8)paused=true;return promise;};
 await h.bridge.processReferencePool(h.items,h.options);assert.equal(h.started.length,8);assert.equal(h.saved.length,8);
});
test('saved upload failure resumes without generating the image twice',async()=>{
 const h=harness();h.options.imageConcurrency=1;const upload=h.options.uploadSavedFn;let fail=true;
 h.options.uploadSavedFn=async owner=>{if(fail){fail=false;throw Error('local upload unavailable');}return upload(owner);};
 await assert.rejects(h.bridge.processReferencePool(h.items,h.options),/upload unavailable/);
 assert.equal(h.started.length,1);assert.equal(h.state.referenceTasks['props:p0:1'].result.base64,'bytes-p0');
 await h.bridge.processReferencePool(h.items,h.options);assert.equal(h.started.filter(id=>id==='p0').length,1);assert.equal(h.saved.length,18);
});
test('an uncertain submitted job is parked instead of submitted again',async()=>{
 const h=harness();h.bridge.referenceTaskState(h.state,h.items[0]).task.job={id:'p0',kind:'props',requestId:'old',submissionPhase:'sent'};
 await h.bridge.processReferencePool(h.items,h.options);assert.ok(!h.started.includes('p0'));assert.equal(h.saved.length,17);
 assert.equal(h.state.referenceTasks['props:p0:1'].job.requestId,'old');
});

test('scientific queue dispatches the reference pool and skips importing completed atlas materials',async()=>{
 const h=harness();h.items[0].locked=true;h.options.api=async(method,path)=>path.includes('/prompts')?{props:h.items,characters:[],locations:[],scenes:[],visual_format:'science_atlas'}:{project_id:'p'};
 await h.bridge.processBridgeQueue({...h.options,batchStage:'scenes'});assert.equal(h.max,8);assert.equal(h.started.length,17);assert.ok(!h.started.includes('p0'));
});
