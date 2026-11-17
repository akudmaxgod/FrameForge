import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
class Node{constructor(tag,text){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};}append(...items){this.children.push(...items);}}
function fixture(mode='dynamic'){
 const characters=Array.from({length:28},(_,i)=>({id:'c'+i,name:'Person '+i,references:{},locked:i<20}));
 const project={mode,characters,character_reference_ids:characters.slice(0,20).map(c=>c.id),character_selection_issues:[],
 reference_requirements:characters.slice(0,20).map(c=>({id:c.id,name:c.name,kind:'character',ready:true})),generations:[],preflight:{ok:true,errors:[]}};
 const main=new Node('main');
 const context=vm.createContext({project,main,selectedSubject:'c0',workspace:(title)=>new Node('section',title),
 el:(tag,text)=>new Node(tag,text),button:(text,fn,cls,id)=>{const n=new Node('button',text);n.id=id;n.click=fn;return n;},
 picked:new Map(),isActiveGeneration:()=>false,latestSubjectAsset:()=>null,image:()=>new Node('img'),renderSubjectDetail:c=>new Node('detail',c.id),render:()=>{}});
 vm.runInContext(source.slice(source.indexOf('function referenceSubjects('),source.indexOf('function latestSubjectAsset(')),context);
 vm.runInContext(source.slice(source.indexOf('function getCandidateReferenceAsset('),source.indexOf('function unconfirmedReadyReferences(')),context);
 vm.runInContext(source.slice(source.indexOf('function sceneBatchBlockReason('),source.indexOf('async function runStoryAudit(')),context);
 return {context,project,main};
}
test('main UI shows twenty, separate collapsed registry keeps eight',()=>{
 const {context,main,project}=fixture();context.renderSubjects();
 const box=main.children[0],tabs=box.children[0],registry=box.children.find(n=>n.tag==='details');
 assert.equal(tabs.children.length,20);assert.equal(registry.children[2].children.length,8);assert.ok(!registry.open);
 assert.equal(project.characters.length,28);
});
test('unlocked reference-only people do not block scenes; selected person does',()=>{
 const {context,project}=fixture();assert.equal(context.sceneBatchBlockReason(),'');
 project.reference_requirements[0].ready=false;
 assert.ok(context.sceneBatchBlockReason().includes('Person 0'));assert.ok(!context.sceneBatchBlockReason().includes('Person 20'));
});
test('manual registry selection and illustration mode remain available',()=>{
 const {context,main}=fixture();context.selectedSubject='c23';context.renderSubjects();
 assert.equal(main.children[0].children.find(n=>n.tag==='details').open,true);
 assert.equal(main.children[0].children.find(n=>n.tag==='detail').textContent,'c23');
 const illustrated=fixture('illustration');illustrated.context.renderSubjects();
 assert.ok(!illustrated.main.children[0].children.some(n=>n.tag==='details'));
});
test('actual bridge queue uses automatic manifest and explicit manual list only',async()=>{
 const bridge=fs.readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js',import.meta.url),'utf8');
 const manifest={characters:Array.from({length:20},(_,i)=>({id:'c'+i})),manual_characters:[],scenes:[],locations:[],props:[]};
 const calls=[],context=vm.createContext({processBridgeItem:async(item,kind)=>{calls.push([kind,item.id]);return {status:'done'};}});
 vm.runInContext(bridge.slice(bridge.indexOf('async function processBridgeQueue('),bridge.indexOf('async function run(')),context);
 await context.processBridgeQueue({project_id:'p',api:async()=>manifest});assert.equal(calls.length,20);
 calls.length=0;manifest.manual_characters=[{id:'explicit-c23'}];
 await context.processBridgeQueue({project_id:'p',api:async()=>manifest});assert.equal(calls.length,21);
 assert.deepEqual(calls.at(-1),['characters','explicit-c23']);
});
