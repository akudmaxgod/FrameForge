import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

for(const kind of ['location','prop'])test(`actual ${kind} editor creates a scoped pair, selects and locks only that reference`, async () => {
  const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
  class Node {
    constructor(tag,text){this.tag=tag;this.textContent=text;this.children=[];this.style={};this.dataset={};this.events={};}
    append(...nodes){this.children.push(...nodes);}
    addEventListener(type,handler){this.events[type]=handler;}
    async click(){if(!this.disabled)await this.events.click?.();}
  }
  const main=new Node('main'),calls=[],location={id:'loc1',name:'Город',revision:1,locked:false,references:{}},other={id:'loc2',name:'Руины',revision:1,locked:false,references:{}};
  const project={characters:[],channel_profile:{profile_type:'forgotten_cities'},locations:kind==='location'?[location,other]:[],props:kind==='prop'?[location,other]:[],scenes:[{id:'scene1'}],generations:[],
    preflight:{errors:[]},analysis_metadata:{forgotten_cities:{anchors:[location,other].map(l=>({canonical_name:l.name,anchor_type:'city_master',reference_required:true,time_layer:'historical_reconstruction'}))}}};
  const context=vm.createContext({project,main,current:'project1',currentSettings:{image_backend:'flow'},picked:new Map(),
    selectedLocation:null,selectedProp:null,labels:{queued:'В очереди'},
    document:{createElement:(tag)=>new Node(tag),createTextNode:text=>text},
    isForgottenCitiesProject:()=>true,isStoryProp:()=>true,
    isActiveGeneration:g=>!['succeeded','failed','cancelled','resource_degraded'].includes(g.state),
    attachDragAndDrop:()=>{},makeUploadButton:()=>new Node('button'),progressMeter:()=>new Node('progress'),showNotice:()=>{},image:(asset,alt)=>new Node('img',asset),
    workspace:()=>new Node('section'),
    api:async(path,body)=>{
      calls.push({path,body});
      if(path==='/bridge/v1/status')return {connected:true,reference_casting:true};
      if(path===`/${kind}s/loc1/cast`){
        location.casting={generation_ids:['g1','g2'],selected_asset_id:null};
        project.generations=['g1','g2'].map(id=>({id,purpose:kind,[`${kind}_id`]:'loc1',[`${kind}_revision`]:1,state:'queued'}));
        return {generation_ids:['g1','g2']};
      }
      if(path===`/${kind}s/loc1/select`){location.casting.selected_asset_id=body.asset_id;return {};}
      if(path==='/bridge/v1/command')return {};
      throw Error(`Unexpected API: ${path}`);
    },
    action:async(path,body)=>{calls.push({path,body});assert.equal(path,`/${kind}s/loc1/lock`);location.locked=true;location.references=kind==='prop'?{prop:body.references.item}:body.references;},
  });
  vm.runInContext(source.slice(source.indexOf('function el('),source.indexOf('function showNotice(')),context);
  vm.runInContext(source.slice(source.indexOf('function referenceJobs('),source.indexOf('function renderAnalysisCertificate(')),context);
  vm.runInContext(source.slice(source.indexOf('function renderLocations('),source.indexOf('function editProp(')),context);
  context.render=()=>{main.children=[];context.renderForgottenCitiesWorkspace();if(kind==='location')context.renderLocations();else context.renderProps();};
  context.refresh=async()=>context.render();
  const find=(id,node=main)=>node.dataset?.testid===id?node:node.children?.map(child=>typeof child==='object'?find(id,child):null).find(Boolean);
  context.render();
  assert.equal(calls.length,0,'Opening the workspace must not mutate the queue');
  const texts=(node)=>[node.textContent,...(node.children||[]).flatMap(c=>typeof c==='object'?texts(c):[])];
  assert.ok(!texts(main).includes('Подготовка эталонов'),'No duplicate casting panel');
  await find(`regenerate-${kind}-loc1`).click();
  assert.equal(calls.filter(c=>c.path.endsWith('/cast')).length,1);
  const command=calls.find(c=>c.path==='/bridge/v1/command').body;
  assert.deepEqual(Array.from(command.generation_ids),['g1','g2']);
  assert.equal(find(`regenerate-${kind}-loc1`).disabled,true);
  await find(`regenerate-${kind}-loc1`).click();
  assert.equal(calls.filter(c=>c.path.endsWith('/cast')).length,1);
  project.generations.forEach((g,i)=>{g.state='succeeded';g.asset_id=`asset${i}`;});
  context.render();
  await find(`${kind}-candidate-loc1-1`).click();
  assert.equal(location.casting.selected_asset_id,'asset1');
  context.render(); // Selected variant comes from persisted API state.
  assert.equal(find(`lock-${kind}-loc1`).disabled,false);
  await find(`lock-${kind}-loc1`).click();
  assert.equal(location.references[kind==='location'?'environment':'prop'],'asset1');
  context.render();
  assert.ok(texts(main).includes('asset1'),'Locked reference stays visible in its existing editor');
  assert.equal(other.locked,false);
  assert.equal(calls.filter(c=>/batch|scenes\/|loc2\//.test(c.path)).length,0);
});
