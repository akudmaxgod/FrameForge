import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {STYLE_PRESETS,presetForStyle} from '../imagegen/static/ui-config.mjs';
test('marker preset is discoverable while existing default stays realism',()=>{
 const marker=STYLE_PRESETS.find(x=>x.id==='marker-explainer');assert.ok(marker);
 assert.equal(marker.name,'Маркерный эксплейнер');assert.equal(presetForStyle(marker.prompt).id,marker.id);
 assert.equal(presetForStyle('old unknown style').id,'realism');
 for(const term of ['oversized pure white round heads','thick black marker outlines','flat muted','story-required architecture'])assert.ok(marker.prompt.includes(term));
 assert.ok(fs.existsSync(new URL('../imagegen/static'+marker.image,import.meta.url)));
});
