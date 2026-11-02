const labels={checked:'Проверено',waiting_generation:'Ждёт изображения',waiting_registry:'Ждёт подтверждения бесплатной квоты',waiting_model:'Ждёт доступной модели',waiting_quota:'Ждёт восстановления квоты',queued:'В очереди проверки',running:'Проверяется',retry_wait:'Повтор проверки позже',requires_correction:'Требует исправления',semantic_uncertainty:'Не удалось уверенно проверить',unverified_legacy:'Нет подтверждённой проверки',user_exception:'Оставлен по решению пользователя',stale_review:'Данные изменились',error_response:'Некорректный ответ проверки',error_access:'Проверка недоступна',transport_unknown:'Исход отправки не подтверждён',rejected_by_user:'Отклонён пользователем'};
Object.assign(labels,{error_request:'Ошибка запроса проверки',budget_too_small:'Запрос превышает разрешённую квоту',cancelled:'Проверка завершена без результата',waiting_prompt:'Ожидает проверки промпта',blocked_prompt:'Промпт требует внимания',fail:'Обнаружено противоречие',uncertain:'Не удалось уверенно проверить'});
Object.assign(labels,{manual_approved:'Утверждён вручную',needs_manual_review:'Ожидает ручного утверждения'});
const cancelledVisualCategories=new Set(['source_image','source_reference','source_prompt']);
function node(tag,text){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value;}
export function qualityControls({project,api,button,refresh,enabled}){
 if(!project||!enabled)return null;
 const section=node('section');section.className='control-section quality-controls';section.dataset.testid='quality-controls';
 section.append(node('strong','Проверки и исключения'),node('p','Загружаю сохранённое состояние…'));
 Promise.all([api(`/projects/${project.id}/google-review-summary`),api('/google-queue'),api(`/projects/${project.id}/google-queue/recovery`)]).then(([summary,quota,recovery])=>{
   if(!section.isConnected)return;section.replaceChildren(node('strong','Проверки и исключения'));
   if(!summary.enabled){section.append(node('p','Новая бесплатная очередь выключена.'));return;}
   const manual=summary.visual_review_mode==='manual';
   if(manual)section.append(node('p','Изображения выбираются и утверждаются вручную. Автоматическая оценка кадров, эталонов и конечных промптов отключена. Проверка разбора сценария сохранена.'));
   const counts=Object.entries(summary.counts||{}).map(([state,count])=>`${labels[state]||'Требует внимания'}: ${count}`);
   section.append(node('p',counts.join(' · ')||'Изображений пока нет.'));
   const promptWaits=manual?[]:(summary.prompt_waits||[]);
   const waits=(summary.analysis_waits?.length||0)+promptWaits.length;
   if(waits)section.append(node('p',manual?`Разборов, ожидающих проверки: ${waits}. Создание кадров по ним ещё недоступно.`:`План или промпт ожидают проверки: ${waits}. Генерация этих кадров удерживается.`));
   if(summary.batch_waits?.length)section.append(node('p',`Пакетов на паузе или в ожидании: ${summary.batch_waits.length}.`));
   if(recovery.paused)section.append(node('p','Проверки этого проекта приостановлены. Сохранённые этапы ждут продолжения.'));
   const stageWaits=[...(summary.analysis_waits||[]).map(item=>({...item,title:'Разбор сценария'})),
     ...promptWaits.map(item=>({...item,title:`Промпт кадра ${item.position??'—'}`}))];
   for(const item of stageWaits){
     const entry=node('details');entry.className='queue-recovery';entry.dataset.testid=`stage-wait-${item.generation_id}`;
     entry.append(node('summary',`${item.title}: ${labels[item.state]||'Требует внимания'}`),node('p',item.reason||'Сохранённое задание ждёт результата проверки.'));
     if(item.next_retry_at)entry.append(node('p',`Следующая проверка: ${new Date(item.next_retry_at*1000).toLocaleString()}`));
     entry.append(node('p','Повтор проверки выполняется в разделе восстановления ниже. Если исходные данные изменились, отмените это задание и подготовьте новое.'));
     let pending=false;const message=node('p');entry.append(message);
     const cancel=button('Отменить сохранённое задание',async()=>{
       if(pending)return;pending=true;cancel.disabled=true;
       try{await api(`/generations/${item.generation_id}/cancel`,{});await refresh(true);}
       catch(error){message.textContent=error.message||'Не удалось отменить задание.';}
       finally{pending=false;cancel.disabled=false;}
     },'secondary');entry.append(cancel);section.append(entry);
   }
   for(const item of (manual?[]:(summary.reference_checks||[])).filter(item=>item.state!=='checked'))section.append(node('p',`Эталон «${item.anchor}»: ${labels[item.state]||'Требует внимания'}`));
   for(const item of (summary.exceptions||[]).filter(item=>item.asset_id)){
     const entry=node('details');entry.append(node('summary',`Кадр ${item.position}: ${labels[item.state]||'Требует внимания'}`));
     const image=node('img');image.src=`/api/assets/${encodeURIComponent(item.asset_id)}`;image.alt=`Кадр ${item.position}`;image.style.maxWidth='100%';image.loading='lazy';
     const reason=manual?(item.manual_note||(item.state==='rejected_by_user'?'Вариант отклонён вручную.':'Просмотрите вариант и утвердите его в карточке кадра.')):(item.reason||item.manual_note||'Требуется проверка.');
     entry.append(image,node('blockquote',item.source_quote||'Цитата не сохранена.'),node('p',reason));
     if(!manual&&item.next_retry_at)entry.append(node('p',`Следующая проверка: ${new Date(item.next_retry_at*1000).toLocaleString()}`));
     section.append(entry);
   }
   const categoryLabels={source_image:'изображение',source_prompt:'конечный промпт',source_reference:'эталон',source_plan:'план сцен'};
   for(const job of recovery.jobs||[]){
     const cancelledVisual=manual&&cancelledVisualCategories.has(job.category);
     const entry=node('details');entry.className='queue-recovery';entry.dataset.testid=`check-recovery-${job.id}`;
     entry.append(node('summary',`Восстановить проверку: ${categoryLabels[job.category]||'смысл'} — ${labels[job.state]||'Требует внимания'}`));
     entry.append(node('p',`Отправок этой проверки: ${job.attempts} из 3. Сохранённые изображения остаются на месте.`));
     if(cancelledVisual)entry.append(node('p','Эта автоматическая проверка отменена. Можно завершить сохранённую запись без результата; кадр утверждается вручную.'));
     if(job.state==='transport_unknown')entry.append(node('p','Сервис мог получить запрос. Повтор заблокирован; можно только завершить эту проверку без подтверждённого результата.'));
     const label=node('label','Причина решения');const note=node('textarea');label.append(note);entry.append(label);
     const message=node('p');entry.append(message);let pending=false;
     const action=(name,value)=>{
       const control=button(name,async()=>{
         if(pending)return;if(note.value.trim().length<5){message.textContent='Укажите причину: не менее 5 символов.';return;}
         pending=true;control.disabled=true;
         try{await api(`/projects/${project.id}/google-queue/recovery`,{job_id:job.id,expected_state:job.state,action:value,note:note.value});message.textContent=value==='retry_check'?'В очередь добавлена только проверка.':'Проверка завершена без подтверждённого результата.';await refresh(true);}
         catch(error){message.textContent=error.message||'Не удалось сохранить решение. Обновите список.';}
         finally{pending=false;control.disabled=false;}
       },'secondary');entry.append(control);
     };
     if(!cancelledVisual&&job.state!=='transport_unknown'&&job.attempts<3)action('Повторить только проверку','retry_check');
     action('Завершить без результата','abandon_check');section.append(entry);
   }
   section.append(node('small',`Зарезервировано запросов приложением: ${quota.reserved_requests||0}. Внешний расход неизвестен; точный остаток Google не подтверждён. Кредиты Flow учитываются отдельно.`));
   section.append(button(recovery.paused?'Продолжить проверки':'Приостановить проверки',async()=>{await api(`/projects/${project.id}/google-queue/pause`,{paused:!recovery.paused});await refresh(true);},'secondary'));
 }).catch(()=>{if(section.isConnected)section.replaceChildren(node('strong','Проверки и исключения'),node('p','Не удалось прочитать состояние. Сохранённые задания не изменены.'));});
 return section;
}
