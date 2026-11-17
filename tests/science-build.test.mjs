import test from 'node:test';
import assert from 'node:assert/strict';
import {buildScienceMovie} from '../imagegen/static/science-build.mjs';
function fixture(){
 const calls=[],state={revision:1,doc:{clips:[{scene_id:'s'}],audio_tracks:[{role:'voice',enabled:true}]}};
 let saved;const options={id:'p',getState:()=>state,flush:async()=>calls.push('flush'),synchronize:async()=>calls.push('sync'),progress:()=>{},wait:async()=>{},saveCheckpoint:c=>{saved=c;},accept:r=>{state.revision=r.revision;state.doc=r.document;},api:async(path,data)=>{
  calls.push({path,data});if(path.endsWith('/voice-fit'))return {id:'fit',status:'queued'};
  if(path.endsWith('/voice-fit/fit'))return {id:'fit',status:'ready',proposal:{can_apply:true}};
  if(path.endsWith('/apply'))return {revision:2,document:state.doc};
  if(path.endsWith('/export'))return {id:'mp4',status:'queued'};
  throw Error(path);
 }};return {options,calls,state,get saved(){return saved;}};
}
test('whole movie aligns phrases, applies timing and exports the saved revision once',async()=>{
 const h=fixture();const job=await buildScienceMovie(h.options);assert.equal(job.id,'mp4');
 const fit=h.calls.find(c=>c.path?.endsWith('/voice-fit'));assert.equal(fit.data.timing_mode,'phrases');assert.equal(fit.data.short_video_policy,'retime');
 const exported=h.calls.filter(c=>c.path?.endsWith('/export'));assert.equal(exported.length,1);assert.equal(exported[0].data.expected_revision,2);assert.ok(exported[0].data.request_key);assert.equal(h.saved.export_id,'mp4');
});
test('missing narration or cancelled preparation never starts an export',async()=>{
 const h=fixture();h.state.doc.audio_tracks=[];await assert.rejects(buildScienceMovie(h.options),/озвучку/);assert.ok(!h.calls.some(c=>c.path?.endsWith('/export')));
 const j=fixture();j.options.cancelled=()=>true;const api=j.options.api;j.options.api=async(p,d)=>p.endsWith('/cancel')?{}:api(p,d);
 await assert.rejects(buildScienceMovie(j.options),/остановлена/);assert.ok(!j.calls.some(c=>c.path?.endsWith('/export')));
});

test('resume reuses the saved alignment and stable export key',async()=>{
 const h=fixture();h.options.checkpoint={fit_id:'fit',request_key:'same'};await buildScienceMovie(h.options);
 assert.ok(!h.calls.includes('sync'));assert.ok(!h.calls.some(c=>c.path?.endsWith('/voice-fit')));
 assert.equal(h.calls.find(c=>c.path?.endsWith('/export')).data.request_key,'same');
});
