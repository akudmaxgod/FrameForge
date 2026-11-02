const fields={material:'Материал',shape:'Форма',scale:'Размер',era:'Эпоха',origin:'Происхождение',distinctive_features:'Отличительные признаки'};
const states={bound:'Версия закреплена',stale:'Сценарий изменился — проверьте привязку',conflict:'Есть противоречие со сценарием'};
function node(tag,text){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value;}
function evidence(entry){
 const doc=entry.document,box=node('div');
 box.append(node('p',`Источник: ${doc.origin?.project_title||'Исходный проект'}`));
 const quotes=doc.evidence_spans?.length?doc.evidence_spans:(doc.evidence?[doc.evidence]:[]);
 if(!quotes.length)box.append(node('p','Точное текстовое основание не сохранено.'));
 if(doc.identity_scope==='source_type')box.append(node('p','Тип конструкции из сценария. Упоминания могут описывать разные объекты; их размеры и детали не объединяются в один объект.'));
 for(const quote of quotes)box.append(node('blockquote',quote.quote));
 for(const [key,label] of Object.entries(fields)){
  const prop=doc.properties?.[key];
  box.append(node('small',`${label}: ${prop?.value||(prop?.status==='multiple_source_claims'?'Несколько сведений — смотрите цитаты':'Не подтверждено')}`));
 }
 for(const check of doc.historical_checks||[]){
  const state={supported:'Подтверждено источником',contradicted:'Есть противоречие',uncertain:'Не подтверждено'}[check.verdict]||'Требует проверки';
  box.append(node('p',`${state}: ${check.claim}`));
  for(const source of check.sources||[]){
   const link=node('a',source.title);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';box.append(link,node('p',source.reason));
  }
 }
 box.append(node('p','Это сведения исходного сценария. Историческая достоверность проверяется отдельно; изображения выбирайте и утверждайте вручную.'));
 return box;
}
export function catalogControls({project,api,button,refresh,enabled}){
 if(!project||!enabled)return null;
 const section=node('section');section.className='control-section';section.dataset.testid='catalog-controls';
 section.append(node('strong','Общий каталог'),node('p','Загружаю сохранённые версии…'));
 api(`/projects/${project.id}/source-catalog`).then(view=>{
  if(!section.isConnected)return;
  section.replaceChildren(node('strong','Общий каталог'));
  section.append(node('p','Сохраняйте сведения с их источником. В каждом проекте используется выбранная версия; новые версии не заменяют её автоматически.'));
  if((view.primary_sources||[]).length){
   const gallery=node('details');gallery.dataset.testid='primary-sources';gallery.append(node('summary','Источники и фотографии'));
   gallery.append(node('p','Эти материалы помогают выбрать иллюстрацию. Фотография показывает видимый объект; она не подтверждает все слова сценария и не утверждает будущий кадр.'));
   for(const source of view.primary_sources){
    const item=node('div');item.append(node('strong',source.title));
    const state={source_changed:'Сценарий изменился — пересмотрите применимость источника',file_changed:'Файл изменился или отсутствует — прежняя сверка недействительна'}[source.state];
    if(state)item.append(node('p',state));
    if(source.state==='available'&&source.url){
     const link=node('a',source.scope==='document_context'?'Открыть документ':'Открыть фотографию');link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';
     if(source.scope==='architecture_context'){
      const image=node('img');image.src=source.url;image.alt=source.title;image.loading='lazy';image.style.maxWidth='240px';image.style.width='100%';image.style.height='auto';image.style.display='block';link.append(image);
     }
     item.append(link);
    }
    for(const feature of source.features||[])item.append(node('p',feature));
    item.append(node('p',source.limits));
    const original=node('a',`Источник: ${source.publisher}`);original.href=source.source_url;original.target='_blank';original.rel='noopener noreferrer';item.append(original);
    gallery.append(item);
   }
   section.append(gallery);
  }
  section.append(button('Сохранить сведения этого сценария',async()=>{
   await api(`/projects/${project.id}/source-catalog`,{project_version:view.project_version});await refresh(true);
  },'secondary'));
  for(const entry of view.bindings||[]){
   const details=node('details');details.append(node('summary',`${entry.document.name} · версия ${entry.revision} · ${states[entry.state]||'Требует внимания'}`),evidence(entry));
   for(const conflict of entry.conflicts||[])details.append(node('p',conflict));
   if(entry.latest_revision>entry.revision)details.append(button(`Применить версию ${entry.latest_revision}`,async()=>{
    await api(`/projects/${project.id}/source-catalog/bind`,{anchor_id:entry.anchor_id,entry_id:entry.id,revision:entry.latest_revision,expected_binding:entry.binding_token});await refresh(true);
   },'secondary'));
   const label=node('label','Заметка к новой версии (не подтверждение факта)'),note=node('textarea');note.value=entry.document.description_unverified||'';label.style.display='block';label.style.marginTop='12px';label.append(note);
   details.append(label,button('Сохранить заметку в новой версии',async()=>{
    await api(`/source-catalog/${entry.id}/revise`,{expected_revision:entry.revision,changes:{description_unverified:note.value}});await refresh(true);
   },'secondary'));
   section.append(details);
  }
  if(!(view.bindings||[]).length)section.append(node('p','В этом проекте ещё нет сохранённых привязок.'));
  for(const candidate of view.candidates||[]){
   const details=node('details');details.append(node('summary',`В другом проекте: ${candidate.document.name} · версия ${candidate.revision}`),evidence(candidate));
   for(const conflict of candidate.conflicts||[])details.append(node('p',conflict));
   if(!(candidate.conflicts||[]).length){
    const existing=(view.bindings||[]).find(entry=>entry.anchor_id===candidate.anchor_id);
    details.append(button('Использовать эту версию',async()=>{
     await api(`/projects/${project.id}/source-catalog/bind`,{anchor_id:candidate.anchor_id,entry_id:candidate.id,revision:candidate.revision,expected_binding:existing?.binding_token||null});await refresh(true);
    },'secondary'));
   }
   section.append(details);
  }
 }).catch(()=>{if(section.isConnected)section.replaceChildren(node('strong','Общий каталог'),node('p','Не удалось прочитать каталог. Сохранённые версии не изменены.'));});
 return section;
}
