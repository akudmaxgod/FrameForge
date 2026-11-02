import {compositionControls} from './composition-controls.mjs';
const modes={literal:'Событие из текста',illustrative:'Иллюстрация возможной функции или гипотезы',reported_insert:'Нейтральная вставка о чужом утверждении',neutral_detail:'Предмет или деталь без разыгрывания события',selected_clause:'Один фрагмент с отдельным временем'};
const layers={documented_event:'Событие с указанной датой',historical_reconstruction:'Историческая реконструкция',modern_ruins:'Современное состояние',excavation:'Раскопки',museum_display:'Музей'};
function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
function field(box,title,control){const label=node('label',title);label.className='editorial-field';control.setAttribute('aria-label',title);label.append(control);box.append(label);return control;}
function select(values,empty){const n=node('select');if(empty){const o=node('option',empty);o.value='';n.append(o);}for(const [value,title] of Object.entries(values)){const o=node('option',title);o.value=value;n.append(o);}return n;}
function selectedSpan(input,span){
 const a=input.selectionStart,b=input.selectionEnd;if(a===b)throw Error('Выделите нужные слова в цитате.');
 // Python stores code-point offsets; the browser selects UTF-16 positions and
 // normalizes CRLF in textareas. Map both without changing the source quote.
 const chars=Array.from(span.quote),offsets=new Map([[0,0]]);let visible=0;
 for(let i=0;i<chars.length;i++){const c=chars[i];if(c==='\r'&&chars[i+1]==='\n')i++;visible+=c==='\r'?1:c.length;offsets.set(visible,i+1);}
 if(input.value!==span.quote.replace(/\r\n?/g,'\n')||!offsets.has(a)||!offsets.has(b))throw Error('Выберите целые символы исходной цитаты.');
 const start=offsets.get(a),end=offsets.get(b);return {start:span.start+start,end:span.start+end,quote:chars.slice(start,end).join('')};
}
function preview(plan){
 const d=plan.document,box=node('div');box.className='editorial-preview';
 box.append(node('strong',`Вариант ${plan.revision}`),node('p',modes[d.mode]),node('blockquote',d.focus.quote),node('p',`Главный предмет: ${d.subject.quote}`),node('p',`Время: ${layers[d.time_layer]}${d.time_evidence?' · '+d.time_evidence.quote:''}`));
 box.append(node('p','Люди: '+(d.participants.map(p=>p.name).join(', ')||'не выбраны')),node('p','Животные: '+(d.animals.map(p=>p.name).join(', ')||'не выбраны')),node('p',d.reason));
 for(const c of d.source_historical_checks||[])box.append(node('p',`${d.omit_claims.includes(c.claim)?'Не изображать; достоверность не подтверждена':'Требует отдельного решения'}: ${c.claim}`));
 const remaining=(d.original_semantic_review?.findings||[]).filter(f=>!d.resolved_codes.includes(f.code));
 for(const f of remaining)box.append(node('p','Остаётся на проверке: '+f.reason));
 box.append(node('p','Полная озвучиваемая цитата сохраняется. Проверка исторических сведений и готового изображения выполняется отдельно.'));
 if(plan.apply_blocked_reason)box.append(node('p',plan.apply_blocked_reason));
 return box;
}
export function editorialControls({scene,enabled,api,button,refresh}){
 if(!enabled&&!scene.data?.frame_focus)return null;
 const section=node('details');section.className='editorial-controls';section.dataset.testid=`editorial-controls-${scene.position}`;
 section.append(node('summary','Смысл кадра и точная цитата'));let loaded=false,request=0;
 const focus=scene.data?.frame_focus;
 if(focus){
  const info=node('section');info.dataset.testid=`frame-focus-${scene.position}`;
  const statuses={fact:'Утверждение источника',hypothesis:'Предположение',reported:'Чужое утверждение или слух',negated:'Отрицание',question:'Вопрос',comparison:'Сравнение',conditional:'Условие'};
  const roles={actor:'действует',observer:'наблюдает',listener:'слушает'};
  info.append(node('strong',focus.subject),node('blockquote',focus.narration.quote),node('p','Действие: '+focus.action));
  info.append(node('p','Участники: '+(focus.participants.map(p=>`${p.name} — ${roles[p.role]||p.role}`).join('; ')||'нет видимых участников')));
  info.append(node('p',`Место: ${focus.place}. Время: ${focus.period}.`),node('p','Состояние: '+focus.state),node('p',statuses[focus.source_status]||focus.source_status));
  const clock=focus.timing;
  const event=`Эпизод ${focus.parent_episode.ordinal}: ${focus.parent_episode.title}`;
  info.append(node('small',Number.isFinite(clock?.start)&&Number.isFinite(clock?.end)
   ?`Озвучка: ${clock.start.toFixed(2)}–${clock.end.toFixed(2)} сек. ${event}`
   :`${event}. Время появится после добавления озвучки.`));
  const evidence=node('details');evidence.append(node('summary','Основания в сценарии'));
  for(const quote of focus.evidence)evidence.append(node('blockquote',quote.quote));
  info.append(evidence);section.append(info);
  info.append(compositionControls({scene,api,button,refresh}));
  for(const advice of scene.data?.source_review_advice||[]){
   const note=node('details');note.append(node('summary','Редакторский совет · необязательно'),node('p',advice.message));
   for(const q of advice.evidence||[])note.append(node('blockquote',q.quote));info.append(note);
  }
  const uncertain=(scene.data?.source_context_review?.uncertainties||[]).filter(u=>u.status==='unresolved_safe');
  if(uncertain.length){
   const notice=node('div');notice.dataset.testid=`frame-context-uncertainty-${scene.position}`;
   notice.append(node('strong','Личность участника не уточнена'));
   for(const quote of new Set(uncertain.map(u=>u.source.quote)))notice.append(node('p',`«${quote}»: источник не позволяет уверенно выбрать имя. Требуется проверка привязки участника.`));
   info.append(notice);
  }
 }
 if(!enabled)return section;
 section.addEventListener('toggle',()=>{if(section.open&&!loaded){loaded=true;load();}});
 async function load(){
  const holder=node('div');section.append(holder);holder.append(node('p','Читаю основания кадра…'));
  try{
   let choices=await api(`/scenes/${scene.id}/editorial-plans/choices`);
   if(!section.isConnected)return;
   holder.replaceChildren();holder.append(node('p','Выберите допустимое изображение. Подсказка требует вашего решения; она не утверждает изображение автоматически.'));
   holder.append(node('strong','Полная цитата озвучки'),node('blockquote',choices.source.quote));
   for(const f of choices.semantic_review.findings)holder.append(node('p',f.reason));
   if(choices.binding_error)holder.append(node('p',choices.binding_error));
   const current=choices.current_decision;
   if(current)holder.append(node('p','Применённое решение: '+modes[current.mode]),node('blockquote',current.focus.quote));
   const context=node('textarea');context.readOnly=true;context.rows=5;context.value=choices.context.quote;
   field(holder,'Текущая цитата и предшествующий контекст',context);
   const form=node('div'),viewer=node('div');holder.append(form,viewer);
   let focus=choices.focus,subject=null,plan=null,epoch=0;
   function invalidate(){epoch++;plan=null;viewer.replaceChildren();}
   function renderForm(){
    invalidate();form.replaceChildren();subject=null;
    const focusText=node('textarea');focusText.readOnly=true;focusText.rows=3;focusText.value=focus.quote;
    field(form,'Выбранный фрагмент для изображения',focusText);
    const subjectText=node('p','Главный предмет ещё не выбран');form.append(subjectText);
    form.append(button('Выбрать выделенные слова главным предметом',async()=>{subject=selectedSpan(focusText,focus);invalidate();subjectText.textContent='Главный предмет: '+subject.quote;},'secondary'));
    if(choices.subjects.length){
     const s=select(Object.fromEntries(choices.subjects.map((v,i)=>[String(i),v.quote])),'Выберите предмет из текста');field(form,'Предмет из разбора текста',s);
     s.onchange=()=>{subject=s.value===''?null:choices.subjects[Number(s.value)];invalidate();subjectText.textContent=subject?'Главный предмет: '+subject.quote:'Главный предмет ещё не выбран';};
    }
    const mode=field(form,'Способ изображения',select(modes));mode.value=choices.suggested_mode;
    const layer=field(form,'Временное состояние',select(layers,'Выберите время'));layer.value=choices.suggested_time_layer||'';
    const time=field(form,'Основание времени из сценария',select(Object.fromEntries(choices.time_evidence.map((v,i)=>[String(i),`${v.quote} · позиция ${v.start}`])),'Без указанной даты'));
    const checked={roles:[],animals:[],omit_claims:[]};
    for(const [key,title,entries] of [['roles','Люди в кадре',choices.roles],['animals','Животные в кадре',choices.animals],['omit_claims','Не изображать спорные свойства',choices.historical_checks]]){
     form.append(node('strong',title));
     if(!entries.length)form.append(node('p',key==='omit_claims'?'Такие свойства не отмечены.':'Явные участники не найдены в выбранном фрагменте.'));
     for(const e of entries){const input=node('input');input.type='checkbox';input.checked=key!=='omit_claims';const value=e.name||e.claim;
      const label=node('label',value);label.className='editorial-check';label.prepend(input);form.append(label);checked[key].push({input,value});input.onchange=invalidate;
      if(e.evidence)form.append(node('small','Основание: '+e.evidence.quote));
     }
    }
    const reason=node('textarea');reason.rows=2;reason.maxLength=2000;field(form,'Почему выбран этот вариант',reason);
    for(const input of [mode,layer,time,reason])input.addEventListener('input',invalidate);
    form.append(button('Подготовить предпросмотр',async()=>{
     if(!subject)throw Error('Выберите главный предмет в точной цитате.');if(!layer.value)throw Error('Выберите временное состояние.');if(!reason.value.trim())throw Error('Укажите основание выбора.');
     const e=epoch,r=++request;
     const decision={mode:mode.value,focus,subject,time_layer:layer.value,time_evidence:time.value===''?null:choices.time_evidence[Number(time.value)],reason:reason.value.trim()};
     for(const key of Object.keys(checked))decision[key]=checked[key].filter(c=>c.input.checked).map(c=>c.value);
     const result=await api(`/scenes/${scene.id}/editorial-plans`,{decision,revision:choices.revision});
     if(!section.isConnected||e!==epoch||r!==request)return;
     plan=result;viewer.replaceChildren(preview(plan));
     if(!plan.apply_blocked_reason)viewer.append(button('Применить просмотренный вариант',async()=>{
      if(plan!==result||e!==epoch)throw Error('Выбор изменился. Подготовьте новый предпросмотр.');
      await api(`/scenes/${scene.id}/editorial-plans/apply`,{plan_id:result.id,revision:result.document.base.revision});await refresh(true);
     },'primary'));
    },'secondary'));
   }
   async function chooseFocus(value){
    invalidate();const r=++request;
    const updated=await api(`/scenes/${scene.id}/editorial-plans/choices`,{focus:value});
    if(!section.isConnected||r!==request)return;
    choices=updated;focus=updated.focus;renderForm();
   }
   holder.insertBefore(button('Использовать выделенный фрагмент',async()=>chooseFocus(selectedSpan(context,choices.context)),'secondary'),form);
   holder.insertBefore(button('Вернуться к полной цитате',async()=>chooseFocus(choices.source),'secondary'),form);
   renderForm();
   const versions=node('details');versions.append(node('summary','Сохранённые варианты'));holder.append(versions);let versionsLoaded=false;
   versions.addEventListener('toggle',async()=>{
    if(!versions.open||versionsLoaded)return;versionsLoaded=true;
    try{const stored=await api(`/scenes/${scene.id}/editorial-plans`);if(!section.isConnected)return;
     for(const v of stored.plans){const row=preview(v);row.append(node('small',v.applied?'Этот вариант применялся.':'Сохранён для просмотра.'));versions.append(row);}
     if(!stored.plans.length)versions.append(node('p','Сохранённых вариантов пока нет.'));
    }catch(error){versionsLoaded=false;versions.append(node('p','Не удалось прочитать сохранённые варианты.'));}
   });
  }catch(error){if(section.isConnected){loaded=false;holder.replaceChildren(node('p',error.message||'Не удалось прочитать основания кадра.'));}}
 }
 return section;
}
