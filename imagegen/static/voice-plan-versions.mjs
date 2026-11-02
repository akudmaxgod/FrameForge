function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
function seconds(value){return Number(value.toFixed(3));}
export function voicePlanVersions({project,api,button,refresh}){
 if(!project?.voice_workflow?.options?.versioned_plan)return null;
 const section=node('section');section.className='voice-plan-versions';section.dataset.testid='voice-plan-versions';
 section.append(node('strong','Версии плана по озвучке'),node('p','Читаю сохранённые варианты…'));
 let request=0;
 api(`/projects/${project.id}/voice/plans`).then(async overview=>{
  if(!section.isConnected)return;
  section.replaceChildren(node('strong','Версии плана по озвучке'));
  section.append(node('p','Новый вариант хранится отдельно. Применение проверяет изменения проекта; изображения, видео и монтаж не заменяются.'));
  const select=node('select');select.setAttribute('aria-label','Версия плана по озвучке');
  for(const entry of overview.versions){const option=node('option',`Версия ${entry.revision}${entry.applied?' · применялась':''}`);option.value=entry.id;select.append(option);}
  const viewer=node('div');section.append(select,viewer);
  const show=async id=>{
   const current=++request;viewer.replaceChildren(node('p','Читаю выбранную версию…'));
   try{
    const plan=await api(`/projects/${project.id}/voice/plans/${id}`);
    if(!section.isConnected||current!==request)return;
    const doc=plan.document;viewer.replaceChildren(node('p',`${doc.frames.length} кадров · ${seconds(doc.duration)} сек.`));
    const audio=node('audio');audio.controls=true;audio.preload='metadata';audio.src=`/api/assets/${encodeURIComponent(doc.audio_asset_id)}`;viewer.append(audio);
    viewer.append(node('p','Точные цитаты сохраняются. Приблизительное время отмечено; ручная граница не означает подтверждённое распознавание речи.'));
    if(doc.boundary_audit){const weak=doc.boundary_audit.boundaries.filter(r=>['approximate','unmatched'].includes(r.support_status)).length;
     viewer.append(node('p',`На проверку начала фраз: ${weak}. Точность времени по независимому прослушиванию пока не измерена.`));}
    for(const conflict of doc.conflicts||[])viewer.append(node('p',conflict));
    if(plan.apply_blocked_reason)viewer.append(node('p',plan.apply_blocked_reason));
    if(overview.candidate_id===id&&!(doc.conflicts||[]).length&&!plan.apply_blocked_reason)viewer.append(button(project.voice_workflow.options.auto_render?'Применить план и подготовить изображения':'Применить этот план',async()=>{
     await api(`/projects/${project.id}/voice/plans/${id}/apply`,{});await refresh(true);
    },'primary'));
    else if(id!==overview.active_id&&overview.candidate_id!==id&&!plan.apply_blocked_reason)viewer.append(button('Выбрать этот вариант для применения',async()=>{
     await api(`/projects/${project.id}/voice/plans/${id}/choose`,{});await refresh(true);
    },'secondary'));
    const shots=node('details');shots.append(node('summary','Посмотреть распределение кадров по озвучке'));viewer.append(shots);
    for(let index=0;index<doc.frames.length;index++){
     const frame=doc.frames[index],timing=frame.timing||frame.data.voice_timing,row=node('article');row.className='voice-plan-row';
     row.append(node('strong',`Кадр ${index+1} · ${seconds(timing.start)} — ${seconds(timing.end)} сек.`),node('p',frame.narration_source?.quote||frame.data.source_quote));
     const content=frame.data.frame_focus;
     if(content){
      const status={fact:'Утверждение источника',hypothesis:'Предположение',reported:'Чужое утверждение или слух',negated:'Отрицание',question:'Вопрос',comparison:'Сравнение',conditional:'Условие'};
      row.append(node('strong','Главный предмет: '+content.subject),node('p','Действие: '+content.action),node('p','Участники: '+(content.participants.map(p=>p.name).join(', ')||'нет видимых участников')),node('p',`${content.place} · ${content.period} · ${content.state}`),node('p',status[content.source_status]),node('p',content.visual_direction));
      const evidence=node('details');evidence.append(node('summary','Основания в сценарии'));for(const q of content.evidence)evidence.append(node('blockquote',q.quote));row.append(evidence);
     }
     if(timing.timing_needs_review)row.append(node('small','Есть приблизительное время. Проверьте на слух.'));
     row.append(button('Послушать фрагмент',async()=>{audio.currentTime=timing.start;await audio.play();},'secondary'));
     if(index+1<doc.frames.length&&doc.kind!=='narration-expansion/1'){
      const label=node('label',timing.manual_end?'Ручная граница после кадра, сек.':'Граница после кадра, сек.');label.style.display='block';label.style.marginTop='12px';
      const input=node('input');input.type='number';input.min=0;input.max=doc.duration;input.step=.01;input.value=seconds(timing.end);input.setAttribute('aria-label',`Граница после кадра ${index+1}, секунды`);label.append(input);
      row.append(label,button('Сохранить границу в новом варианте',async()=>{
       if(input.value.trim()===''||!Number.isFinite(Number(input.value)))throw Error('Укажите время в секундах.');
       await api(`/projects/${project.id}/voice/plans/${id}/boundary`,{index:index+1,seconds:Number(input.value)});await refresh(true);
      },'secondary'));
     }
     shots.append(row);
    }
   }catch(error){if(current===request&&section.isConnected)viewer.replaceChildren(node('p','Не удалось прочитать вариант. Текущие сцены сохранены.'));}
  };
  select.onchange=()=>show(select.value);
  const selected=overview.candidate_id||overview.active_id||overview.versions[0]?.id;
  if(selected){select.value=selected;await show(selected);}else{select.remove();viewer.replaceChildren(node('p','Отдельные версии ещё не подготовлены.'));}
  const focusLabel=node('label',' Согласовать содержание каждого кадра с озвучкой'),focus=node('input');focus.type='checkbox';focus.checked=true;focus.dataset.testid='voice-replan-frame-content-focus';focusLabel.prepend(focus);section.append(focusLabel,node('p','Дополнительный разбор сохраняет отдельный вариант для проверки. Готовые изображения и видео сохраняются.'));
  const uploadLabel=node('label','Другая версия озвучки'),upload=node('input');upload.type='file';upload.accept='.mp3,.wav,.m4a,.aac,.ogg,.flac';upload.setAttribute('aria-label','Новая версия озвучки');uploadLabel.style.display='block';uploadLabel.style.marginTop='16px';uploadLabel.append(upload);
  section.append(uploadLabel,button('Подготовить вариант с новой озвучкой',async()=>{
   const file=upload.files?.[0];if(!file)throw Error('Выберите файл озвучки.');if(file.size>100000000)throw Error('Озвучка должна быть не больше 100 МБ.');
   const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('Не удалось прочитать озвучку.'));reader.readAsDataURL(file);});
   const audio=await api(`/projects/${project.id}/edit/audio`,{filename:file.name,data:encoded});
   await api(`/projects/${project.id}/voice/replan`,{project_version:project.version,frame_content_focus:focus.checked,audio_asset_id:audio.asset_id});await refresh(true);
  },'secondary'));
  section.append(button('Подготовить новую версию по текущему тексту и озвучке',async()=>{
   await api(`/projects/${project.id}/voice/replan`,{project_version:project.version,frame_content_focus:focus.checked});await refresh(true);
  },'secondary'));
 }).catch(()=>{if(section.isConnected)section.replaceChildren(node('strong','Версии плана по озвучке'),node('p','Не удалось прочитать версии. Текущий план сохранён.'));});
 return section;
}
