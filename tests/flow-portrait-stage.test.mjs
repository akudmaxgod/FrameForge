import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const app = fs.readFileSync(new URL('../imagegen/static/app.js', import.meta.url), 'utf8');
const bridge = fs.readFileSync(new URL('../imagegen/static/frameforge-flow-v1.user.js', import.meta.url), 'utf8');

test('the same button prepares missing portraits, then starts scenes only after confirmation', async () => {
  const calls = [];
  let acknowledged, control, confirmed = false;
  const node = () => ({append() {}});
  const subject = {id:'hero', revision:2, references:{}, locked:false};
  const project = {id:'p', scenes:[{id:'scene'}], generations:[]};
  const context = vm.createContext({
    current:'p', project, currentSettings:{image_backend:'flow'}, picked:new Map(),
    el:node, main:node(), render() {}, sceneDisplayGeneration:()=>null,
    referenceSubjects:()=>[subject], sceneBatchBlockReason:()=>null,
    refresh:async()=>{}, showNotice(){}, setTimeout(fn){fn();}, confirm:()=>confirmed,
    button(text, click, _style, id){
      if(id==='generate-scenes')control={text,click};
      return node();
    },
    async api(path, data) {
      if(path==='/bridge/v1/status')return {
        connected:true, staged_image_batch:true, project_id:'p', last_ack_command_id:acknowledged,
        status:'Портреты готовы к проверке', running:acknowledged==='scenes',
      };
      calls.push({path,data});
      if(path==='/bridge/v1/command'){
        acknowledged=data.batch_stage;
        return {command_id:acknowledged};
      }
      if(path==='/characters/hero/lock'){
        subject.locked=true;
        subject.references=data.references;
      }
      return {};
    },
  });
  vm.runInContext(app.slice(app.indexOf('let sceneBatchStarting='), app.indexOf('async function runStoryAudit(')), context);
  context.renderBatchActionBar();
  assert.match(control.text, /Сгенерировать портреты/);
  await control.click();
  assert.equal(calls[0].data.batch_stage, 'portraits');
  assert.equal(calls.length, 1);

  project.generations.push({character_id:'hero', character_revision:2, state:'succeeded', asset_id:'image'});
  context.renderBatchActionBar();
  assert.match(control.text, /Подтвердить портреты/);
  await control.click();
  assert.equal(calls.length, 1);
  confirmed=true;
  await control.click();
  assert.equal(calls[1].path, '/characters/hero/lock');
  assert.equal(calls[1].data.references.portrait, 'image');
  assert.equal(calls[2].data.batch_stage, 'scenes');
});

test('portrait stage skips reviewed and existing variants, registry entries and scenes', async () => {
  const processed = [];
  const context = vm.createContext({
    processBridgeItem:async (item,kind)=>{processed.push([kind,item.id]);return {status:'succeeded'};},
  });
  vm.runInContext(bridge.slice(bridge.indexOf('async function processBridgeQueue('), bridge.indexOf('async function run(')), context);
  const manifest = {
    characters:[{id:'ready',variants_count:1},{id:'locked',portrait_confirmed:true},{id:'missing'}],
    manual_characters:[{id:'registry'}], locations:[{id:'place'}], props:[{id:'prop'}],
    scenes:[{scene_id:'scene'}],
  };
  await context.processBridgeQueue({
    project_id:'p', batchStage:'portraits', api:async (_method,path)=>{
      assert.match(path,/references_only=1$/);
      return manifest;
    },
  });
  assert.deepEqual(processed, [['characters','missing']]);
  processed.length=0;
  const requests = [];
  await context.processBridgeQueue({project_id:'p', batchStage:'scenes', api:async (_method,path)=>{
    requests.push(path);
    return path.includes('?') ? {...manifest, scenes:[]} : manifest;
  }});
  assert.deepEqual(requests, ['/api/projects/p/prompts?references_only=1', '/api/projects/p/prompts']);
  assert.deepEqual(processed.map(([kind])=>kind), ['locations','props','scenes']);
});

test('saved scene result survives an isolated portrait run', () => {
  const context=vm.createContext({});
  vm.runInContext(bridge.slice(bridge.indexOf('function scopeSavedWork('), bridge.indexOf('async function run(')),context);
  const state={job:{kind:'scenes',id:'scene'},result:{mediaId:'previous'},error:'saved'};
  context.scopeSavedWork(state,'portraits');
  assert.equal(state.job,null);
  assert.equal(state.deferredSceneWork.job.id,'scene');
  context.scopeSavedWork(state,'scenes');
  assert.equal(state.result.mediaId,'previous');
  assert.equal(state.error,'saved');
  assert.equal(state.deferredSceneWork,undefined);
});

test('manifest and upload timeouts have separate deadlines and messages', async () => {
  const requests = [];
  const context = vm.createContext({GM_xmlhttpRequest(request){
    requests.push(request);
    request.ontimeout();
  }});
  vm.runInContext(bridge.slice(bridge.indexOf('function api('), bridge.indexOf('async function createClientCompositeReference(')), context);
  await assert.rejects(context.api('GET', '/api/projects/p/prompts?references_only=1'), /при подготовке промптов/);
  await assert.rejects(context.api('POST', '/api/scenes/s/upload', {}), /результат сохранён в мосте/);
  assert.ok(requests[0].timeout > 30000);
  assert.equal(requests[1].timeout, 90000);
});
