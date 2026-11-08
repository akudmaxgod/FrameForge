import {foregroundPairDialog} from './montage-foreground.mjs';
import {overlayFootage,segmentDuration,setClipSpeed} from './montage-layers.mjs';
import {normalize} from './montage-timeline.mjs';
import {MontagePlayer} from './montage-player.mjs';
import {MontageLanes,timelineExtent,timelineTime} from './montage-lanes.mjs';
import {mountVoiceFit} from './montage-fit-controls.mjs';
import {mountMediaLibraryButton} from './media-library-panel.mjs';
import {mountMaterialSearchButton} from './material-search.mjs';
const sessions=new Map();
const loadingSessions=new Map();
const copy=x=>structuredClone(x),key=id=>'frameforge-edit:'+id;
const time=x=>(x/1000).toFixed(2)+' с';
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function button(text,fn){const b=node('button',text);b.type='button';b.onclick=()=>Promise.resolve().then(fn).catch(e=>b.closest('.montage')?.dispatchEvent(new CustomEvent('montage-error',{detail:e})));return b;}
function field(parent,label,value,change,{min=0,max=86400,step=.1,type='number'}={}){
  const wrap=node('label',label,'montage-field'),input=node('input');input.type=type;
  if(type==='checkbox')input.checked=Boolean(value);else{input.value=value;input.min=min;input.max=max;input.step=step;}
  input.onchange=()=>{if(!input.checkValidity()){input.reportValidity();return;}try{change(type==='checkbox'?input.checked:Number(input.value));}catch(e){parent.closest('.montage')?.dispatchEvent(new CustomEvent('montage-error',{detail:e}));}};wrap.append(input);parent.append(wrap);return input;
}
function persist(s){try{localStorage.setItem(key(s.id),JSON.stringify({revision:s.revision,document:s.doc}));}catch{ s.error='Не удалось сохранить резервный черновик в браузере.';}}
async function flush(s){
  clearTimeout(s.timer);
  if(s.promise){await s.promise;return flush(s);}
  if(s.conflict)throw Error(s.error||'Перечитайте монтаж после конфликта.');
  if(!s.dirty)return;
  const version=s.version,document=copy(s.doc);s.saving=true;s.notify?.();
  s.promise=s.api(`/projects/${s.id}/edit`,{expected_revision:s.revision,document});
  try{
    const result=await s.promise;s.revision=result.revision;s.sources=result.sources;
    if(version===s.version){s.dirty=false;s.error='';try{localStorage.removeItem(key(s.id));}catch{}}
    else persist(s);
  }catch(e){s.error=e.message;s.conflict=e.status===409;persist(s);throw e;}
  finally{s.promise=null;s.saving=false;s.notify?.();}
  if(s.dirty)return flush(s);
}

