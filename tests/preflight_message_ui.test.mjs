import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
function message(project){
  const context=vm.createContext({project,isStoryProp:prop=>prop.importance!=='incidental'});
  vm.runInContext(source.slice(source.indexOf('function sceneBatchBlockReason('),source.indexOf('function renderScenes(')),context);
  return context.sceneBatchBlockReason();
}
test('missing references list actual names without requesting another analysis',()=>{
  const result=message({preflight:{ok:false,errors:[{code:'missing_required_reference',message:'Missing seal'}]},
    characters:[],locations:[{name:'Город',locked:true},{name:'Главный вид',locked:false},{name:'Руины',locked:false}],
    props:[{name:'Печать',locked:false},{name:'Декор',locked:false,importance:'incidental'}]});
  for(const name of ['Главный вид','Руины','Печать'])assert.ok(result.includes(name));
  assert.ok(!result.includes('Декор'));
  assert.ok(!result.includes('ошибки профильного анализа'));
  assert.ok(source.includes('const reason=sceneBatchBlockReason();if(reason)throw Error(reason)'));
});
test('real profile faults keep their concrete reason',()=>{
  const result=message({preflight:{ok:false,errors:[{code:'ir_invariant',message:'Не определён предмет сцены'}]},locations:[{name:'Руины',locked:false}]});
  assert.ok(result.includes('Не определён предмет сцены'));
});
test('completed setup permits the explicit generation action',()=>{
  assert.equal(message({preflight:{ok:true,errors:[]},locations:[{name:'Город',locked:true}],props:[{name:'Печать',locked:true}]}),'');
});
