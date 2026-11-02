import {effectiveClip} from './montage-layers.mjs';
// Same millisecond contract as editing.normalize; shared fixtures test parity.
export function normalize(document){
  const clips=document.clips.filter(c=>!c.excluded).map(effectiveClip).map(c=>{const duration_ms=(c.trim_out_ms-c.trim_in_ms)/(c.playback_rate??1)+(c.hold_last_ms||0);return {...c,duration_ms,base_duration_ms:duration_ms,in_ms:0,out_ms:0,out_kind:'cut'};});
  const joins=new Map((document.transitions||[]).map(t=>[`${t.left}|${t.right}`,t]));
  for(let i=0;i<clips.length-1;i++){
    const left=clips[i],right=clips[i+1],t=joins.get(`${left.edit_clip_id}|${right.edit_clip_id}`)||{};
    const kind=t.kind||'cut',duration=kind==='cut'?0:Math.min(t.duration_ms||0,left.duration_ms/2,right.duration_ms/2);
    left.out_ms=right.in_ms=duration;left.out_kind=kind;right.in_kind=kind;
  }
  let cursor=0;
  for(const c of clips){c.start_ms=cursor;c.end_ms=cursor+c.duration_ms;if(document.timing_mode==='voice_fit'){cursor=c.end_ms;c.base_end_ms=cursor;if(c.out_kind==='dissolve'){c.end_ms+=c.out_ms;c.duration_ms+=c.out_ms;}}else cursor=c.end_ms-(c.out_kind==='dissolve'?c.out_ms:0);}
  return {clips,duration_ms:cursor,audio_tracks:structuredClone(document.audio_tracks||[])};
}
export function sourceSeconds(c,time){const seconds=(c.trim_in_ms+(time-c.start_ms)*(c.playback_rate??1))/1000;return c.media_type==='image'?seconds:Math.min(seconds,Math.max(c.trim_in_ms/1000,c.trim_out_ms/1000-.001));}
export function cameraBox(width,height,p,t){t=Math.max(0,Math.min(1,t));const scale=(1+p.overscan)*(p.scale_start+t*(p.scale_end-p.scale_start)),w=width/scale,h=height/scale,px=p.pan_x_start+t*(p.pan_x_end-p.pan_x_start),py=p.pan_y_start+t*(p.pan_y_end-p.pan_y_start);return {x:Math.max(0,Math.min(width-w,width/2+px*width/2-w/2)),y:Math.max(0,Math.min(height-h,height/2+py*height/2-h/2)),w,h};}
export function whiteFade(c,time){
 const local=time-c.start_ms;
 if(c.in_kind==='white'&&c.in_ms&&local<c.in_ms/2)return Math.max(0,Math.min(1,1-local/(c.in_ms/2)));
 if(c.out_kind==='white'&&c.out_ms&&c.duration_ms-local<c.out_ms/2)return Math.max(0,Math.min(1,1-(c.duration_ms-local)/(c.out_ms/2)));
 return 0;
}
export function freezeRegion(c){
 if(!(c.hold_last_ms>0))return null;
 const end=c.base_end_ms??c.start_ms+c.base_duration_ms;
 return {start_ms:end-c.hold_last_ms,end_ms:end,duration_ms:c.hold_last_ms,
  left_percent:100*(end-c.hold_last_ms-c.start_ms)/(c.end_ms-c.start_ms),
  width_percent:100*c.hold_last_ms/(c.end_ms-c.start_ms)};
}
export function opacity(c,time){
  const local=time-c.start_ms;
  if(c.in_kind==='dissolve'&&c.in_ms&&local<c.in_ms)return Math.max(0,local/c.in_ms);
  if(c.in_kind==='black'&&c.in_ms&&local<c.in_ms/2)return Math.max(0,local/(c.in_ms/2));
  if(c.out_kind==='black'&&c.out_ms&&c.duration_ms-local<c.out_ms/2)return Math.max(0,(c.duration_ms-local)/(c.out_ms/2));
  return 1;
}
export function audioGain(track,local,length){
  return (track.volume??1)*Math.min(1,track.fade_in_ms?Math.max(0,local/track.fade_in_ms):1)*Math.min(1,track.fade_out_ms?Math.max(0,(length-local)/track.fade_out_ms):1);
}
