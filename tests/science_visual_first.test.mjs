import test from 'node:test';
import assert from 'node:assert/strict';
import {visibleNodes,focusState,playbackCommands} from '../imagegen/static/montage-science-atlas.mjs';
test('a new spotlight replaces the previous one at its audio cue',()=>{
 const p={nodes:[],elements:[{}],annotations:[{},{}],cue_ms:[null,0,1200]};
 assert.deepEqual(visibleNodes(p,.3),[0,1]);assert.deepEqual(visibleNodes(p,1.3),[0,2]);
});
test('static frame preview retains annotations before timing is available',()=>{
 assert.deepEqual(visibleNodes({nodes:[],elements:[],annotations:[{},{}]},0),[0,1]);
});

test('focus moves between illustration details with easing',()=>{const p={commands:[{type:'ring',cue_index:0,x:200,y:300},{type:'ring',cue_index:1,x:800,y:300}],cue_ms:[0,1000]};assert.equal(focusState(p,1.175).x,500);assert.equal(focusState(p,1.175).phase,.5);assert.equal(focusState(p,1.5).x,800);});

test('static schematic rings are not timed attention markers',()=>{assert.equal(focusState({commands:[{type:'ring',x:100,y:100}],cue_ms:[0]},0),null);});

test('saved measurement schemes acquire arrows, drawing and typing without modifying source',()=>{
 const raw=[{type:'ring',x:500,y:300,radius:100},{type:'line',x1:400,y1:300,x2:600,y2:300},{type:'text',text:'24 mm',size:34}];
 const commands=playbackCommands({presentation:'visual_first',explanatory_scheme:{kind:'measurement'},commands:raw});
 assert.equal(commands.length,7);assert.equal(commands.find(c=>c.text)?.typing_cps,24);assert.ok(commands[0].draw_ms);assert.equal(raw[0].draw_ms,undefined);
});
