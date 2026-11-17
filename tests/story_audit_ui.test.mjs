import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
function context(api){
  const ctx=vm.createContext({current:'p',project:{scenes:Array.from({length:10},(_,i)=>({id:'s'+i}))},
    storyAuditBusy:false,storyAuditStop:false,storyAuditProgress:'',
    sceneDisplayGeneration:()=>({asset_id:'a'}),render:()=>{},refresh:async()=>{},showNotice:()=>{},api});
  vm.runInContext(source.slice(source.indexOf('async function runStoryAudit('),source.indexOf('function renderStoryAudit(')),ctx);
  return ctx;
}
test('audit submits only ready frames in packs of four; repeated clicks do not start another pass',async()=>{
  const calls=[];let release;
  const ctx=context(async(path,body)=>{calls.push({path,body});if(calls.length===1)await new Promise(resolve=>release=resolve);return {items:[{status:'keep'}]};});
  const first=ctx.runStoryAudit();await ctx.runStoryAudit();assert.equal(calls.length,1);release();await first;
  assert.deepEqual(calls.map(x=>Array.from(x.body.scene_ids)),[['s0','s1','s2','s3'],['s4','s5','s6','s7'],['s8','s9']]);
  assert.ok(calls.every(x=>x.path==='/projects/p/story-audit'));
  assert.equal(ctx.storyAuditBusy,false);
});
test('unavailable Gemini stops the audit without starting generation or discarding saved results',async()=>{
  const calls=[];const ctx=context(async(path,body)=>{calls.push(path);return {items:[{status:'unverified'}]};});
  await ctx.runStoryAudit();assert.deepEqual(calls,['/projects/p/story-audit']);assert.equal(ctx.storyAuditBusy,false);
});
test('one malformed verdict does not prevent auditing subsequent packs',async()=>{
  const calls=[];const ctx=context(async(path,body)=>{calls.push(path);return {items:[{status:'unverified'}],stop_audit:false};});
  await ctx.runStoryAudit();assert.equal(calls.length,3);
  assert.ok(calls.every(path=>path==='/projects/p/story-audit'));
});
test('select all checks only actionable recommendations; clear selection makes no API calls',async()=>{
  class Node{
    constructor(tag,text){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};}
    append(...children){this.children.push(...children);}
    addEventListener(){}
  }
  const calls=[],selected=new Set(),box=new Node('section');
  const ctx=vm.createContext({project:{story_audit:[
    {id:'a',status:'meaning',position:1},{id:'b',status:'variety',position:2},
    {id:'c',status:'meaning',position:3,stale:true},{id:'d',status:'meaning',position:4,batch_id:'batch'},
    {id:'e',status:'keep',position:5},{id:'f',status:'unverified',position:6}]},
    isForgottenCitiesProject:()=>true,storyAuditBusy:false,storyAuditExpanded:false,storyAuditProgress:'',storyAuditRegenerating:false,
    storyAuditSelected:selected,el:(tag,text)=>new Node(tag,text),
    button:(text,handler,cls,id)=>{const node=new Node('button',text);node.handler=handler;node.dataset.testid=id;return node;},
    api:async(...args)=>calls.push(args),action:async(...args)=>calls.push(args),current:'p'});
  vm.runInContext(source.slice(source.indexOf('function renderStoryAudit('),source.indexOf('function renderScenes(')),ctx);
  ctx.renderStoryAudit(box);
  const all=node=>[node,...node.children.flatMap(all)],nodes=all(box);
  await nodes.find(n=>n.dataset.testid==='audit-select-all').handler();
  assert.deepEqual([...selected],['a','b']);
  const checks=nodes.filter(n=>n.tag==='input');assert.equal(checks.length,2);assert.ok(checks.every(n=>n.checked));
  await nodes.find(n=>n.dataset.testid==='audit-clear-selection').handler();
  assert.equal(selected.size,0);assert.ok(checks.every(n=>!n.checked));assert.equal(calls.length,0);
});
