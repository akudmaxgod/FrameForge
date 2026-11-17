// One local workflow: current sources -> phrase alignment -> applied timings -> MP4.
export async function buildScienceMovie({id,api,getState,flush,synchronize,accept,progress=()=>{},checkpoint={},saveCheckpoint=()=>{},wait=ms=>new Promise(r=>setTimeout(r,ms)),cancelled=()=>false}){
 const base=`/projects/${id}/edit`;let fit;
 await flush();
 if(checkpoint.fit_id){fit=await api(`${base}/voice-fit/${checkpoint.fit_id}`);if(fit.status==='applied'&&fit.applied_revision!==getState().revision){fit=null;checkpoint={};}}
 if(!fit){
  progress('Обновляю готовые сцены');await synchronize();await flush();
  const state=getState();if(!(state.doc.audio_tracks||[]).some(a=>a.role==='voice'&&a.enabled!==false))throw Error('Добавьте озвучку на звуковую дорожку, затем повторите сборку.');
  if(!state.doc.clips.some(c=>!c.excluded))throw Error('Готовые сцены пока не добавлены в монтаж.');
  fit=await api(`${base}/voice-fit`,{expected_revision:state.revision,timing_mode:'phrases',short_video_policy:'retime'});
  checkpoint={fit_id:fit.id,request_key:globalThis.crypto.randomUUID()};saveCheckpoint(checkpoint);
 }
 while(['queued','recognizing'].includes(fit.status)){
  if(cancelled()){await api(`${base}/voice-fit/${fit.id}/cancel`,{});saveCheckpoint(null);throw Error('Сборка остановлена до экспорта.');}
  progress(`${fit.stage||'Сопоставляю слова с озвучкой'} · ${Math.round(fit.progress||0)}%`);await wait(1000);fit=await api(`${base}/voice-fit/${fit.id}`);
 }
 if(cancelled())throw Error('Сборка остановлена до экспорта.');
 if(fit.status==='ready'){
  if(!fit.proposal?.can_apply)throw Error(fit.proposal?.conflicts?.map(x=>x.message).join(' ')||'Не удалось сопоставить сцены с записью.');
  progress('Применяю тайминги слов');accept(await api(`${base}/voice-fit/${fit.id}/apply`,{expected_revision:getState().revision}));
 }else if(fit.status==='applied')accept(await api(base));
 else throw Error(fit.error||fit.stage||'Подгонка озвучки не завершена.');
 if(cancelled())throw Error('Сборка остановлена до экспорта.');
 checkpoint.phase='export';saveCheckpoint(checkpoint);progress('Собираю итоговый MP4 на видеокарте');
 const job=await api(`${base}/export`,{expected_revision:getState().revision,request_key:checkpoint.request_key});
 checkpoint.export_id=job.id;saveCheckpoint(checkpoint);return job;
}
