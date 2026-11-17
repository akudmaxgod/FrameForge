import test from 'node:test';
import assert from 'node:assert/strict';
import {normalize} from '../imagegen/static/montage-timeline.mjs';
import {trimTimelineItem} from '../imagegen/static/montage-lanes.mjs';
test('short Omni footage fits voice interval and next scene starts at the voice boundary',()=>{
  const doc={clips:[{edit_clip_id:'omni',trim_in_ms:0,trim_out_ms:8000,playback_rate:.8,source_duration_ms:8000},{edit_clip_id:'local',trim_in_ms:0,trim_out_ms:6000}],transitions:[],audio_tracks:[{role:'voice',trim_in_ms:0,trim_out_ms:16000,offset_ms:0}]};
  let timeline=normalize(doc);assert.equal(timeline.clips[1].start_ms,10000);assert.equal(timeline.duration_ms,16000);
  trimTimelineItem(doc,{kind:'video',id:'omni',edge:'end'},-1000);
  timeline=normalize(doc);assert.equal(timeline.clips[1].start_ms,9000);assert.equal(doc.audio_tracks[0].trim_out_ms,16000);
});
