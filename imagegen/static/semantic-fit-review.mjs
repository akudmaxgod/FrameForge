const seconds=n=>(n/1000).toFixed(2)+' с';
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
export function openSemanticReview({proposal,onSave,pause,notice}){
 const mapping=proposal.mapping,bindings=mapping.bindings,voice=mapping.audio_binding;
 const dialog=node('dialog',undefined,'semantic-review-dialog'),header=node('header'),body=node('div',undefined,'semantic-review-body'),footer=node('footer');
 const title=node('div');title.append(node('h2','Проверка смысловых привязок'),node('p','Сравните кадр с распознанной фразой. Оценка сходства — ориентир модели, а не вероятность правильного ответа.'));
 const close=node('button','Закрыть');close.type='button';close.onclick=()=>dialog.close();header.append(title,close);
 const controls=node('div',undefined,'semantic-review-tools'),filter=node('input');filter.type='checkbox';filter.checked=bindings.some(b=>b.needs_review&&!b.reviewed);
 const filterLabel=node('label');filterLabel.append(filter,document.createTextNode('Только требуют проверки'));
 const all=node('button','Подтвердить все привязки после просмотра');all.type='button';
 const summary=node('span'),back=node('button','←'),next=node('button','→'),pageLabel=node('span');back.type=next.type='button';
 controls.append(filterLabel,summary,back,pageLabel,next);const table=node('table');
 let page=0,audio=null,disposed=false;
 const drafts=new Map(bindings.map(b=>[b.clip_id,{accepted:Boolean(b.reviewed),start_ms:b.start_ms,changed:false}]));
 function render(){
  const remaining=bindings.filter(b=>b.needs_review&&!drafts.get(b.clip_id).accepted).length;
  const list=filter.checked?bindings.filter(b=>b.needs_review):bindings;
  page=Math.max(0,Math.min(page,Math.max(0,Math.ceil(list.length/20)-1)));summary.textContent=`Всего ${bindings.length} · ждут подтверждения ${remaining}`;
  back.disabled=page===0;next.disabled=(page+1)*20>=list.length;pageLabel.textContent=`${page+1} / ${Math.max(1,Math.ceil(list.length/20))}`;
  table.replaceChildren();const head=node('tr');for(const label of ['Кадр и промпт','Распознанная речь','Начало на монтаже','Проверка'])head.append(node('th',label));table.append(head);
  for(const binding of list.slice(page*20,page*20+20)){
   const index=bindings.indexOf(binding),draft=drafts.get(binding.clip_id),clip=proposal.document.clips.find(c=>c.edit_clip_id===binding.clip_id);
   const tr=node('tr',undefined,binding.needs_review?'semantic-review-needed':''),visual=node('td'),phrase=node('td'),timing=node('td'),approval=node('td');
   visual.append(node('strong','Клип '+(index+1)));
   if(clip?.source_image_asset_id){const img=node('img');img.src='/api/assets/'+clip.source_image_asset_id;img.loading='lazy';img.alt='Клип '+(index+1);visual.append(img);}
   const description=node('details');description.append(node('summary','Промпт кадра'),node('p',binding.text));visual.append(description);
   phrase.append(node('p',binding.narration),node('small',`Исходное сходство: ${binding.similarity.toFixed(2)}${binding.manual_boundary?' · ручная граница':''}`));
   const listen=node('button','Прослушать');listen.type='button';listen.onclick=()=>{
    pause();audio?.pause();audio=new Audio('/api/assets/'+voice.asset_id);
    const start=voice.trim_in_ms+Math.max(0,draft.start_ms-voice.offset_ms),end=voice.trim_in_ms+((index+1<bindings.length?drafts.get(bindings[index+1].clip_id).start_ms:binding.end_ms)-voice.offset_ms);
    audio.onloadedmetadata=()=>{audio.currentTime=start/1000;audio.play().catch(e=>notice(e.message,true));};audio.ontimeupdate=()=>{if(audio.currentTime*1000>=end)audio.pause();};
   };phrase.append(listen);
   const input=node('input');input.type='number';input.min='0';input.step='.01';input.value=(draft.start_ms/1000).toFixed(2);input.disabled=index===0;input.setAttribute('aria-label','Начало клипа '+(index+1)+', с');
   input.onchange=()=>{draft.start_ms=Number(input.value)*1000;draft.changed=true;draft.accepted=false;render();};
   const end=index+1<bindings.length?drafts.get(bindings[index+1].clip_id).start_ms:binding.end_ms;timing.append(input,node('small','Конец: '+seconds(end)));
   const label=node('label'),check=node('input');check.type='checkbox';check.checked=draft.accepted;check.setAttribute('aria-label','Подтвердить привязку клипа '+(index+1));check.onchange=()=>{draft.accepted=check.checked;render();};label.append(check,document.createTextNode('Проверено'));approval.append(label,node('p',binding.needs_review?binding.reasons.join('. '):'Сильное совпадение с речью'));
   tr.append(visual,phrase,timing,approval);table.append(tr);
  }
 }
 filter.onchange=()=>{page=0;render();};back.onclick=()=>{page--;render();};next.onclick=()=>{page++;render();};all.onclick=()=>{for(const d of drafts.values())d.accepted=true;render();};
 const error=node('p',undefined,'semantic-review-error');error.setAttribute('role','alert');
 const save=node('button','Сохранить проверку привязок');save.type='button';save.className='primary';save.onclick=async()=>{
  save.disabled=true;error.textContent='';audio?.pause();
  try{await onSave(bindings.map(b=>{const d=drafts.get(b.clip_id);return {clip_id:b.clip_id,accepted:d.accepted,...(d.changed?{start_ms:d.start_ms}:{})};}));if(!disposed)dialog.close();}
  catch(e){error.textContent=e.message;}finally{save.disabled=false;}
 };
 footer.append(all,save);body.append(controls,table,error);dialog.append(header,body,footer);document.body.append(dialog);
 dialog.addEventListener('close',()=>{audio?.pause();dialog.remove();disposed=true;});render();dialog.showModal();
 return {dispose(){disposed=true;audio?.pause();dialog.remove();}};
}
