import test from 'node:test';
import assert from 'node:assert/strict';
import {setClipSpeed,segmentDuration} from '../imagegen/static/montage-layers.mjs';
import {trimTimelineItem} from '../imagegen/static/montage-lanes.mjs';
import {normalize} from '../imagegen/static/montage-timeline.mjs';
const fixture=()=>({clips:[{edit_clip_id:'a',source_kind:'scene',source_duration_ms:8000,trim_in_ms:0,trim_out_ms:8000,muted:false,
 footage_overlay:{source_kind:'footage',source_duration_ms:8000,trim_in_ms:0,trim_out_ms:8000,muted:false}}],transitions:[],audio_tracks:[{role:'voice',trim_in_ms:0,trim_out_ms:8000}]});
test('speed changes footage duration without demanding a longer original, source asset remains intact',()=>{
 const doc=fixture(),audio=structuredClone(doc.audio_tracks);setClipSpeed(doc,'a',true,2);
 assert.equal(segmentDuration(doc.clips[0]),4000);assert.equal(normalize(doc).duration_ms,4000);
 assert.equal(doc.clips[0].footage_overlay.trim_out_ms,8000);
 setClipSpeed(doc,'a',true,.5);assert.equal(segmentDuration(doc.clips[0]),16000);
 assert.equal(doc.clips[0].trim_out_ms,8000);assert.equal(doc.clips[0].hold_last_ms,8000);
 assert.equal(normalize(doc).duration_ms,16000);assert.deepEqual(doc.audio_tracks,audio);
 setClipSpeed(doc,'a',true,1);assert.equal(segmentDuration(doc.clips[0]),8000);assert.equal(doc.clips[0].hold_last_ms,0);
});
test('overlay edge trim follows playback speed and lane mute preserves individual settings',()=>{
 const doc=fixture();setClipSpeed(doc,'a',true,2);
 trimTimelineItem(doc,{kind:'overlay',id:'a',edge:'end'},-1000);
 assert.equal(normalize(doc).duration_ms,3000);assert.equal(doc.clips[0].footage_overlay.trim_out_ms,6000);
 doc.clips[0].footage_overlay.muted=false;doc.footage_muted=true;
 assert.equal(normalize(doc).clips[0].muted,true);assert.equal(doc.clips[0].footage_overlay.muted,false);
 doc.footage_muted=false;assert.equal(normalize(doc).clips[0].muted,false);
});
