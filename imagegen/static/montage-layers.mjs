export const segmentDuration=c=>(c.trim_out_ms-c.trim_in_ms)/(c.playback_rate??1)+(c.hold_last_ms||0);
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
