import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
const script=source.slice(source.indexOf('let sceneBatchStarting='),source.indexOf('async function runStoryAudit()'));
function harness(options={}){
 const calls=[],notices=[],handlers=[];let status={connected:true,running:false,project_id:'p',...options.status};
 const node=()=>({append:()=>{},querySelector:()=>null});
 const ctx=vm.createContext({current:'p',project:{id:'p',scenes:Array.from({length:482},(_,i)=>({id:'s'+i}))},currentSettings:{image_backend:options.backend||'flow'},
  flowBridgeStatus:null,setTimeout:fn=>{fn();return 0;},crypto:{randomUUID:()=> 'batch-key'},main:node(),el:node,
  button:(text,fn)=>{const n=node();handlers.push({text,fn,node:n});return n;},
  unconfirmedReadyReferences:()=>[],sceneDisplayGeneration:()=>null,
  autoAcceptReadyReferences:async()=>{calls.push({path:'accept'});if(options.accept)await options.accept(ctx);},
  refresh:async()=>calls.push({path:'refresh'}),render:()=>calls.push({path:'render'}),
  sceneBatchBlockReason:()=>options.block||'',showNotice:(message)=>notices.push(message),
  action:async(path,data)=>{calls.push({path,data});return {};},
  api:async(path,data)=>{
   calls.push({path,data});
   if(path==='/bridge/v1/status')return {...status};
   if(path==='/bridge/v1/command'){
    if(options.commandError)throw Error(options.commandError);
    const id=data.action+'-id';
    if(!options.noAck)status={...status,project_id:'p',last_ack_command_id:id,running:data.action==='start'&&!options.stopped,status:options.stopped||''};
    return {command_id:id};
   }
   throw Error('Unexpected API '+path);
  }});
 vm.runInContext(script,ctx);ctx.renderBatchActionBar();
 const click=handlers.find(b=>/Сгенерировать все/.test(b.text)).fn;
 return {ctx,calls,notices,click};
}
test('actual all-scenes button dispatches Flow image-only start instead of a local 482-item batch',async()=>{
 const h=harness();await h.click();
 const starts=h.calls.filter(c=>c.data?.action==='start');assert.equal(starts.length,1);
 assert.equal(starts[0].data.project_id,'p');assert.equal(starts[0].data.images_only,true);
 assert.equal(h.calls.some(c=>c.path.includes('/batch')),false);
 assert.ok(h.calls.findIndex(c=>c.path==='accept')<h.calls.indexOf(starts[0]));
 assert.ok(h.notices.some(n=>/Flow подтвердил запуск/.test(n)));
});
test('disconnected bridge fails before accepting references or creating jobs',async()=>{
 const h=harness({status:{connected:false}});await assert.rejects(h.click(),/Мост Flow не подключён/);
 assert.equal(h.calls.some(c=>c.path==='accept'||c.data?.action==='start'||c.path.includes('/batch')),false);
});
test('bridge is rebound to active FrameForge project and check is acknowledged before start',async()=>{
 const h=harness({status:{project_id:'previous'}});await h.click();
 assert.deepEqual(h.calls.filter(c=>c.path==='/bridge/v1/command').map(c=>c.data.action),['check','start']);
});
test('running or pending start cannot dispatch a duplicate',async()=>{
 for(const status of [{running:true},{pending_command:'start'}]){
  const h=harness({status});await h.click();assert.equal(h.calls.some(c=>c.path==='accept'||c.data?.action==='start'),false);
 }
});
test('a busy different project is never rebound or interrupted',async()=>{
 const h=harness({status:{running:true,project_id:'other'}});await assert.rejects(h.click(),/другим проектом/);
 assert.equal(h.calls.some(c=>c.path==='/bridge/v1/command'),false);
});
test('failed start is reported without a false successful launch or orphan local batch',async()=>{
 const h=harness({commandError:'Flow unavailable'});await assert.rejects(h.click(),/Flow unavailable/);
 assert.equal(h.notices.some(n=>/подтвердил запуск/.test(n)),false);assert.equal(h.calls.some(c=>c.path.includes('/batch')),false);
});
test('missing acknowledgment cannot be advertised as a running generation',async()=>{
 const h=harness({noAck:true});await assert.rejects(h.click(),/не подтвердил команду/);
 assert.equal(h.notices.some(n=>/подтвердил запуск/.test(n)),false);
});
test('acknowledged but stopped bridge displays its real failure',async()=>{
 const h=harness({stopped:'Сервис создания изображений недоступен'});
 await assert.rejects(h.click(),/Сервис создания изображений недоступен/);
 assert.equal(h.notices.some(n=>/подтвердил запуск/.test(n)),false);
});
test('project changes during reference acceptance cannot start the wrong project',async()=>{
 const h=harness({accept:async ctx=>{ctx.current='other';}});await assert.rejects(h.click(),/проект.*изменился/);
 assert.equal(h.calls.some(c=>c.data?.action==='start'),false);
});
test('preflight error still blocks Flow generation',async()=>{
 const h=harness({block:'Required reference is missing'});await assert.rejects(h.click(),/Required reference/);
 assert.equal(h.calls.some(c=>c.data?.action==='start'),false);
});
test('other image backends retain the normal local batch behavior',async()=>{
 const h=harness({backend:'comfy'});await h.click();
 assert.equal(h.calls.filter(c=>c.path==='/projects/p/batch').length,1);
 assert.equal(h.calls.some(c=>c.path==='/bridge/v1/command'),false);
});
test('two concurrent clicks result in one start command',async()=>{
 let finish;const pending=new Promise(resolve=>finish=resolve);const h=harness({accept:()=>pending});
 const first=h.click();await new Promise(resolve=>setImmediate(resolve));await h.click();finish();await first;
 assert.equal(h.calls.filter(c=>c.data?.action==='start').length,1);
});
