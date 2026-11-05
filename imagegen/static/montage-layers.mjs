export const segmentDuration=c=>(c.trim_out_ms-c.trim_in_ms)/(c.playback_rate??1)+(c.hold_last_ms||0);
export function setClipSpeed(doc,id,layerSelected,speed){
 if(!Number.isFinite(speed)||speed<.05||speed>4)throw Error('Скорость должна быть от 0.05× до 4×.');
 const base=doc.clips.find(c=>c.edit_clip_id===id);if(!base)return;
 const draft=structuredClone(base),clip=layerSelected&&draft.footage_overlay?draft.footage_overlay:draft;
 if(clip.media_type==='image')return;
 clip.playback_rate=speed;clip.muted=true;clip.manual_timing=clip.timing_locked=true;
 if(clip!==draft){
   const span=segmentDuration(clip);if(span<1000/24||span>86400000)throw Error('Длительность сегмента выходит за допустимые границы.');
   const baseRate=draft.playback_rate??1,moving=Math.min(span,(draft.source_duration_ms-draft.trim_in_ms)/baseRate);
   draft.trim_out_ms=draft.trim_in_ms+moving*baseRate;draft.hold_last_ms=Math.max(0,span-moving);
   if(draft.hold_last_ms)draft.muted=true;
   draft.manual_timing=draft.timing_locked=true;
 }
 Object.assign(base,draft);
}
export function effectiveClip(base){
 const layer=base.footage_overlay;if(!layer||layer.enabled===false)return structuredClone(base);
 const c=structuredClone(layer);c.edit_clip_id=base.edit_clip_id;c.excluded=base.excluded||false;
 c.narration_origin_clip_id=base.narration_origin_clip_id||base.edit_clip_id;
 c.trim_out_ms=c.trim_in_ms+(segmentDuration(base)-(c.hold_last_ms||0))*(c.playback_rate??1);c.footage_overlay=structuredClone(layer);return c;
}
export function overlayFootage(doc,id,footage){
 const base=doc.clips.find(c=>c.edit_clip_id===id);
 if(!base||base.source_kind==='footage')return false;
 const layer=structuredClone(footage),span=segmentDuration(base);
 if(layer.media_type!=='image'&&layer.trim_in_ms+span>layer.source_duration_ms+.01)throw Error('Футаж короче выбранного сегмента. Выберите более длинное видео.');
 layer.edit_clip_id=base.edit_clip_id;layer.origin_scene_id=base.scene_id;layer.enabled=true;layer.hold_last_ms=0;layer.playback_rate=1;
 layer.trim_out_ms=layer.trim_in_ms+span;layer.excluded=false;base.footage_overlay=layer;return true;
}
