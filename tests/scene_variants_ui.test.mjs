import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
function variants(scene,generations){
  const context=vm.createContext({project:{generations}});
  vm.runInContext(source.slice(source.indexOf('function sceneGenerations('),source.indexOf('function sceneDisplayGeneration(')),context);
  return Array.from(context.sceneVariants(scene));
}

test('one uploaded image shared by two queues appears once, retaining selected generation',()=>{
  const scene={id:'s1',current_revision:1,active_generation_id:'g2'};
  const generations=['g1','g2'].map(id=>({id,scene_id:'s1',scene_revision:1,state:'succeeded',asset_id:'image1'}));
  const result=variants(scene,generations);
  assert.equal(result.length,1);
  assert.equal(result[0].id,'g2');
  assert.match(source,/const variants = sceneVariants\(scene\);/);
});

test('distinct images stay selectable while other scenes, queued jobs and old revisions are excluded',()=>{
  const scene={id:'s1',current_revision:2,active_generation_id:'g2'};
  const generations=[
    {id:'g1',scene_id:'s1',scene_revision:2,state:'succeeded',asset_id:'image1'},
    {id:'g2',scene_id:'s1',scene_revision:2,state:'succeeded',asset_id:'image2'},
    {id:'old',scene_id:'s1',scene_revision:1,state:'succeeded',asset_id:'image3'},
    {id:'queued',scene_id:'s1',scene_revision:2,state:'queued'},
    {id:'other',scene_id:'s2',scene_revision:2,state:'succeeded',asset_id:'image4'},
  ];
  assert.deepEqual(variants(scene,generations).map(g=>g.id),['g1','g2']);
});
