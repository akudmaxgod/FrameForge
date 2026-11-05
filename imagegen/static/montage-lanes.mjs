import {normalize,freezeRegion} from './montage-timeline.mjs';
import {applyAutomaticTransitions} from './montage-transitions.mjs';

const FRAME=1000/24;
const clamp=(n,lo,hi)=>Math.max(lo,Math.min(hi,n));
const names={video:'Видео',voice:'Озвучка',music:'Музыка'};
const uploadNames={voice:'озвучку',music:'музыку'};
function element(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function control(text,action){const n=element('button',text);n.type='button';n.onclick=action;return n;}
export function timelineTime(ms){
  const seconds=Math.max(0,ms)/1000,m=Math.floor(seconds/60),s=(seconds%60).toFixed(2).padStart(5,'0');
  return `${m}:${s}`;
}
export function timelineExtent(doc){
  const timeline=normalize(doc);
  return Math.max(timeline.duration_ms,...(doc.audio_tracks||[]).filter(a=>a.enabled!==false).map(a=>(a.offset_ms||0)+a.trim_out_ms-a.trim_in_ms));
}

// Edits only the montage document. Source media and scene generation are untouched.
export function trimTimelineItem(doc,{kind,id,edge},delta){
  if(kind==='overlay'){
    const base=doc.clips.find(c=>c.edit_clip_id===id);if(!base?.footage_overlay)return;
    const draft=structuredClone(base),layer=draft.footage_overlay,rate=layer.playback_rate??1;
    const oldSpan=(base.trim_out_ms-base.trim_in_ms)/(base.playback_rate??1)+(base.hold_last_ms||0);
    delta=edge==='start'?clamp(delta,-layer.trim_in_ms/rate,oldSpan-FRAME):clamp(delta,-oldSpan+FRAME,(layer.source_duration_ms-layer.trim_out_ms)/rate);
    trimTimelineItem({clips:[draft]},{kind:'video',id,edge},delta);
    const span=(draft.trim_out_ms-draft.trim_in_ms)/(draft.playback_rate??1)+(draft.hold_last_ms||0);
    if(edge==='start')layer.trim_in_ms+=(oldSpan-span)*rate;
    layer.trim_out_ms=layer.trim_in_ms+span*rate;layer.hold_last_ms=0;layer.manual_timing=layer.timing_locked=true;
    Object.assign(base,draft);return;
  }
  const item=kind==='video'?doc.clips.find(c=>c.edit_clip_id===id):doc.audio_tracks.find(a=>a.role===id);
  if(!item)return;
  if(kind==='video'){item.manual_timing=true;item.timing_locked=true;}
  if(kind==='video')delta*=item.playback_rate??1;
  const minimum=kind==='video'?FRAME:10;
  if(edge==='start'){
    const lower=kind==='video'?-item.trim_in_ms:-Math.min(item.trim_in_ms,item.offset_ms||0);
    delta=clamp(delta,lower,item.trim_out_ms-item.trim_in_ms-minimum);
    item.trim_in_ms+=delta;
    if(kind!=='video')item.offset_ms=(item.offset_ms||0)+delta;
  }else{
    if(kind==='video'&&item.hold_last_ms){item.hold_last_ms=Math.max(0,item.hold_last_ms+delta/(item.playback_rate??1));}
    else item.trim_out_ms=clamp(item.trim_out_ms+delta,item.trim_in_ms+minimum,item.source_duration_ms);
  }
}
export function reorderTimelineClip(doc,id,beforeId){
  if(id===beforeId)return;
  const index=doc.clips.findIndex(c=>c.edit_clip_id===id);if(index<0)return;
  const [clip]=doc.clips.splice(index,1);
  const before=beforeId?doc.clips.findIndex(c=>c.edit_clip_id===beforeId):-1;
  if(before>=0)doc.clips.splice(before,0,clip);else doc.clips.push(clip);
}

export class MontageLanes{
  constructor(host,{projectId,getDocument,sceneLabel,onEdit,onSelect,onSeek,onUpload,onPause,onError,onDelete}){
    Object.assign(this,{getDocument,sceneLabel,onEdit,onSelect,onSeek,onUpload,onPause,onError,onDelete});
    this.scale=12;this.current=0;this.selection=null;this.snap=true;this.disposed=false;
    this.storageKey='frameforge-timeline-scale:'+projectId;
    try{const saved=Number(localStorage.getItem(this.storageKey));if(saved>=.25&&saved<=240)this.scale=saved;}catch{}
    this.root=element('section',undefined,'montage-timeline');this.root.setAttribute('aria-label','Таймлайн');this.root.dataset.testid='montage-timeline';host.append(this.root);
    const tools=element('div',undefined,'montage-timeline-tools'),title=element('strong','Таймлайн'),zoom=element('input');zoom.type='range';zoom.min=0;zoom.max=100;zoom.setAttribute('aria-label','Масштаб таймлайна');
    this.zoom=zoom;zoom.oninput=()=>this.setScale(.25*Math.pow(960,Number(zoom.value)/100));
    tools.append(title,control('−',()=>this.setScale(this.scale/1.5)),zoom,control('+',()=>this.setScale(this.scale*1.5)),control('Весь ролик',()=>this.setScale(Math.max(.25,(this.viewport.clientWidth-30)/Math.max(1,timelineExtent(this.getDocument())/1000)))));
    const snap=element('label',undefined,'timeline-snap'),check=element('input');check.type='checkbox';check.checked=true;check.onchange=()=>this.snap=check.checked;snap.append(check,document.createTextNode('Привязка'));tools.append(snap);
    this.summary=element('span',undefined,'timeline-summary');tools.append(this.summary);this.root.append(tools);
    const auto=element('div',undefined,'timeline-auto-transitions'),preset=this.getDocument().auto_transitions||{};
    const types=element('fieldset',undefined,'transition-types');types.append(element('legend','Виды переходов'));
    const chosen=preset.kinds||[preset.kind||'black'],checks=[];
    for(const [value,label] of [['dissolve','Плавное смешивание'],['black','Затухание через чёрный'],['white','Затухание через белый']]){
      const row=element('label',undefined,'transition-type'),check=element('input');check.type='checkbox';check.value=value;check.checked=chosen.includes(value);check.setAttribute('aria-label',label);checks.push(check);row.append(check,element('span',label));types.append(row);
    }
    const explanation=element('p','Отмеченные виды чередуются по порядку.','transition-types-hint');
    const every=element('input');every.type='number';every.min=1;every.max=20;every.step=1;every.value=preset.every||3;every.setAttribute('aria-label','Каждый какой стык');
    const duration=element('input');duration.type='number';duration.min=.05;duration.max=5;duration.step=.05;duration.value=(preset.duration_ms||500)/1000;duration.setAttribute('aria-label','Длительность автоматического перехода, с');
    auto.append(element('strong','Автопереходы'),types,explanation,element('span','Каждый'),every,element('span','стык ·'),duration,element('span','сек.'),control('Добавить переходы',()=>{
      try{this.onPause();this.onEdit(doc=>applyAutomaticTransitions(doc,{kinds:checks.filter(c=>c.checked).map(c=>c.value),every:Number(every.value),duration_ms:Number(duration.value)*1000}));}catch(e){this.onError(e);}
    }));this.root.append(auto);
    const grid=element('div',undefined,'montage-timeline-grid'),labels=element('div',undefined,'timeline-labels');labels.append(element('div','Время','timeline-label-ruler'));
    for(const kind of ['overlay','video','voice','music']){
      const label=element('div',undefined,'timeline-label timeline-label-'+kind);label.append(element('strong',kind==='overlay'?'Футажи':kind==='video'?'Исходные кадры':names[kind]));
      if(kind==='overlay'){
        this.footageMute=control('🔊',()=>{this.onPause();this.onEdit(doc=>{doc.footage_muted=!doc.footage_muted;});});
        label.append(this.footageMute);
      }
      if(kind!=='video'&&kind!=='overlay')label.append(control('+ Файл',()=>onUpload(kind)));
      labels.append(label);
    }
    this.viewport=element('div',undefined,'timeline-scroll');this.viewport.tabIndex=0;this.viewport.setAttribute('aria-label','Прокрутка таймлайна');
    this.canvas=element('div',undefined,'timeline-canvas');this.ruler=element('div',undefined,'timeline-ruler');this.ruler.setAttribute('aria-label','Шкала времени');this.canvas.append(this.ruler);
    this.lanes={};for(const kind of ['overlay','video','voice','music']){const lane=element('div',undefined,'timeline-lane timeline-lane-'+kind);lane.dataset.lane=kind;lane.setAttribute('aria-label','Дорожка '+({overlay:'футажей',video:'исходных кадров',voice:'озвучки',music:'музыки'}[kind]));this.lanes[kind]=lane;this.canvas.append(lane);}
    this.playhead=element('div',undefined,'timeline-playhead');this.playhead.setAttribute('role','slider');this.playhead.setAttribute('aria-label','Позиция курсора');this.playhead.setAttribute('aria-valuemin','0');this.playhead.tabIndex=0;
    this.playhead.append(element('span',undefined,'timeline-playhead-head'));this.canvas.append(this.playhead);
    this.playhead.onkeydown=e=>{const keys={ArrowLeft:-FRAME,ArrowRight:FRAME};if(e.key in keys){e.preventDefault();onSeek(clamp(this.current+keys[e.key]*(e.shiftKey?24:1),0,this.extent));}else if(e.key==='Home'||e.key==='End'){e.preventDefault();onSeek(e.key==='Home'?0:this.extent);}};
    this.playhead.onpointerdown=e=>this.scrub(e);this.ruler.onpointerdown=e=>this.scrub(e);
    for(const lane of Object.values(this.lanes))lane.onpointerdown=e=>{if(e.target===lane)this.scrub(e);};
    this.viewport.append(this.canvas);grid.append(labels,this.viewport);this.root.append(grid);
    this.hint=element('p','Перетащите видео для смены порядка. Края обрезают клип. Озвучку и музыку можно сдвигать по времени.','timeline-hint');this.warning=element('p',undefined,'montage-warning timeline-duration-warning');this.warning.setAttribute('role','status');this.root.append(this.hint,this.warning);
    this.viewport.onscroll=()=>{if(this.drag)return;cancelAnimationFrame(this.scrollFrame);this.scrollFrame=requestAnimationFrame(()=>this.renderVisible());};
    this.resize=new ResizeObserver(()=>{if(!this.disposed&&!this.drag)this.render();});this.resize.observe(this.viewport);
    const onWheel=e=>{
      if(e.ctrlKey||e.metaKey){
        e.preventDefault();
        const delta=Math.max(-100,Math.min(100,e.deltaY));
        const factor=Math.pow(1.002,-delta);
        this.zoomAt(this.scale*factor,e.clientX);
      }
    };
    this.root.addEventListener('wheel',onWheel,{passive:false});
    this.cleanupWheel=()=>this.root.removeEventListener('wheel',onWheel);
    this.render();
  }
  zoomAt(scale,clientX){
    const old=this.scale,next=clamp(scale,.25,240);
    if(Math.abs(next-old)<.0001)return;
    const box=this.viewport.getBoundingClientRect();
    const mouseX=clientX!==undefined?clamp(clientX-box.left,0,this.viewport.clientWidth):this.viewport.clientWidth/2;
    const timeSec=(this.viewport.scrollLeft+mouseX)/old;
    this.scale=next;try{localStorage.setItem(this.storageKey,this.scale);}catch{}
    this.render();
    this.viewport.scrollLeft=Math.max(0,timeSec*this.scale-mouseX);
    this.renderVisible();
  }
  setScale(scale){
    this.zoomAt(scale);
  }
  timeAt(clientX){const box=this.viewport.getBoundingClientRect();return clamp((clientX-box.left+this.viewport.scrollLeft)/this.scale*1000,0,this.extent);}
  scrub(e){
    if(e.button!==0)return;e.preventDefault();this.onPause();const target=e.currentTarget;target.setPointerCapture(e.pointerId);
    const move=event=>this.onSeek(this.timeAt(event.clientX));
    const stop=event=>{if(event.type!=='pointercancel')move(event);target.removeEventListener('pointermove',move);target.removeEventListener('pointerup',stop);target.removeEventListener('pointercancel',stop);this.cleanupGesture=null;};
    target.addEventListener('pointermove',move);target.addEventListener('pointerup',stop);target.addEventListener('pointercancel',stop);this.cleanupGesture=()=>stop({type:'pointercancel'});move(e);
  }
  setTime(time,follow=false){
    this.current=time;this.playhead.style.left=`${time/1000*this.scale}px`;this.playhead.setAttribute('aria-valuenow',Math.round(time));this.playhead.setAttribute('aria-valuetext',timelineTime(time));
    if(follow&&!this.drag){const x=time/1000*this.scale;if(x<this.viewport.scrollLeft||x>this.viewport.scrollLeft+this.viewport.clientWidth-25)this.viewport.scrollLeft=Math.max(0,x-60);}
  }
  select(kind,id){this.selection={kind,id};this.onSelect(kind,id);this.highlight();}
  highlight(){for(const n of this.canvas.querySelectorAll('[data-item]'))n.classList.toggle('selected',n.dataset.kind===this.selection?.kind&&n.dataset.item===this.selection?.id);}
  render(){
    if(this.footageMute){const muted=!!this.getDocument().footage_muted;this.footageMute.textContent=muted?'🔇':'🔊';this.footageMute.title=muted?'Включить звук футажей с учётом настроек клипов':'Выключить звук всех футажей';this.footageMute.setAttribute('aria-label',this.footageMute.title);this.footageMute.setAttribute('aria-pressed',String(muted));}
    if(this.disposed)return;
    this.doc=this.getDocument();this.timeline=normalize(this.doc);this.extent=timelineExtent(this.doc);
    this.canvas.style.width=`${Math.max(this.viewport.clientWidth,this.extent/1000*this.scale+40)}px`;
    this.zoom.value=Math.log(this.scale/.25)/Math.log(960)*100;this.playhead.setAttribute('aria-valuemax',Math.round(this.extent));
    this.summary.textContent=`Видео ${timelineTime(this.timeline.duration_ms)}`;
    const voice=this.doc.audio_tracks.find(a=>a.role==='voice'&&a.enabled!==false),end=voice?(voice.offset_ms||0)+voice.trim_out_ms-voice.trim_in_ms:0;
    const gap=end-this.timeline.duration_ms;
    this.warning.textContent=gap>FRAME?`Озвучка длиннее видеоряда на ${timelineTime(gap)}. В этом участке нет изображения; экспорт MP4 заканчивается вместе с видеорядом.`:'';this.warning.hidden=!this.warning.textContent;
    this.renderVisible();this.setTime(this.current);
  }
  renderVisible(){
    if(this.disposed||!this.timeline)return;
    const lo=Math.max(0,(this.viewport.scrollLeft-150)/this.scale*1000),hi=(this.viewport.scrollLeft+this.viewport.clientWidth+150)/this.scale*1000;
    this.ruler.replaceChildren();const interval=[.25,.5,1,2,5,10,15,30,60,120,300,600].find(s=>s*this.scale>=75)||1200;
    for(let ms=Math.floor(lo/(interval*1000))*interval*1000;ms<=Math.min(hi,this.extent)+1;ms+=interval*1000){const tick=element('span',timelineTime(ms).replace(/\.00$/,''),'timeline-tick');tick.style.left=`${ms/1000*this.scale}px`;this.ruler.append(tick);}
    for(const lane of Object.values(this.lanes))lane.replaceChildren();
    const clips=this.timeline.clips;
    if(!clips.length)this.lanes.video.append(element('p','Добавьте готовые сцены кнопкой над плеером.','timeline-empty'));
    for(const c of clips){
      if(c.end_ms<lo||c.start_ms>hi)continue;
      const raw=this.doc.clips.find(x=>x.edit_clip_id===c.edit_clip_id)||c;
      const label=raw.source_kind==='footage'?(raw.title||'Футаж'):`Сцена ${this.sceneLabel(raw.scene_id)}`,block=this.block('video',c.edit_clip_id,c.start_ms,c.end_ms,label,raw);
      if(raw.footage_overlay){
        const overlay=this.block('overlay',c.edit_clip_id,c.start_ms,c.end_ms,raw.footage_overlay.title||'Футаж',raw.footage_overlay);
        overlay.classList.toggle('disabled',raw.footage_overlay.enabled===false);this.lanes.overlay.append(overlay);
      }
      if(raw.source_image_asset_id){const img=element('img');img.loading='lazy';img.draggable=false;img.src='/api/assets/'+raw.source_image_asset_id;img.alt='';block.prepend(img);}
      const frozen=freezeRegion(c);
      if(frozen){const tail=element('div',undefined,'timeline-freeze');tail.style.left=`${frozen.left_percent}%`;tail.style.width=`${frozen.width_percent}%`;tail.dataset.testid='timeline-freeze';tail.setAttribute('role','img');tail.setAttribute('aria-label',`СТОП-КАДР · ${(frozen.duration_ms/1000).toFixed(2)} с`);tail.append(element('span','СТОП-КАДР'));block.append(tail);block.title+=` · Стоп-кадр ${(frozen.duration_ms/1000).toFixed(2)} с`;}
      if(c.in_kind==='dissolve'&&c.in_ms)block.classList.add('timeline-overlap');this.lanes.video.append(block);
      const next=clips[clips.indexOf(c)+1];
      if(next){const marker=control('◇',()=>this.quickTransition(c.edit_clip_id,next.edit_clip_id,marker));marker.className='timeline-transition';marker.style.left=`${(c.end_ms-(c.out_kind==='dissolve'?c.out_ms/2:0))/1000*this.scale}px`;marker.title='Быстрый переход · '+(c.out_kind||'cut');marker.setAttribute('aria-label',`Переход после сцены ${label}`);this.lanes.video.append(marker);}
    }
    for(const role of ['voice','music']){
      const a=this.doc.audio_tracks.find(t=>t.role===role);
      if(!a){const upload=control('Загрузить '+uploadNames[role],()=>this.onUpload(role));upload.className='timeline-empty';this.lanes[role].append(upload);continue;}
      const start=a.offset_ms||0,end=start+a.trim_out_ms-a.trim_in_ms;
      if(end>=lo&&start<=hi){const block=this.block(role,role,start,end,names[role],a);if(a.enabled===false)block.classList.add('disabled');this.lanes[role].append(block);}
      if(a.enabled!==false&&end>this.timeline.duration_ms){const tail=element('div',undefined,'timeline-audio-overhang');tail.style.left=`${Math.max(start,this.timeline.duration_ms)/1000*this.scale}px`;tail.style.width=`${(end-Math.max(start,this.timeline.duration_ms))/1000*this.scale}px`;tail.title='Участок звука без видеоряда';this.lanes[role].append(tail);}
    }
    this.highlight();
  }
  block(kind,id,start,end,title,item){
    const block=element('div',undefined,'timeline-block timeline-block-'+kind);block.dataset.kind=kind;block.dataset.item=id;block.dataset.start=String(start);block.dataset.end=String(end);block.style.left=`${start/1000*this.scale}px`;block.style.width=`${Math.max(3,(end-start)/1000*this.scale)}px`;block.title=`${title} · ${timelineTime(start)} — ${timelineTime(end)}`;
    block.tabIndex=0;block.setAttribute('role','button');block.setAttribute('aria-label',`${title} на таймлайне`);
    const caption=element('span',undefined,'timeline-block-caption');caption.append(element('strong',title),element('small',timelineTime(end-start)));block.append(caption);
    block.onpointerdown=e=>{if(kind==='overlay'){e.preventDefault();this.onPause();this.select(kind,id);this.onSeek(start);return;}this.beginGesture(e,{kind,id,edge:null,start,end});};
    block.onkeydown=e=>{
      if(e.target!==block)return;
      if(e.key==='Enter'||e.key===' '){e.preventDefault();this.select(kind,id);this.onSeek(start);}
      else if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
        e.preventDefault();const step=e.key==='ArrowLeft'?-1:1;
        this.onEdit(doc=>{if(kind==='video'||kind==='overlay'){const clips=doc.clips.filter(c=>!c.excluded),i=clips.findIndex(c=>c.edit_clip_id===id),next=i+step;if(next>=0&&next<clips.length)reorderTimelineClip(doc,id,step<0?clips[next].edit_clip_id:clips[next+1]?.edit_clip_id);}else{const a=doc.audio_tracks.find(t=>t.role===id);a.offset_ms=Math.max(0,(a.offset_ms||0)+step*(e.shiftKey?1000:FRAME));}});
      }
      else if(e.key==='Delete'||e.key==='Backspace'){
        if(this.onDelete){e.preventDefault();e.stopPropagation();this.select(kind,id);this.onDelete(kind,id);}
      }
    };
    for(const edge of ['start','end']){
      const handle=element('button',undefined,'timeline-trim timeline-trim-'+edge);handle.type='button';handle.setAttribute('aria-label',`Обрезать ${edge==='start'?'начало':'конец'}: ${title}`);
      handle.onpointerdown=e=>{e.stopPropagation();this.beginGesture(e,{kind,id,edge,start,end});};
      handle.onkeydown=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();e.stopPropagation();this.select(kind,id);this.onEdit(doc=>trimTimelineItem(doc,{kind,id,edge},(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?1000:FRAME)));}};block.append(handle);
    }
    return block;
  }
  snapped(value,exclude){
    if(!this.snap)return value;
    const anchors=[0,this.current,...this.timeline.clips.filter(c=>c.edit_clip_id!==exclude).flatMap(c=>[c.start_ms,c.end_ms]),...this.doc.audio_tracks.filter(a=>a.role!==exclude&&a.enabled!==false).flatMap(a=>[a.offset_ms||0,(a.offset_ms||0)+a.trim_out_ms-a.trim_in_ms])];
    const nearest=anchors.reduce((best,x)=>Math.abs(x-value)<Math.abs(best-value)?x:best,Infinity);
    return Math.abs(nearest-value)<=8/this.scale*1000?nearest:value;
  }
  beginGesture(e,gesture){
    if(e.button!==0)return;e.preventDefault();this.onPause();this.select(gesture.kind,gesture.id);
    const target=e.currentTarget,block=target.closest('.timeline-block');target.setPointerCapture(e.pointerId);
    const original=structuredClone(this.getDocument()),origin=e.clientX,startScroll=this.viewport.scrollLeft;
    this.drag={...gesture};let delta=0,moved=false,beforeId;
    const insertion=element('div',undefined,'timeline-insertion');this.canvas.append(insertion);
    const move=event=>{
      const box=this.viewport.getBoundingClientRect();if(event.clientX>box.right-25)this.viewport.scrollLeft+=20;else if(event.clientX<box.left+25)this.viewport.scrollLeft-=20;
      const raw=(event.clientX-origin+this.viewport.scrollLeft-startScroll)/this.scale*1000;moved=moved||Math.abs(event.clientX-origin)>3;
      if(!moved)return;
      if(gesture.edge){
        delta=this.snapped((gesture.edge==='start'?gesture.start:gesture.end)+raw,gesture.id)-(gesture.edge==='start'?gesture.start:gesture.end);
        const preview=structuredClone(original);trimTimelineItem(preview,gesture,delta);
        const visual=['video','overlay'].includes(gesture.kind),base=preview.clips.find(c=>c.edit_clip_id===gesture.id);
        const edited=visual?(gesture.kind==='overlay'?base.footage_overlay:base):preview.audio_tracks.find(a=>a.role===gesture.id);
        const start=visual?gesture.start:edited.offset_ms||0,width=(edited.trim_out_ms-edited.trim_in_ms)/(visual?(edited.playback_rate??1):1)+(edited.hold_last_ms||0);
        block.style.left=`${start/1000*this.scale}px`;block.style.width=`${Math.max(3,width/1000*this.scale)}px`;
      }else if(gesture.kind==='video'){
        delta=raw;block.style.transform=`translateX(${raw/1000*this.scale}px)`;block.classList.add('dragging');
        const desired=gesture.start+raw+(gesture.end-gesture.start)/2,other=this.timeline.clips.filter(c=>c.edit_clip_id!==gesture.id);
        const before=other.find(c=>desired<(c.start_ms+c.end_ms)/2);beforeId=before?.edit_clip_id;insertion.style.left=`${(before?before.start_ms:this.timeline.duration_ms)/1000*this.scale}px`;insertion.hidden=false;
      }else{
        const candidates=[this.snapped(gesture.start+raw,gesture.id)-gesture.start,this.snapped(gesture.end+raw,gesture.id)-gesture.end].filter(d=>Math.abs(d-raw)>.000001);
        delta=candidates.length?candidates.reduce((best,d)=>Math.abs(d-raw)<Math.abs(best-raw)?d:best):raw;delta=Math.max(-gesture.start,delta);block.style.left=`${(gesture.start+delta)/1000*this.scale}px`;
      }
      block.title=`${gesture.edge?'Обрезка':'Перемещение'} · ${timelineTime(gesture.start+delta)}`;
    };
    const stop=event=>{
      target.removeEventListener('pointermove',move);target.removeEventListener('pointerup',stop);target.removeEventListener('pointercancel',stop);document.removeEventListener('keydown',cancel);if(target.hasPointerCapture(e.pointerId))target.releasePointerCapture(e.pointerId);this.drag=null;this.cleanupGesture=null;insertion.remove();
      if(event.type!=='pointercancel'&&moved){
        try{this.onEdit(doc=>{if(gesture.edge)trimTimelineItem(doc,gesture,delta);else if(gesture.kind==='video')reorderTimelineClip(doc,gesture.id,beforeId);else{const a=doc.audio_tracks.find(t=>t.role===gesture.id);a.offset_ms=Math.max(0,(a.offset_ms||0)+delta);}});}catch(error){this.onError(error);}
      }else{this.renderVisible();if(event.type!=='pointercancel')this.onSeek(gesture.start);}
      this.render();
    };
    const cancel=event=>{if(event.key==='Escape'){event.preventDefault();stop({type:'pointercancel'});}};
    insertion.hidden=true;target.addEventListener('pointermove',move);target.addEventListener('pointerup',stop);target.addEventListener('pointercancel',stop);document.addEventListener('keydown',cancel);this.cleanupGesture=()=>stop({type:'pointercancel'});
  }
  quickTransition(left,right,anchor){
    this.transitionMenu?.remove();this.onPause();
    const menu=element('div',undefined,'timeline-transition-menu');this.transitionMenu=menu;
    menu.setAttribute('role','dialog');menu.setAttribute('aria-label','Быстрый переход');
    const box=anchor.getBoundingClientRect();menu.style.cssText=`position:fixed;z-index:10000;left:${Math.min(box.left,window.innerWidth-230)}px;top:${Math.max(8,box.top-145)}px;background:#191827;border:1px solid #8764d4;border-radius:8px;padding:10px;display:grid;gap:5px;`;
    for(const [kind,title] of [['cut','Без перехода'],['dissolve','Растворение'],['black','Через чёрный'],['white','Через белый']])menu.append(control(title,()=>{this.onEdit(doc=>{doc.transitions=doc.transitions.filter(t=>!(t.left===left&&t.right===right));if(kind!=='cut')doc.transitions.push({left,right,kind,duration_ms:doc.auto_transitions?.duration_ms||500});});menu.remove();}));
    menu.append(control('Закрыть',()=>menu.remove()));menu.onkeydown=e=>{if(e.key==='Escape'){menu.remove();anchor.focus();}};document.body.append(menu);menu.querySelector('button').focus();
  }
  dispose(){this.disposed=true;this.transitionMenu?.remove();this.cleanupGesture?.();this.cleanupWheel?.();this.resize.disconnect();cancelAnimationFrame(this.scrollFrame);this.root.remove();}
}
