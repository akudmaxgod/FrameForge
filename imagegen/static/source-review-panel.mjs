function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
const kinds={structural:'Ошибка структуры',semantic:'Смысловой конфликт',editorial:'Редакторский совет',provider:'Сервис проверки'};
export function sourceReviewPanel({project,api,button,refresh,notice}){
 const box=node('details');box.dataset.testid='source-review-progress';box.append(node('summary','Проверка содержания и продолжение после сбоя'));const holder=node('div');box.append(holder);let loading=false;
 async function load(){
  if(loading)return;loading=true;holder.replaceChildren(node('p','Читаю состояние проверки…'));
  try{
   const result=await api(`/projects/${project.id}/source-review-status`);holder.replaceChildren();
   if(!result.reports.length)holder.append(node('p','Новая покадровая проверка ещё не запускалась. Сохранённые кадры не менялись.'));
   for(const report of result.reports){
    const counts=report.counts||{},section=node('section');section.append(node('strong',report.stage==='text'?'План по сценарию':'Отдельный план кадров'),node('p',`Принято: ${counts.accepted||0} из ${report.frames.length}. Требуют внимания: ${counts.failed||0}. Не завершено: ${(counts.pending||0)+(counts.reviewing||0)+(counts.paused||0)}.`));
    if(report.stale)section.append(node('p','Сценарий изменился после этой проверки; отчёт относится к прежнему тексту.'));
    if(report.resume_note)section.append(node('p',report.resume_note));
    const sequence=report.sequence_review;
    if(sequence){
     section.append(node('p',sequence.status==='complete'?`Связность проверена: ${(sequence.checked_pairs||[]).length} соседних пар.`:'Проверка связности ещё не завершена. План не опубликован.'));
     for(const issue of sequence.issues||[]){
      const pair=(issue.related_frames||[]).map(key=>report.frames.find(f=>f.key===key)?.number).filter(n=>n!==undefined);
      const details=node('details');details.append(node('summary',`Связность кадров ${pair.join(' → ')} · ${kinds[issue.kind]||issue.kind}`),node('p',issue.message));
      if(issue.minimal_correction)details.append(node('p','Минимальное исправление: '+issue.minimal_correction));
      for(const q of issue.evidence||[])details.append(node('blockquote',q.quote));
      if(issue.kind==='editorial')details.append(node('small','Необязательный совет. Он не блокирует план.'));
      section.append(details);
     }
    }
    for(const frame of report.frames){
     for(const issue of frame.issues||[]){
      const item=node('details');item.append(node('summary',`Кадр ${frame.number} · ${kinds[issue.kind]||issue.kind}`),node('blockquote',frame.source.quote),node('p',issue.message));
      for(const evidence of issue.evidence||[])item.append(node('blockquote',evidence.quote));
      if(issue.kind==='editorial')item.append(node('small','Необязательный совет. Он не блокирует кадр.'));
      if(issue.kind==='provider')item.append(node('small','Сбой сервиса не считается смысловой ошибкой кадра.'));
      section.append(item);
     }
    }
    if(report.can_resume)section.append(button('Продолжить незавершённую проверку',async()=>{
     await api(`/projects/${project.id}/source-review-status/resume`,{stage:report.stage,run_id:report.run_id});notice('Продолжаю проверку. Принятые кадры используются повторно; генерация медиа не запускается.');await refresh(true);await load();
    },'secondary'));
    holder.append(section);
   }
   holder.append(button('Обновить состояние проверки',load,'secondary'));
  }catch(e){holder.replaceChildren(node('p',e.message));}finally{loading=false;}
 }
 box.addEventListener('toggle',()=>{if(box.open)load();});return box;
}
