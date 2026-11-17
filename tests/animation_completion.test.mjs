import test from 'node:test';
import assert from 'node:assert/strict';
import {beginAnimationRun,animationRunProgress} from '../imagegen/static/animation-completion.mjs';
const scene=(id,engine='omni_flash',status='queued_omni',asset=null)=>({id,position:Number(id),data:{animation:{selected_engine:engine,status}},video_asset_id:asset});
test('a mixed run waits for the last cloud clip and excludes unlaunched frames',()=>{
  const scenes=[scene('1'),scene('2','ken_burns','planned_ken_burns'),scene('3','ken_burns','planned_ken_burns')];
  const run=beginAnimationRun(null,scenes.slice(0,2));
  scenes[1].video_asset_id='local';
  let progress=animationRunProgress(run,scenes);assert.equal(progress.finished,false);assert.equal(progress.completed.length,1);
  scenes[0].video_asset_id='cloud';progress=animationRunProgress(run,scenes);assert.equal(progress.finished,true);assert.equal(progress.total,2);
});
test('rebuilding requires a new asset and errors cannot announce success',()=>{
  const scenes=[scene('1','ken_burns','completed','old'),scene('2')];
  const run=beginAnimationRun(null,scenes,{replaceAssets:true});
  assert.equal(animationRunProgress(run,scenes).completed.length,0);
  scenes[0].video_asset_id='new';scenes[1].data.animation.status='failed';
  const progress=animationRunProgress(run,scenes);assert.equal(progress.finished,true);assert.equal(progress.failed.length,1);assert.equal(progress.completed.length,1);
});
test('saved runs restore correctly and a new run resets its dismissal',()=>{
  const scenes=[scene('1')];let run=JSON.parse(JSON.stringify(beginAnimationRun(null,scenes)));
  scenes[0].video_asset_id='new';assert.equal(animationRunProgress(run,scenes).finished,true);
  run.finished=true;run.dismissed=true;run.notified=true;
  const next=beginAnimationRun(run,[scene('2')]);assert.equal(next.dismissed,false);assert.equal(next.notified,false);assert.deepEqual(next.targets.map(t=>t.scene_id),['2']);
});
test('deleted or stale sources require attention; concurrent starts keep prior targets',()=>{
  let run=beginAnimationRun(null,[scene('1')]);run=beginAnimationRun(run,[scene('2')]);
  const stale=scene('1','omni_flash','completed','video');stale.video_stale=true;
  const progress=animationRunProgress(run,[stale]);assert.equal(progress.finished,true);assert.equal(progress.failed.length,2);
});
