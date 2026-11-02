import {normalize,opacity,audioGain,sourceSeconds,cameraBox,whiteFade} from './montage-timeline.mjs';

function waitMedia(media,event,ready){
  if(ready())return Promise.resolve();
  if(media.error)return Promise.reject(Error('Браузер не смог прочитать медиа (код '+media.error.code+').'));
  return new Promise((resolve,reject)=>{
    const finish=error=>{clearTimeout(timer);media.removeEventListener(event,ok);media.removeEventListener('error',bad);error?reject(error):resolve();};
    const ok=()=>finish(),bad=()=>finish(Error('Не удалось загрузить медиа.'));
    const timer=setTimeout(()=>finish(Error('Медиа не готово. Повторите воспроизведение.')),15000);
    media.addEventListener(event,ok,{once:true});media.addEventListener('error',bad,{once:true});
  });
}
export class MontagePlayer{
  constructor(stage,onTime,onError){
    this.stage=stage;this.onTime=onTime;this.onError=onError;this.time=0;this.playing=false;this.masterVolume=1;this.version=0;
    this.videos=Array.from({length:2},()=>{const v=document.createElement('video');v.preload='metadata';v.playsInline=true;stage.append(v);return v;});
    this.stills=Array.from({length:2},()=>{const canvas=document.createElement('canvas');canvas.width=1280;canvas.height=720;canvas.style.visibility='hidden';stage.append(canvas);return {canvas,asset:null,image:null};});
    this.audio=Array.from({length:2},()=>{const a=document.createElement('audio');a.preload='metadata';a.hidden=true;stage.append(a);return a;});
    this.whiteCover=document.createElement('div');this.whiteCover.style.cssText='position:absolute;inset:0;background:white;opacity:0;z-index:50;pointer-events:none';stage.append(this.whiteCover);
  }
  get duration(){return Math.max(this.timeline?.duration_ms||0,...(this.doc?.audio_tracks||[]).filter(a=>a.enabled!==false).map(a=>(a.offset_ms||0)+a.trim_out_ms-a.trim_in_ms));}
  setDocument(doc){this.pause();this.doc=structuredClone(doc);this.timeline=normalize(doc);this.seek(Math.min(this.time,this.duration));}
  pause(){this.playing=false;this.version++;cancelAnimationFrame(this.raf);for(const m of [...this.videos,...this.audio])m.pause();this.onTime?.(this.time,false);}
  async position(media,asset,seconds,force){
    if(media.dataset.asset!==asset){media.pause();media.dataset.asset=asset;media.src='/api/assets/'+asset;media.load();force=true;}
    await waitMedia(media,'loadedmetadata',()=>media.readyState>=1);
    seconds=Math.max(0,Math.min(seconds,Math.max(0,media.duration-.001)));
    if(force||Math.abs(media.currentTime-seconds)>.12){
      media.currentTime=seconds;
      await waitMedia(media,'seeked',()=>!media.seeking);
    }
    await waitMedia(media,'loadeddata',()=>media.readyState>=2);
  }
  prepare(time,force=false){
    const version=this.version;
    this.pending=(this.pending||Promise.resolve()).catch(()=>{}).then(()=>version===this.version?this.prepareNow(time,force):undefined);
    return this.pending;
  }
  async prepareNow(time,force=false){
    const clips=this.timeline.clips.filter(c=>c.start_ms<=time&&time<c.end_ms);
    this.whiteCover.style.opacity=String(Math.max(0,...clips.map(c=>whiteFade(c,time))));
    const assignments=[];
    const used=new Set();
    for(const c of clips){let v=this.videos.find(m=>!used.has(m)&&m.dataset.clip===c.edit_clip_id)||this.videos.find(m=>!used.has(m));used.add(v);v.dataset.clip=c.edit_clip_id;assignments.push([v,c]);}
    for(const v of this.videos)if(!used.has(v)){v.pause();v.style.visibility='hidden';v.volume=0;}
    await Promise.all(assignments.map(async([v,c],i)=>{
      const still=this.stills[this.videos.indexOf(v)];
      if(c.media_type==='image'){
        v.pause();v.style.visibility='hidden';v.volume=0;
        if(still.asset!==c.source_image_asset_id){const image=new Image();image.src='/api/assets/'+c.source_image_asset_id;await image.decode();still.image=image;still.asset=c.source_image_asset_id;}
        const image=still.image,canvas=still.canvas,ctx=canvas.getContext('2d');ctx.fillStyle='black';ctx.fillRect(0,0,canvas.width,canvas.height);
        if(c.camera_motion){const box=cameraBox(image.naturalWidth,image.naturalHeight,c.camera_motion,sourceSeconds(c,time)/Math.max(1/24,((c.camera_motion_total_ms||c.base_duration_ms)-(c.hold_last_ms||0))/1000-1/24));ctx.drawImage(image,box.x,box.y,box.w,box.h,0,0,canvas.width,canvas.height);}
        else{const scale=(c.framing==='fill'?Math.max:Math.min)(canvas.width/image.naturalWidth,canvas.height/image.naturalHeight),w=image.naturalWidth*scale,h=image.naturalHeight*scale;ctx.drawImage(image,(canvas.width-w)/2,(canvas.height-h)/2,w,h);}
        canvas.style.visibility='visible';canvas.style.zIndex=String(i+1);canvas.style.opacity=String(opacity(c,time));return;
      }
      still.canvas.style.visibility='hidden';
      v.playbackRate=c.playback_rate??1;
      await this.position(v,c.source_video_asset_id,sourceSeconds(c,time),force);
      v.dataset.holding=String(sourceSeconds(c,time)>=(c.trim_out_ms/1000-.002));
      if(v.dataset.holding==='true')v.pause();
      v.style.visibility='visible';v.style.zIndex=String(i+1);v.style.opacity=String(opacity(c,time));
      v.style.objectFit=c.framing==='fill'?'cover':'contain';
      let gain=opacity(c,time);
      if(c.out_kind==='dissolve'&&c.out_ms)gain*=Math.min(1,(c.end_ms-time)/c.out_ms);
      v.muted=!this.doc.source_audio||c.muted;v.volume=Math.min(1,(c.volume??1)*gain*this.masterVolume);
    }));
    for(let i=0;i<this.videos.length;i++)if(!used.has(this.videos[i]))this.stills[i].canvas.style.visibility='hidden';
    await Promise.all(this.audio.map(async(a,i)=>{
      const track=this.doc.audio_tracks[i];
      const length=track?track.trim_out_ms-track.trim_in_ms:0,local=track?time-track.offset_ms:0;
      if(!track||!track.enabled||local<0||local>=length){a.pause();a.dataset.active='';return;}
      a.dataset.active='1';await this.position(a,track.asset_id,(track.trim_in_ms+local)/1000,force);
      a.volume=Math.min(1,audioGain(track,local,length)*this.masterVolume);
    }));
    // Keep the next clip ready in the unused video element.
    if(clips.length===1){
      const index=this.timeline.clips.findIndex(c=>c.edit_clip_id===clips[0].edit_clip_id),next=this.timeline.clips[index+1],idle=this.videos.find(v=>!used.has(v));
      if(next&&idle&&next.media_type!=='image'){
        const preloadKey=next.source_video_asset_id+':'+next.trim_in_ms;
        if(idle.dataset.preloadKey!==preloadKey){
          idle.dataset.preloadKey=preloadKey;idle.dataset.clip=next.edit_clip_id;idle.muted=true;
          // A future clip must never stall the current frame or narration.
          this.position(idle,next.source_video_asset_id,next.trim_in_ms/1000,false)
            .then(()=>{if(idle.style.visibility!=='visible')idle.pause();})
            .catch(()=>{if(idle.dataset.preloadKey===preloadKey)delete idle.dataset.preloadKey;});
        }
      }
    }
  }
  seek(time){
    this.pause();this.time=Math.max(0,Math.min(time,Math.max(0,this.duration-.001)));const version=this.version;
    this.prepare(this.time,true).then(()=>{if(version===this.version)this.onTime?.(this.time,false);}).catch(e=>this.onError?.(e));
  }
  async play(){
    this.pause();if(!this.duration)return;
    if(this.time>=this.duration-1)this.time=0;
    this.playing=true;const version=this.version;
    let last=performance.now();
    const tick=async()=>{
      if(!this.playing||version!==this.version)return;
      const now=performance.now();const delta=Math.min(100,now-last);last=now;
      this.time=Math.min(this.duration,this.time+delta);
      if(this.time>=this.duration){this.pause();return;}
      try{
        await this.prepare(this.time);
        if(!this.playing||version!==this.version){for(const m of [...this.videos,...this.audio])m.pause();return;}
        await Promise.all([...this.videos.filter(v=>v.style.visibility==='visible'&&v.dataset.holding!=='true'),...this.audio.filter(a=>a.dataset.active)].map(m=>m.paused?m.play():Promise.resolve()));
        // Freeze the shared clock while media was buffering or seeking.
        if(performance.now()-now>80)last=performance.now();
        this.onTime?.(this.time,true);this.raf=requestAnimationFrame(tick);
      }catch(e){this.pause();this.onError?.(e);}
    };
    this.raf=requestAnimationFrame(tick);
  }
  dispose(){this.pause();this.onTime=null;this.onError=null;for(const m of [...this.videos,...this.audio]){m.removeAttribute('src');m.load();}this.stage.replaceChildren();}
}