export async function mountMontage(host,{project,api,downloadZip,notice}){
  const id=project.id;
  let s=sessions.get(id);
  if(!s){
    if(!loadingSessions.has(id))loadingSessions.set(id,(async()=>{
      const result=await api(`/projects/${id}/edit`);const state={id,api,doc:result.document,revision:result.revision,sources:result.sources,lastExport:result.last_export_id,history:[],future:[],version:0,dirty:false,error:'',conflict:false};
      try{const draft=JSON.parse(localStorage.getItem(key(id)));if(draft){state.doc=draft.document;state.dirty=true;state.conflict=draft.revision!==state.revision;state.error=state.conflict?'Серверная версия изменилась. Локальный черновик сохранён. Перечитайте монтаж.':'';}}catch{}
      sessions.set(id,state);return state;
    })());
    try{s=await loadingSessions.get(id);}finally{loadingSessions.delete(id);}
  }else if(!s.dirty&&!s.promise){
    const result=await api(`/projects/${id}/edit`);
    if(!s.dirty&&!s.promise&&result.revision>s.revision){
      s.doc=result.document;s.revision=result.revision;s.sources=result.sources;s.lastExport=result.last_export_id;
      s.history=[];s.future=[];
    }
  }
  const root=node('section',undefined,'workspace montage');host.append(root);
  root.addEventListener('montage-error',e=>notice(e.detail.message,true));
  const toolbar=node('div',undefined,'montage-toolbar'),status=node('span',undefined,'montage-save');
  const heading=node('h2','Монтаж');root.append(heading,toolbar);
  const disclosure=(title,content,cls='')=>{const d=node('details',undefined,'montage-disclosure '+cls);d.append(node('summary',title),content);return d;};
  const more=node('div',undefined,'montage-more-actions'),morePanel=disclosure('Ещё',more,'montage-more');
  let selected=null,selectedKind='video',selectedLayer='footage',page=0,disposed=false,job,timer,lanes;
  const stage=node('div',undefined,'montage-stage'),top=node('div',undefined,'montage-top'),inspector=node('aside',undefined,'montage-inspector'),preview=node('div');
  const controls=node('div',undefined,'montage-toolbar'),clock=node('span','0.00 с'),seek=node('input');seek.type='range';seek.min=0;seek.step=1;seek.setAttribute('aria-label','Время монтажа');
  const play=button('▶ Воспроизвести',()=>player.playing?player.pause():player.play());
  stage.tabIndex=0;stage.setAttribute('role','region');stage.setAttribute('aria-label','Предпросмотр видео');
  const player=new MontagePlayer(stage,(t,playing)=>{seek.value=t;clock.textContent=`${timelineTime(t)} / ${timelineTime(player.duration||0)}`;play.textContent=playing?'Ⅱ Пауза':'▶ Воспроизвести';lanes?.setTime(t,playing);},e=>notice(e.message,true));
  seek.oninput=()=>player.seek(Number(seek.value));
  const jump=direction=>{const clips=player.timeline.clips;const index=clips.findIndex(c=>c.start_ms<=player.time&&player.time<c.end_ms);player.seek(clips[Math.max(0,Math.min(clips.length-1,index+direction))]?.start_ms||0);};
  controls.append(button('‹ Клип',()=>jump(-1)),play,button('Клип ›',()=>jump(1)),clock,button('Полный экран',()=>stage.requestFullscreen()));
  field(controls,'Громкость плеера',1,v=>player.masterVolume=v,{min:0,max:1,step:.05});
  preview.className='montage-preview';preview.append(stage,controls,seek);top.append(preview);root.append(top);
  const strip=node('div',undefined,'montage-strip'),pagination=node('div',undefined,'montage-toolbar'),audio=node('div',undefined,'montage-audio'),exports=node('section',undefined,'montage-exports');
  const timelineHost=node('div');root.append(timelineHost);
  const splitter=node('div',undefined,'montage-preview-splitter');splitter.tabIndex=0;splitter.setAttribute('role','separator');splitter.setAttribute('aria-label','Высота предпросмотра');splitter.setAttribute('aria-orientation','horizontal');
  splitter.title='Потяните вверх или вниз, чтобы изменить высоту предпросмотра';splitter.style.cssText='height:10px;min-height:10px;width:100%;position:relative;z-index:100;flex:none;cursor:row-resize;touch-action:none;background:linear-gradient(transparent 4px,#655087 4px,#655087 6px,transparent 6px);border-radius:5px;';root.insertBefore(splitter,timelineHost);
  const heightKey='frameforge-preview-height';
  const resizePreview=h=>{h=Math.max(200,Math.min(window.innerHeight-140,h));const body=preview.closest('.montage-editor-body');if(body){body.style.height=(h+20)+'px';body.style.minHeight='0';body.style.flex='0 0 auto';}preview.style.height=h+'px';preview.style.flex='none';splitter.setAttribute('aria-valuenow',String(Math.round(h)));try{localStorage.setItem(heightKey,String(h));}catch{}};
  try{const saved=Number(localStorage.getItem(heightKey));if(saved>=200)resizePreview(saved);}catch{}
  splitter.onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();player.pause();splitter.setPointerCapture(e.pointerId);const y=e.clientY,h=preview.getBoundingClientRect().height;const move=ev=>resizePreview(h+ev.clientY-y);const stop=()=>{splitter.removeEventListener('pointermove',move);splitter.removeEventListener('pointerup',stop);splitter.removeEventListener('pointercancel',stop);if(splitter.hasPointerCapture(e.pointerId))splitter.releasePointerCapture(e.pointerId);};splitter.addEventListener('pointermove',move);splitter.addEventListener('pointerup',stop);splitter.addEventListener('pointercancel',stop);};
  splitter.onkeydown=e=>{if(['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();resizePreview(preview.getBoundingClientRect().height+(e.key==='ArrowUp'?-20:20));}};
  splitter.ondblclick=()=>resizePreview(360);
  const inspectorPanel=disclosure('Настройки выбранного клипа',inspector,'montage-clip-settings');root.append(inspectorPanel);
  const audioPanel=disclosure('Озвучка и музыка',audio,'montage-audio-settings');
  const sceneList=node('details',undefined,'montage-scene-list');sceneList.append(node('summary','Список сцен'),pagination,strip);root.append(audioPanel,sceneList,exports);root.insertBefore(exports,inspectorPanel);
  const saveStatus=()=>{if(disposed)return;status.textContent=s.saving?'Сохраняю…':s.error?`Не сохранено: ${s.error}`:s.dirty?'Не сохранено':'Сохранено · версия '+s.revision;status.dataset.dirty=String(s.dirty);};
  s.notify=saveStatus;
  const changed=()=>{s.version++;s.dirty=true;if(!s.conflict)s.error='';persist(s);clearTimeout(s.timer);s.timer=setTimeout(()=>flush(s).catch(e=>notice(e.message,true)),650);render();};
  const edit=fn=>{const before=copy(s.doc);fn(s.doc);if(JSON.stringify(before)===JSON.stringify(s.doc))return;s.history.push(before);if(s.history.length>60)s.history.shift();s.future=[];changed();};
  const sync=async(addOnly,clipIds)=>{await flush(s);const result=await api(`/projects/${id}/edit/sync`,{expected_revision:s.revision,document:s.doc,add_only:addOnly,clip_ids:clipIds});edit(()=>s.doc=result.document);if(result.adjusted_clips.length)notice('Обрезки сокращены до длительности новых источников. Изменение можно отменить.');};
  const undo=()=>{if(!s.history.length)return;s.future.push(copy(s.doc));s.doc=s.history.pop();changed();};
  const redo=()=>{if(!s.future.length)return;s.history.push(copy(s.doc));s.doc=s.future.pop();changed();};
  const deleteSelected=()=>{
    if(selectedKind==='video'){
      if(!selected)return;
      const index=s.doc.clips.findIndex(x=>x.edit_clip_id===selected);
      if(index<0)return;
      const toRemove=s.doc.clips[index];
      if(toRemove.footage_overlay&&selectedLayer==='footage'){
        player.pause();selectedLayer='base';edit(()=>{delete toRemove.footage_overlay;toRemove.media_replacement_locked=true;});
        notice('Футаж удалён. Исходный кадр возвращён; длительность и озвучка сохранены. Отмена — Ctrl+Z.');return;
      }
      const name=toRemove.source_kind==='footage'?(toRemove.title||'Футаж'):`Сцена ${sceneMap.get(toRemove.scene_id)?.position||'?'}`;
      edit(doc=>{
        const i=doc.clips.findIndex(x=>x.edit_clip_id===selected);
        if(i>=0){
          doc.clips.splice(i,1);
          doc.transitions=(doc.transitions||[]).filter(t=>t.left!==selected&&t.right!==selected);
        }
      });
      selected=s.doc.clips[Math.min(index,s.doc.clips.length-1)]?.edit_clip_id||null;
      if(lanes)lanes.selection=selected?{kind:'video',id:selected}:null;
      render();
      notice(`Удалено: ${name}. Отмена — Ctrl+Z.`);
    }else if(selectedKind==='voice'||selectedKind==='music'){
      const a=s.doc.audio_tracks.find(t=>t.role===selectedKind);
      if(!a)return;
      const label=selectedKind==='voice'?'Озвучка':'Музыка';
      edit(doc=>{
        doc.audio_tracks=doc.audio_tracks.filter(t=>t.role!==selectedKind);
      });
      render();
      notice(`Удалена дорожка: ${label}. Отмена — Ctrl+Z.`);
    }
  };
  async function addLibraryItem(item){
    player.pause();
    const target=selected;
    if(selectedKind!=='video'||!s.doc.clips.some(c=>c.edit_clip_id===target&&!c.excluded&&c.source_kind!=='footage'))throw Error('Выберите кадр на таймлайне для ручной вставки.');
    const result=await api(`/projects/${id}/edit/media-library/${item.id}/clip`,{});
    edit(doc=>{if(!overlayFootage(doc,target,result.clip))throw Error('Выбранный кадр изменился. Выберите место вставки снова.');selectedKind='video';selectedLayer='footage';});
    await flush(s);notice('Материал добавлен вручную. Изменение можно отменить.');
  }
  const footageInput=node('input');footageInput.type='file';footageInput.accept='.png,.jpg,.jpeg,.webp,.bmp,.mp4,.mov,.m4v,.mkv,.webm';footageInput.hidden=true;
  const manualTarget=()=>selectedKind==='video'&&s.doc.clips.find(c=>c.edit_clip_id===selected&&!c.excluded&&c.source_kind!=='footage');
  const addFootage=button('Добавить фото / видео',()=>{if(!manualTarget())throw Error('Выберите кадр на таймлайне для ручной вставки.');footageInput.click();});
  footageInput.onchange=async()=>{
    const file=footageInput.files[0];if(!file)return;
    addFootage.disabled=true;player.pause();
    try{
      const target=manualTarget()?.edit_clip_id;
      if(!target)throw Error('Выберите кадр на таймлайне для ручной вставки.');
      if(file.size>512000000)throw Error(`${file.name}: файл должен быть не больше 512 МБ.`);
      const result=await api(`/projects/${id}/edit/video`,file);
      edit(doc=>{if(!overlayFootage(doc,target,result.clip))throw Error('Выбранный кадр изменился. Выберите место вставки снова.');selectedKind='video';selectedLayer='footage';});
      await flush(s);notice('Футаж наложен на выбранный кадр. Изменение можно отменить.');
    }catch(e){notice(e.message,true);}finally{addFootage.disabled=false;footageInput.value='';}
  };
  toolbar.append(addFootage,footageInput);
  function onDocumentChanged(newDoc,newRev,sources){
      s.history.push(copy(s.doc));s.future=[];
      if(sources)s.sources=sources;
      s.doc = newDoc;
      s.revision = newRev;
      s.dirty = false;
      render();
    }
  mountMediaLibraryButton(toolbar, { projectId: id, api, notice, onManualAdd:addLibraryItem, onDocumentChanged, beforeOpen: async()=>{player.pause();await flush(s);} });
  mountMaterialSearchButton(toolbar, {
    projectId: id,
    api,
    notice,
    beforeOpen: async () => { player.pause(); await flush(s); },
    getSelectedClip: () => {
      const clip = s.doc.clips.find(c => c.edit_clip_id === selected);
      const scene = sceneMap.get(clip?.scene_id)?.data;
      return {
        clip_id: manualTarget()?.edit_clip_id || null,
        expected_revision: s.revision,
        quote: scene?.source_quote || '',
        search_text: scene?.source_quote || scene?.beat || '',
      };
    },
    onManualAdd: addLibraryItem,
    onDocumentChanged
  });
  toolbar.append(button('Добавить готовые сцены',()=>sync(true)),button('Обновить все источники',()=>sync(false)),button('Сохранить',()=>flush(s)),button('↶ Отменить',()=>undo()),button('↷ Повторить',()=>redo()),status);
  more.append(button('Перечитать с сервера',async()=>{if(s.dirty){try{localStorage.setItem(key(id)+':recovery',JSON.stringify(s.doc));}catch{}}const result=await api(`/projects/${id}/edit`);s.doc=result.document;s.revision=result.revision;s.sources=result.sources;s.lastExport=result.last_export_id;s.conflict=false;s.dirty=false;s.error='';s.history=[];s.future=[];localStorage.removeItem(key(id));render();showJob();notice('Монтаж перечитан. Предыдущий несохранённый черновик оставлен в резервной копии браузера.');}));
  more.append(button('Скачать черновик',()=>{const data=localStorage.getItem(key(id)+':recovery');const url=URL.createObjectURL(new Blob([s.dirty?JSON.stringify(s.doc,null,2):data||JSON.stringify(s.doc,null,2)],{type:'application/json'}));const link=node('a');link.href=url;link.download='frameforge-montage-draft.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);}));
  for(const b of [...toolbar.querySelectorAll('button')]){if(['Обновить все источники'].includes(b.textContent))more.prepend(b);}
  toolbar.append(morePanel);
  const sceneMap=new Map(project.scenes.map(scene=>[scene.id,scene]));
  const fitHost=node('div',undefined,'montage-fit-settings');
  const editorBody=node('div',undefined,'montage-editor-body'),library=node('aside',undefined,'montage-library'),libraryTabs=node('div',undefined,'montage-library-tabs');
  library.setAttribute('aria-label','Материалы проекта');libraryTabs.setAttribute('role','tablist');libraryTabs.setAttribute('aria-label','Инструменты монтажа');
  const transitionPane=node('div',undefined,'montage-transition-pane');
  const panels={media:sceneList,audio:audioPanel,transitions:transitionPane,fit:fitHost},tabButtons={};
  const activateLibrary=name=>{for(const [key,panel] of Object.entries(panels)){panel.hidden=key!==name;tabButtons[key].setAttribute('aria-selected',String(key===name));}};
  for(const [name,label] of [['media','Медиа'],['audio','Звук'],['transitions','Переходы'],['fit','Подгонка']]){
   const tab=button(label,()=>activateLibrary(name));tab.setAttribute('role','tab');tab.id='montage-tab-'+name;tab.setAttribute('aria-controls','montage-panel-'+name);tabButtons[name]=tab;libraryTabs.append(tab);
   const panel=panels[name];panel.id='montage-panel-'+name;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tab.id);if(panel.tagName==='DETAILS')panel.open=true;
  }
  library.append(libraryTabs,sceneList,audioPanel,transitionPane,fitHost);activateLibrary('media');
  inspectorPanel.open=true;inspectorPanel.setAttribute('aria-label','Свойства выбранного клипа');
  const resizerLeft=node('div',undefined,'montage-resizer montage-resizer-left');
  resizerLeft.setAttribute('role','separator');resizerLeft.setAttribute('aria-label','Изменить ширину левой панели');resizerLeft.title='Потяните для изменения ширины (двойной клик — сброс)';
  const resizerRight=node('div',undefined,'montage-resizer montage-resizer-right');
  resizerRight.setAttribute('role','separator');resizerRight.setAttribute('aria-label','Изменить ширину правой панели');resizerRight.title='Потяните для изменения ширины (двойной клик — сброс)';
  editorBody.append(library,resizerLeft,top,resizerRight,inspectorPanel);root.insertBefore(editorBody,timelineHost);
  root.insertBefore(splitter,timelineHost);if(preview.style.height)resizePreview(parseFloat(preview.style.height));
  const storageKeyLayout='frameforge-montage-panel-widths';
  let leftWidth=280,rightWidth=300;
  try{const saved=JSON.parse(localStorage.getItem(storageKeyLayout));if(saved&&typeof saved.left==='number')leftWidth=saved.left;if(saved&&typeof saved.right==='number')rightWidth=saved.right;}catch{}
  const applyPanelWidths=()=>{editorBody.style.setProperty('--montage-left-width',`${leftWidth}px`);editorBody.style.setProperty('--montage-right-width',`${rightWidth}px`);};
  applyPanelWidths();
  const bindResizer=(resizer,side)=>{
    resizer.onpointerdown=e=>{
      if(e.button!==0)return;e.preventDefault();try{resizer.setPointerCapture(e.pointerId);}catch{}resizer.classList.add('is-dragging');document.body.classList.add('montage-resizing');
      const startX=e.clientX,startLeft=leftWidth,startRight=rightWidth;
      const onMove=event=>{
        const bodyBox=editorBody.getBoundingClientRect(),delta=event.clientX-startX;
        if(side==='left'){
          const minW=180,maxW=Math.max(minW,bodyBox.width-rightWidth-280);
          leftWidth=Math.max(minW,Math.min(maxW,startLeft+delta));
        }else{
          const minW=200,maxW=Math.max(minW,bodyBox.width-leftWidth-280);
          rightWidth=Math.max(minW,Math.min(maxW,startRight-delta));
        }
        applyPanelWidths();
      };
      const onStop=()=>{
        window.removeEventListener('pointermove',onMove);window.removeEventListener('pointerup',onStop);window.removeEventListener('pointercancel',onStop);
        resizer.removeEventListener('pointermove',onMove);resizer.removeEventListener('pointerup',onStop);resizer.removeEventListener('pointercancel',onStop);
        try{resizer.releasePointerCapture(e.pointerId);}catch{}
        resizer.classList.remove('is-dragging');document.body.classList.remove('montage-resizing');
        try{localStorage.setItem(storageKeyLayout,JSON.stringify({left:Math.round(leftWidth),right:Math.round(rightWidth)}));}catch{}
        window.dispatchEvent(new Event('resize'));
      };
      window.addEventListener('pointermove',onMove);window.addEventListener('pointerup',onStop);window.addEventListener('pointercancel',onStop);
      resizer.addEventListener('pointermove',onMove);resizer.addEventListener('pointerup',onStop);resizer.addEventListener('pointercancel',onStop);
    };
    resizer.ondblclick=()=>{
      if(side==='left')leftWidth=280;else rightWidth=300;
      applyPanelWidths();try{localStorage.setItem(storageKeyLayout,JSON.stringify({left:leftWidth,right:rightWidth}));}catch{}
      window.dispatchEvent(new Event('resize'));
    };
  };
  bindResizer(resizerLeft,'left');bindResizer(resizerRight,'right');
  const exportPanel=disclosure('Экспорт',exports,'montage-export-menu');toolbar.append(exportPanel);
  const fit=mountVoiceFit(fitHost,{id,api,importedPrompts:Boolean(project.analysis_metadata?.prompt_import),flush:()=>flush(s),getState:()=>s,pause:()=>player.pause(),notice,accept:result=>{
   s.history.push(copy(s.doc));s.future=[];s.doc=result.document;s.revision=result.revision;s.sources=result.sources;s.version++;s.dirty=false;s.error='';s.conflict=false;localStorage.removeItem(key(id));render();showJob();
  }});
  lanes=new MontageLanes(timelineHost,{projectId:id,getDocument:()=>s.doc,sceneLabel:sid=>sceneMap.get(sid)?.position||'?',onEdit:edit,onPause:()=>player.pause(),onSeek:ms=>player.seek(ms),onError:e=>notice(e.message,true),onUpload:role=>audio.querySelector(`[data-upload-role="${role}"]`)?.click(),onDelete:deleteSelected,onSelect:(kind,cid)=>{
    selectedLayer=kind==='overlay'?'footage':'base';selectedKind=kind==='overlay'?'video':kind;if(selectedKind==='video'){selected=cid;inspectorPanel.open=true;}else{activateLibrary('audio');audioPanel.open=true;}
    renderInspector(normalize(s.doc));for(const n of strip.children)n.classList.toggle('selected',kind==='video'&&n.dataset.clip===selected);
    for(const n of audio.children)n.classList.toggle('selected',n.dataset.audioRole===kind);
  }});
  transitionPane.append(node('h3','Переходы между сценами'),node('p','Выберите вид, частоту и длительность.'),timelineHost.querySelector('.timeline-auto-transitions'));
  function renderInspector(timeline){
    if(selectedKind!=='video'){
      inspector.replaceChildren();const a=s.doc.audio_tracks.find(t=>t.role===selectedKind),label=selectedKind==='voice'?'Озвучка':'Музыка';inspector.append(node('h3',label));
      if(a)inspector.append(node('p',`На шкале: ${timelineTime(a.offset_ms||0)} — ${timelineTime((a.offset_ms||0)+a.trim_out_ms-a.trim_in_ms)}`),node('p','Перетащите блок для изменения старта. Потяните край для обрезки.'),button('Настройки '+(selectedKind==='voice'?'озвучки':'музыки'),()=>audio.querySelector(`[data-audio-role="${selectedKind}"]`)?.scrollIntoView({behavior:'smooth',block:'center'})));
      return;
    }
    inspector.replaceChildren();const base=s.doc.clips.find(c=>c.edit_clip_id===selected);const c=selectedLayer==='footage'&&base?.footage_overlay?base.footage_overlay:base;
    if(!c){
      const emptyBox=node('div',undefined,'montage-empty-box');
      emptyBox.append(
        node('div','🎞️','montage-empty-icon'),
        node('strong','Клип не выбран'),
        node('p','Добавьте готовые сцены, затем выберите клип.')
      );
      inspector.append(emptyBox);
      return;
    }
    const scene=sceneMap.get(c.scene_id),advanced=node('div'),advancedPanel=disclosure('Дополнительно',advanced,'montage-property-advanced');inspectorPanel.querySelector('summary').textContent=c.source_kind==='footage'?'Настройки футажа':'Настройки сцены '+(scene?.position||'?');inspector.append(node('h3',c.source_kind==='footage'?(c.title||'Футаж'):'Исходная сцена '+(scene?.position||'?')),node('p',c.source_kind==='footage'?'Отдельный клип: обрезайте и перемещайте на видеодорожке.':scene?.data?.beat||''));
    if(base.footage_overlay){
      const layerTools=node('div',undefined,'montage-layer-actions');
      layerTools.append(node('p','Исходный кадр сохранён под футажом. Длительность сегмента общая.'));
      layerTools.append(button(base.footage_overlay.enabled===false?'Показать футаж':'Показать исходный кадр',()=>{player.pause();edit(()=>{base.footage_overlay.enabled=base.footage_overlay.enabled===false;});}));
      layerTools.append(button('Удалить футаж и вернуть кадр',()=>{player.pause();selectedLayer='base';edit(()=>{delete base.footage_overlay;base.media_replacement_locked=true;});notice('Исходный кадр возвращён. Отмена — Ctrl+Z.');}));inspector.append(layerTools);
    }
    const set=(name,value)=>edit(()=>{
      if(c!==base&&name==='excluded'){c.enabled=!value;return;}
      if(c!==base&&['trim_in_ms','trim_out_ms'].includes(name)){
        const span=segmentDuration(base)*(c.playback_rate??1),start=name==='trim_in_ms'?value:value-span;
        if(start<0||start+span>c.source_duration_ms+.01)throw Error('Выбранный отрезок не покрывает весь сегмент.');
        c.trim_in_ms=start;c.trim_out_ms=start+span;return;
      }
      c[name]=value;if(['trim_in_ms','trim_out_ms','hold_last_ms'].includes(name)){c.manual_timing=true;c.timing_locked=true;}
    });
    field(advanced,'Сохранить выбор исходного кадра при подборе',base.media_replacement_locked,v=>edit(()=>base.media_replacement_locked=v),{type:'checkbox'});
    field(advanced,'Сохранить ручные границы при подгонке',c.timing_locked||c.manual_timing,v=>edit(()=>{c.timing_locked=v;c.manual_timing=v;c.allow_voice_retime=!v;}),{type:'checkbox'});
    if(c.media_type==='image'){
      const layer = c === base ? 'base' : 'footage';
      const prepare = endpoint => async settings => {
          await flush(s);
          const version = s.version, expected_revision = s.revision;
          const result = await api(`/projects/${id}/edit/${endpoint}`,{
            ...settings, layer, clip_id:base.edit_clip_id, expected_revision
          });
          const commit = ()=>{
            const current = s.doc.clips.find(item=>item.edit_clip_id===base.edit_clip_id);
            const target = layer==='footage' ? current?.footage_overlay : current;
            if(disposed || s.version!==version || s.revision!==expected_revision || target!==c)
              throw Error('Монтаж изменился. Откройте эффект заново.');
            edit(()=>{target.foreground_effect=result.effect;});
            const active = normalize(s.doc).clips.find(item=>item.edit_clip_id===base.edit_clip_id);
            if(active){
              const peak = result.effect.mode === 'glint' ? result.effect.peak : .5;
              const span = Math.max(0, active.base_duration_ms - 1000/24);
              player.seek(active.start_ms + span * peak);
            }
            notice('Передний план оживлён. Эффект можно отключить в свойствах клипа.');
          };
          return Object.assign(commit,{effect:result.effect});
      };
      inspector.append(button('Параллакс из двух изображений',()=>{
        player.pause();
        foregroundPairDialog(c,{beat:scene?.data?.beat},prepare('foreground-pair'));
      }));
      if(c.foreground_effect?.mode==='parallax'){
        field(inspector,'Смещение по горизонтали, %',Number((c.foreground_effect.shift_x*100).toFixed(2)),
          value=>edit(()=>{c.foreground_effect.shift_x=value/100;}),{min:-6,max:6,step:.2});
        field(inspector,'Смещение по вертикали, %',Number((c.foreground_effect.shift_y*100).toFixed(2)),
          value=>edit(()=>{c.foreground_effect.shift_y=value/100;}),{min:-6,max:6,step:.2});
      }
      if(c.foreground_effect)inspector.append(button('Отключить эффект переднего плана',()=>{
        player.pause();edit(()=>{if(c.foreground_effect.flow_pair)c.foreground_pair_disabled=c.source_image_asset_id;delete c.foreground_effect;});
      }));
    }
    if(c.media_type==='image')inspector.append(node('p',c.foreground_effect?.mode==='parallax'?'Передний план движется целиком, задник неподвижен.':c.foreground_effect?.motion?'Видео передней сцены поверх неподвижного фона.':c.foreground_effect?.flow_pair?'Для этого кадра ещё нет видео переднего плана. Запустите генерацию в Flow.':c.foreground_effect?'Фон неподвижен. Эффект действует только на переднем плане.':c.camera_motion?'2.5D из исходного изображения: движение рассчитано на длительность клипа.':'Неподвижное изображение удерживается весь интервал.'));
    if(c.hold_last_ms)field(inspector,'Стоп-кадр в конце, с',Number((c.hold_last_ms/1000).toFixed(2)),v=>set('hold_last_ms',v*1000),{max:86400,step:.01});
    if(c.media_type==='image')field(inspector,'Длительность изображения, с',(c.trim_out_ms-c.trim_in_ms)/1000,v=>set('trim_out_ms',c.trim_in_ms+v*1000),{min:1/24,max:86400,step:.01});
    else{
     field(inspector,'Начало в исходнике, с',c.trim_in_ms/1000,v=>{if(v*1000>=c.trim_out_ms)throw Error('Начало должно быть раньше конца.');set('trim_in_ms',v*1000);},{max:(c.trim_out_ms-1000/24)/1000,step:.01});
     field(inspector,'Конец в исходнике, с',c.trim_out_ms/1000,v=>{if(v*1000<=c.trim_in_ms)throw Error('Конец должен быть позже начала.');set('trim_out_ms',v*1000);},{min:(c.trim_in_ms+1000/24)/1000,max:c.source_duration_ms/1000,step:.01});
    }
    if(c.media_type!=='image'){
      field(inspector,'Скорость клипа, ×',c.playback_rate??1,v=>edit(doc=>setClipSpeed(doc,base.edit_clip_id,c!==base,v)),{min:.05,max:4,step:.05});
      inspector.append(node('p','Изменение скорости меняет длительность сегмента. Края на таймлайне обрезают сегмент вместе с футажом. Озвучка сохраняет свою скорость.','muted'));
    }
    field(inspector,c!==base?'Скрыть футаж, показать исходный кадр':'Исключён из монтажа',c!==base?c.enabled===false:c.excluded,v=>set('excluded',v),{type:'checkbox'});
    field(inspector,'Выключить звук клипа',c.muted,v=>set('muted',v),{type:'checkbox'});
    field(advanced,'Громкость клипа',c.volume??1,v=>set('volume',v),{max:1,step:.05});
    if(c.source_kind==='footage')field(inspector,'Пометка «Иллюстративный план» в экспорте',c.illustrative,v=>set('illustrative',v),{type:'checkbox'});
    if(c.source_kind==='footage')field(inspector,'Заполнить кадр без чёрных полос',c.framing==='fill',v=>set('framing',v?'fill':'fit'),{type:'checkbox'});
    const actions=node('div',undefined,'montage-toolbar');
    for(const [label,delta] of [['←',-1],['→',1]])actions.append(button(label,()=>{const index=s.doc.clips.findIndex(x=>x.edit_clip_id===c.edit_clip_id),to=index+delta;if(to<0||to>=s.doc.clips.length)return;edit(doc=>{doc.clips.splice(index,1);doc.clips.splice(to,0,base);});}));
    if(c.source_kind!=='footage')actions.append(button('Обновить источник клипа',()=>sync(false,[c.edit_clip_id])));const delBtn=button('🗑️ Удалить клип',()=>deleteSelected());delBtn.className='montage-btn-danger';actions.append(delBtn);advanced.append(actions);inspector.append(advancedPanel);
    const state=s.sources[c.edit_clip_id];if(state?.stale||state?.new_version||state?.missing)inspector.append(node('p',state.missing?'Источник отсутствует':state.stale?'Анимация устарела':'Есть новая версия','montage-warning'));
    const active=timeline.clips,index=active.findIndex(x=>x.edit_clip_id===c.edit_clip_id),next=active[index+1];
    if(index<0||!next)return;
    const existing=s.doc.transitions.find(t=>t.left===c.edit_clip_id&&t.right===next.edit_clip_id)||{kind:'cut',duration_ms:s.doc.auto_transitions?.duration_ms||500};
    const transition=kind=>edit(doc=>{doc.transitions=doc.transitions.filter(t=>!(t.left===c.edit_clip_id&&t.right===next.edit_clip_id));doc.transitions.push({left:c.edit_clip_id,right:next.edit_clip_id,kind,duration_ms:existing.duration_ms});});
    const select=node('select');for(const [value,label] of [['cut','Склейка'],['dissolve','Растворение'],['black','Через чёрный'],['white','Через белый']]){const option=node('option',label);option.value=value;option.selected=value===existing.kind;select.append(option);}select.onchange=()=>transition(select.value);
    inspector.append(node('h4','Переход к следующему клипу'),select);
    field(inspector,'Длительность стыка, с',existing.duration_ms/1000,v=>{existing.duration_ms=v*1000;transition(existing.kind);},{max:5,step:.05});
    inspector.append(node('p',`Фактический стык: ${time(active[index].out_ms)}. Длина ограничена соседними клипами.`,'muted'),button('Применить ко всем стыкам',()=>edit(doc=>{doc.transitions=active.slice(0,-1).map((clip,i)=>({left:clip.edit_clip_id,right:active[i+1].edit_clip_id,kind:existing.kind,duration_ms:existing.duration_ms}));})));
  }
  function renderAudio(){
    audio.replaceChildren();field(audio,'Включить звуки исходных клипов',s.doc.source_audio,v=>edit(doc=>doc.source_audio=v),{type:'checkbox'});
    for(const [role,title] of [['voice','Озвучка'],['music','Музыка']]){
      const row=node('section',undefined,'montage-track');row.dataset.audioRole=role;row.classList.toggle('selected',selectedKind===role);row.append(node('h3',title));audio.append(row);
      const upload=node('input');upload.type='file';upload.dataset.uploadRole=role;upload.accept='.wav,.mp3,.m4a,.aac,.mp4,.ogg,.flac';upload.setAttribute('aria-label','Загрузить '+(role==='voice'?'озвучку':'музыку'));
      upload.onchange=async()=>{const file=upload.files[0];if(!file)return;if(file.size>100000000){notice('Аудиофайл должен быть не больше 100 МБ.',true);return;}upload.disabled=true;try{const data=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file);});const result=await api(`/projects/${id}/edit/audio`,{filename:file.name,data});edit(doc=>{doc.audio_tracks=doc.audio_tracks.filter(a=>a.role!==role);doc.audio_tracks.push({role,asset_id:result.asset_id,source_duration_ms:result.duration_ms,trim_in_ms:0,trim_out_ms:result.duration_ms,offset_ms:0,volume:role==='music'?.25:1,fade_in_ms:0,fade_out_ms:0,enabled:true});});}catch(e){notice(e.message,true);}finally{upload.disabled=false;}};
      row.append(upload);
      const a=s.doc.audio_tracks.find(a=>a.role===role);if(!a){row.append(node('p','Файл не загружен','muted'));continue;}
      row.append(node('p',`Длительность файла: ${time(a.source_duration_ms)} · на шкале ${time(a.offset_ms)} — ${time(a.offset_ms+a.trim_out_ms-a.trim_in_ms)}`));
      const set=(name,value)=>edit(()=>a[name]=value);
      field(row,'Включена',a.enabled,v=>set('enabled',v),{type:'checkbox'});
      for(const [name,label,max] of [['offset_ms','Старт на шкале, с',86400000],['trim_in_ms','Начало в файле, с',a.trim_out_ms-1],['trim_out_ms','Конец в файле, с',a.source_duration_ms],['fade_in_ms','Появление, с',86400000],['fade_out_ms','Затухание, с',86400000]])field(row,label,(a[name]||0)/1000,v=>set(name,v*1000),{max:max/1000,min:name==='trim_out_ms'?(a.trim_in_ms+1)/1000:0,step:.01});
      field(row,'Громкость',a.volume,v=>set('volume',v),{max:1,step:.05});
      row.append(button('Убрать дорожку',()=>edit(doc=>doc.audio_tracks=doc.audio_tracks.filter(t=>t.role!==role))));
    }
  }
  function render(){
    if(disposed)return;
    const timeline=normalize(s.doc);player.setDocument(s.doc);seek.max=timelineExtent(s.doc);
    saveStatus();renderInspector(timeline);renderAudio();lanes.selection={kind:selectedKind==='video'&&selectedLayer==='footage'&&s.doc.clips.find(c=>c.edit_clip_id===selected)?.footage_overlay?'overlay':selectedKind,id:selectedKind==='video'?selected:selectedKind};lanes.render();strip.replaceChildren();pagination.replaceChildren();
    page=Math.min(page,Math.max(0,Math.ceil(s.doc.clips.length/32)-1));
    pagination.append(node('span',`${timeline.clips.length} клипов · ${timelineTime(timeline.duration_ms)}`),button('‹ Лента',()=>{page=Math.max(0,page-1);render();}),node('span',`Страница ${page+1} / ${Math.max(1,Math.ceil(s.doc.clips.length/32))}`),button('Лента ›',()=>{page=Math.min(Math.max(0,Math.ceil(s.doc.clips.length/32)-1),page+1);render();}));
    if(!s.doc.clips.length){
      const emptyBox=node('div',undefined,'montage-empty-box');
      emptyBox.append(
        node('div','🎬','montage-empty-icon'),
        node('strong','В монтаже пока нет сцен'),
        node('p',`Готовых видео в проекте: ${project.scenes.filter(x=>x.video_asset_id&&!x.video_stale).length}. Нажмите «Добавить готовые сцены» на панели выше, чтобы начать монтаж.`)
      );
      strip.append(emptyBox);
    }
    for(const c of s.doc.clips.slice(page*32,page*32+32)){
      const scene=sceneMap.get(c.scene_id),card=button('',()=>{selectedKind='video';selected=c.edit_clip_id;lanes.select('video',selected);renderInspector(timeline);for(const n of strip.children)n.classList.toggle('selected',n.dataset.clip===selected);const active=timeline.clips.find(x=>x.edit_clip_id===selected);if(active){player.seek(active.start_ms);lanes.setTime(active.start_ms,true);}});
      card.className='montage-clip'+(c.excluded?' excluded':'')+(selected===c.edit_clip_id?' selected':'');card.dataset.clip=c.edit_clip_id;
      const label=c.source_kind==='footage'?(c.title||'Футаж'):`Сцена ${scene?.position||'?'}`;
      const durText=time((c.trim_out_ms-c.trim_in_ms)/(c.playback_rate??1)+(c.hold_last_ms||0));
      card.title=`${label}${c.excluded?' · исключена':''} · ${durText}`;
      if(c.source_image_asset_id){const image=node('img');image.loading='lazy';image.src='/api/assets/'+c.source_image_asset_id;image.alt=label;card.append(image);}
      else{const placeholder=node('div',c.source_kind==='footage'?'🎞️':'🎬','montage-clip-placeholder');placeholder.setAttribute('aria-hidden','true');card.append(placeholder);}
      card.append(node('strong',label+(c.excluded?' · исключена':'')),node('span',durText));strip.append(card);
    }
  }
  function showJob(){
    exports.replaceChildren(node('h3','Экспорт'));
    const quality=node('label','Качество '),select=node('select');
    for(const [height,title] of [[1080,'Full HD · 1920 × 1080'],[720,'HD · 1280 × 720']]){const option=node('option',title);option.value=String(height);option.selected=height===(s.doc.export_settings?.height||1080);select.append(option);}
    select.onchange=()=>{edit(doc=>doc.export_settings={width:Number(select.value)===1080?1920:1280,height:Number(select.value),fps:24});showJob();};quality.append(select);exports.append(quality);
    more.replaceChildren(...[...more.children].filter(n=>n.dataset.exportExtra!=='true'));const zip=button('Скачать кадры (.zip)',downloadZip);zip.dataset.exportExtra='true';more.append(zip);exports.append(button('Экспортировать MP4',async()=>{await flush(s);job=await api(`/projects/${id}/edit/export`,{expected_revision:s.revision});s.lastExport=job.id;localStorage.setItem(key(id)+':job',job.id);showJob();poll();}));
    if(s.doc.voice_fit?.cues?.length)exports.append(button('Скачать субтитры (.srt)',async()=>{await flush(s);const result=await api(`/projects/${id}/edit/subtitles`);const url=URL.createObjectURL(new Blob([result.srt],{type:'text/plain;charset=utf-8'}));const a=node('a');a.href=url;a.download='frameforge.srt';a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);}));
    if(!job)return;
    if(['queued','running'].includes(job.status)){const progress=node('progress');progress.max=100;progress.value=job.progress;exports.append(node('p',`${job.stage} · ${Math.round(job.progress)}%`),progress);}
    if(job.error)exports.append(node('p',job.error,'montage-warning'));
    if(job.url){const link=node('a',job.edit_revision===s.revision?'Скачать готовое видео':'Скачать предыдущий экспорт');link.href=job.url;link.download='frameforge.mp4';exports.append(link);}
    if(['queued','running'].includes(job.status))exports.append(button('Отменить экспорт',async()=>{job=await api(`/projects/${id}/edit/exports/${job.id}/cancel`,{});showJob();}));
  }
  async function poll(){clearTimeout(timer);if(disposed||!job)return;try{const announce=job.announce,previous=job.status;job=await api(`/projects/${id}/edit/exports/${job.id}`);if(disposed)return;showJob();if(job.status==='completed'&&(announce||['queued','running'].includes(previous)))notice(`Видео готово · ${job.width} × ${job.height}. Можно скачать MP4.`);if(['queued','running'].includes(job.status))timer=setTimeout(poll,1000);}catch(e){notice(e.message,true);}}
  const keyboard=e=>{
    if(disposed)return;
    const isTyping=Boolean(e.target?.closest?.('input:not([type="checkbox"]):not([type="radio"]), textarea, select, [contenteditable="true"]'));
    if(isTyping)return;
    if(e.defaultPrevented||e.altKey)return;
    if(e.ctrlKey||e.metaKey){
      if((e.key==='z'||e.key==='Z'||e.code==='KeyZ')&&!e.shiftKey){if(s.history.length){e.preventDefault();undo();}}
      else if(((e.key==='z'||e.key==='Z'||e.code==='KeyZ')&&e.shiftKey)||(e.key==='y'||e.key==='Y'||e.code==='KeyY')){if(s.future.length){e.preventDefault();redo();}}
      return;
    }
    if(e.code==='Space'){e.preventDefault();player.playing?player.pause():player.play();}
    else if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();player.seek(Math.max(0,Math.min(player.duration,player.time+(e.key==='ArrowLeft'?-5000:5000))));}
    else if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();deleteSelected();}
  };document.addEventListener('keydown',keyboard);
  const unload=()=>{if(s.dirty)persist(s);};window.addEventListener('beforeunload',unload);
  render();showJob();if(s.dirty&&!s.conflict)s.timer=setTimeout(()=>flush(s).catch(e=>notice(e.message,true)),650);
  const last=s.lastExport||localStorage.getItem(key(id)+':job');if(last){job={id:last};poll();}
  let updating=false;
  return {projectId:id,async prepareMaintenance(){player.pause();await flush(s);},async updateProject(next){if(updating||disposed)return;updating=true;project=next;sceneMap.clear();for(const scene of next.scenes)sceneMap.set(scene.id,scene);try{const result=await api(`/projects/${id}/edit`);if(!disposed){s.sources=result.sources;if(!s.dirty&&!s.promise&&!player.playing&&result.revision!==s.revision){s.history=[...s.history,copy(s.doc)].slice(-60);s.doc=result.document;s.revision=result.revision;s.future=[];render();showJob();}else renderInspector(normalize(s.doc));if(result.last_export_id&&result.last_export_id!==s.lastExport&&!['queued','running'].includes(job?.status)){s.lastExport=result.last_export_id;job={id:s.lastExport,announce:true};poll();}}}catch{}finally{updating=false;}},dispose(){disposed=true;document.removeEventListener('keydown',keyboard);fit.dispose();if(s.notify===saveStatus)s.notify=null;clearTimeout(timer);lanes.dispose();player.dispose();window.removeEventListener('beforeunload',unload);if(s.dirty)flush(s).catch(()=>{});}};
}

