const relationLabels={explicit_cause:'Названная причина',explicit_consequence:'Названное следствие',purpose:'Цель',condition:'Условие',temporal_limit:'Граница периода'};
const modalityLabels={reported:'Чужое утверждение',hypothesis:'Предположение',conditional:'Условие',purpose:'Цель',capability:'Возможность',denied_event:'Отрицание события'};
function node(tag,text){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;}
export function passportControls({project,api,enabled}){
 if(!project||!enabled)return null;
 const panel=node('details');panel.className='control-section source-passport';panel.dataset.testid='source-passport';
 const content=node('div');panel.append(node('summary','События, участники и причины'),content);let loaded=false,pending=false;
 panel.addEventListener('toggle',async()=>{
  if(!panel.open||loaded||pending)return;pending=true;
  content.replaceChildren(node('p','Читаю паспорт сценария…'));
  try{
   const packet=await api(`/projects/${project.id}/source-passport`);if(!panel.isConnected)return;
   loaded=true;content.replaceChildren();const ledger=packet.narrative;
   if(!ledger){content.append(node('p','Цепочка событий пока не подготовлена.'));return;}
   content.append(node('p',`Событий: ${ledger.events.length}. Явных связей: ${ledger.relationships.length}. Полнота смыслового разбора ещё требует проверки.`));
   if(!packet.coverage.ok)content.append(node('p','В привязке кадров есть пропуски или пересечения.'));
   content.append(node('small','Цитата подтверждает содержание сценария. Историческая достоверность проверяется отдельно. Предложения, цели и условия не утверждают, что событие произошло.'));
   for(const event of ledger.events){
    const entry=node('details');entry.dataset.eventId=event.id;
    const scenes=ledger.scene_mapping.filter(row=>row.event_ids.includes(event.id));
    entry.append(node('summary',event.source.quote));entry.append(node('blockquote',event.source.quote));
    for(const [field,label] of [['subjects','Предмет рассказа'],['participants','Люди'],['animals','Животные']]){
     const values=event[field]||[];if(values.length)entry.append(node('p',`${label}: ${[...new Set(values.map(item=>item.name))].join(', ')}`));
    }
    if(event.actions?.length)entry.append(node('p',`Действия: ${event.actions.map(item=>item.quote).join(', ')}`));
    entry.append(node('p',`Время: ${event.time?.evidence?.quote||'точное основание не найдено'}`));
    if(event.place)entry.append(node('p',`Место: ${event.place.evidence.quote}`));
    if(scenes.length)entry.append(node('p',`Связанные кадры: ${scenes.map(row=>project.scenes?.find(s=>s.id===row.scene_id)?.position||'—').join(', ')}`));
    for(const item of event.qualifications||[])entry.append(node('p',`${modalityLabels[item.kind]||'Оговорка'}: «${item.evidence.quote}»`));
    for(const reason of event.review_reasons||[])entry.append(node('p',reason));
    for(const relation of ledger.relationships.filter(row=>row.to_event_id===event.id)){
     const line=node('p',`${relationLabels[relation.kind]||'Связь'}: «${relation.from_source.quote}» → «${relation.to_source.quote}». Основание: «${relation.marker.quote}».`);
     if(relation.polarity==='denied')line.append(node('strong',' Эта причина отрицается.'));
     if(relation.state==='unresolved')line.append(node('strong',' Границы связи требуют уточнения.'));
     entry.append(line);
    }
    content.append(entry);
   }
  }catch{content.replaceChildren(node('p','Не удалось прочитать паспорт. Закройте и откройте раздел для повторной попытки.'));}
  finally{pending=false;}
 });
 return panel;
}
