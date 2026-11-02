import {MontagePlayer} from './montage-player.mjs';
import {openSemanticReview} from './semantic-fit-review.mjs';
const key=id=>'frameforge-voice-fit:'+id;
function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
const seconds=ms=>(ms/1000).toFixed(2)+' с';
export function mountVoiceFit(host,{id,api,flush,getState,accept,pause,notice,importedPrompts=false}){
 const panel=node('section');panel.className='montage-fit-preview';panel.hidden=true;host.append(panel);
 let job=null,timer,disposed=false,previewPlayer,semanticReview;
 function button(label,action){const b=node('button',label);b.type='button';b.onclick=()=>Promise.resolve().then(action).catch(e=>notice(e.message,true));return b;}
 const select=node('select');select.setAttribute('aria-label','Если видео короче фразы');
 const mode=node('select');mode.setAttribute('aria-label','Способ подгонки к озвучке');
 for(const [value,label] of [['semantic','По смыслу · готовые промпты и речь'],['duration_freeze','По длительности · со стоп-кадрами'],['phrases','По фразам · нужны цитаты сценария']]){const o=node('option',label);o.value=value;mode.append(o);}mode.value=importedPrompts?'semantic':'phrases';
 mode.onchange=()=>{select.hidden=mode.value!=='phrases';start.textContent=mode.value==='semantic'?'Сопоставить сцены с озвучкой':mode.value==='duration_freeze'?'Подогнать со стоп-кадрами':'Подогнать монтаж под фразы';};
 for(const [value,label] of [['hold','Удерживать последний кадр'],['speed','Снизить скорость до 0.8×'],['error','Показать конфликт']]){const o=node('option',label);o.value=value;select.append(o);}
 const start=button('Подогнать монтаж под озвучку',async()=>{
  await flush();pause();const state=getState();
  job=await api(`/projects/${id}/edit/voice-fit`,{expected_revision:state.revision,short_video_policy:select.value,timing_mode:mode.value});
  localStorage.setItem(key(id),job.id);render();if(['queued','recognizing'].includes(job.status))poll();
 });
 const tools=node('div');tools.className='montage-toolbar';tools.append(mode,select,start);host.prepend(tools);mode.onchange();
 function render(){
  semanticReview?.dispose();semanticReview=null;previewPlayer?.dispose();previewPlayer=null;panel.replaceChildren();panel.hidden=!job;if(!job)return;
  const byDuration=job.timing_mode==='duration_freeze'||job.proposal?.method==='duration_freeze';
  const semantic=job.timing_mode==='semantic'||job.proposal?.method==='semantic';
  panel.append(node('h3',semantic?'Смысловая привязка готовых сцен':byDuration?'Подгонка стоп-кадрами под длину озвучки':'Подгонка под фразы озвучки'),node('p',job.stage));
  if(job.error)panel.append(node('p',job.error));
  if(['queued','recognizing'].includes(job.status)){panel.append(node('p',`${job.progress||0}% · Расчёт выполняется на этом ПК.`),button('Отменить расчёт',async()=>{job=await api(`/projects/${id}/edit/voice-fit/${job.id}/cancel`,{});render();}));return;}
  const proposal=job.proposal;
  if(!proposal)return;
  if(job.status==='applied'){
   panel.append(node('p','Длительности применены. Исходные файлы сохранены.'),button('Откатить подгонку',async()=>{
    await flush();const state=getState(),result=await api(`/projects/${id}/edit/voice-fit/${job.id}/rollback`,{expected_revision:state.revision});accept(result);job=await api(`/projects/${id}/edit/voice-fit/${job.id}`);render();
   }));return;
  }
  if(job.status==='rolled_back'){panel.append(node('p','Монтаж возвращён к варианту до подгонки.'));return;}
  for(const issue of proposal.conflicts.filter(x=>!semantic||x.code!=='semantic_review')){const p=node('p',issue.message);p.className='montage-warning';panel.append(p);}
  for(const warning of proposal.warnings)panel.append(node('p',warning.message));
  if(semantic){
   const rows=proposal.mapping.bindings,waiting=rows.filter(b=>b.needs_review&&!b.reviewed).length;
   panel.append(node('p',`${rows.length} привязок · требуют проверки: ${waiting}`),node('p','Промпты сохранены. Привязки построены по распознанной речи; неоднозначные соответствия нужно подтвердить.'));
   panel.append(button('Проверить смысловые привязки',()=>{semanticReview=openSemanticReview({proposal,pause,notice,onSave:async decisions=>{
    await flush();job=await api(`/projects/${id}/edit/voice-fit/${job.id}/review`,{expected_review_revision:proposal.mapping.review_revision||0,decisions});render();notice('Проверка привязок сохранена. Применение монтажа — отдельной кнопкой.');
   }});}));
  }else{
  const table=node('table'),head=node('tr');for(const label of [byDuration?'Клип':'Фраза','Сейчас','После подгонки','Стоп-кадр'])head.append(node('th',label));table.append(head);
  for(const c of proposal.changes){const row=node('tr');row.append(node('td',c.label||c.quote),node('td',`${seconds(c.old_start_ms)} — ${seconds(c.old_end_ms)}`),node('td',`${seconds(c.new_start_ms)} — ${seconds(c.new_end_ms)}`),node('td',seconds(c.hold_last_ms||0)));table.append(row);}panel.append(table);
  }
  panel.append(node('p','Предпросмотр не меняет сохранённый монтаж. Речь воспроизводится в исходной скорости.'));
  if(proposal.can_apply){
   const stage=node('div');stage.className='montage-stage';const clock=node('p','0.00 с'),seek=node('input');seek.type='range';seek.min=0;seek.step=1;seek.max=proposal.timeline.duration_ms;seek.setAttribute('aria-label','Предпросмотр подгонки');
   previewPlayer=new MontagePlayer(stage,(time)=>{clock.textContent=seconds(time);seek.value=time;},e=>notice(e.message,true));previewPlayer.setDocument(proposal.document);
   seek.oninput=()=>previewPlayer.seek(Number(seek.value));panel.append(stage,clock,seek,button('Прослушать вариант',()=>{pause();return previewPlayer.playing?previewPlayer.pause():previewPlayer.play();}),button('Применить подгонку',async()=>{
    previewPlayer.pause();await flush();const state=getState();const result=await api(`/projects/${id}/edit/voice-fit/${job.id}/apply`,{expected_revision:state.revision});accept(result);job=await api(`/projects/${id}/edit/voice-fit/${job.id}`);render();notice(semantic?'Смысловые привязки применены к монтажу. Доступен откат.':byDuration?'Добавлены стоп-кадры. Монтаж покрывает озвучку целиком. Доступен откат.':'Монтаж подогнан под фразы. Доступен откат.');
   }));
  }else panel.append(node('p',semantic?'Подтвердите спорные привязки в окне проверки. После сохранения проверки можно будет применить вариант.':'Применение недоступно: устраните указанные конфликты и создайте новый предпросмотр.'));
 }
 async function poll(){clearTimeout(timer);if(disposed||!job)return;try{job=await api(`/projects/${id}/edit/voice-fit/${job.id}`);if(disposed)return;render();if(['queued','recognizing'].includes(job.status))timer=setTimeout(poll,1000);}catch(e){notice(e.message,true);}}
 const previous=localStorage.getItem(key(id));if(previous){job={id:previous};poll();}
 return {dispose(){disposed=true;clearTimeout(timer);semanticReview?.dispose();previewPlayer?.dispose();panel.remove();tools.remove();}};
}
