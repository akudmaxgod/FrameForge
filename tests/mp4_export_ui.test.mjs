import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../imagegen/static/app.js',import.meta.url),'utf8');
function context(api){
  const notices=[];
  const ctx=vm.createContext({api,refresh:async()=>{},showNotice:message=>notices.push(message)});
  vm.runInContext(source.slice(source.indexOf('async function assembleProjectMp4('),source.indexOf('function sceneGenerations(')),ctx);
  return {ctx,notices};
}
test('assembly reports progress and success without a blocked popup',async()=>{
  let release;const {ctx,notices}=context(async()=>{await new Promise(resolve=>release=resolve);return {url:'/api/assets/video'};});
  const control={textContent:'Собрать MP4'};const pending=ctx.assembleProjectMp4('p',control);
  assert.equal(control.textContent,'Собираю MP4…');release();await pending;
  assert.equal(control.textContent,'Собрать MP4');assert.match(notices.at(-1),/Скачать MP4/);
});
test('assembly failure returns the actual server reason and resets button',async()=>{
  const {ctx}=context(async()=>{throw Error('Нет анимации кадра 4');});const control={textContent:'Собрать MP4'};
  await assert.rejects(ctx.assembleProjectMp4('p',control),/Нет анимации кадра 4/);
  assert.equal(control.textContent,'Собрать MP4');
});
