export function analysisError(error){
  if(/(?:Groq request size limit|Request too large|Groq API error \(413\))/i.test(error||''))return {
    text:'Сервис проверки отклонил слишком большой запрос.',
    help:'Повторите проверку: программа разделит запрос на меньшие части.',
    details:error
  };
  return null;
}

const terminalReviewStates=new Set(['error_response','error_request','error_access','budget_too_small','transport_unknown','cancelled']);
export function analysisReviewState(generation,job){
  if(generation?.purpose!=='analysis'||generation.state!=='waiting_review')return null;
  const wait=generation.snapshot?.plan_review_wait;
  if(!wait)return null;
  const state=job?.state||wait.state;
  if(!terminalReviewStates.has(state))return null;
  let text='Заключительная проверка плана остановлена.';
  if(job?.reason==='http_503')text='Google временно недоступен (ошибка 503).';
  else if(job?.reason?.match(/^http_50[024]$/))text='Сервис Google не выполнил проверку.';
  else if(state==='error_access')text='Нет доступа к сервису проверки Google.';
  else if(state==='budget_too_small')text='Запрос проверки превышает доступный лимит.';
  else if(state==='transport_unknown')text='Не удалось подтвердить результат отправки. Повтор заблокирован.';
  else if(state==='error_response')text='Не удалось разобрать ответ проверки плана.';
  else if(state==='cancelled')text='Проверка плана завершена без результата.';
  const scenes=generation.snapshot?.pending_analysis?.scenes?.length;
  return {title:'Проверка плана остановлена',text,
    help:scenes?`Разбор сохранён: ${scenes} сцен. Заново разбирать сценарий не нужно.`:'Разбор сохранён. Заново разбирать сценарий не нужно.',
    canRetry:job?.can_retry===true,jobId:wait.job_id};
}
