// ==UserScript==
// @name FrameForge Flow Bridge (new)
// @namespace frameforge.local.bridge
// @version 0.1.16
// @match https://flow.google.com/project/*
// @grant GM_xmlhttpRequest
// @grant GM_getValue
// @grant GM_setValue
// @connect 127.0.0.1
// @noframes
// ==/UserScript==
(() => {
 'use strict';
 const CHANNEL='frameforge.bridge.v1';
 const STORE='ff-flow-v1-state';
 let peer=null, peerOrigin=null, running=false, pauseRequested=false, pending=null;
 let heartbeatBusy=false, ackCommandId=null, processedCommandId=null;
 let state=null;
 const queueKey=(projectId,flowProject)=>`${STORE}:${projectId}:${flowProject}`;
 const root=document.createElement('aside');
 root.style.cssText='position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#171923;color:white;padding:16px;border:1px solid #888;border-radius:12px;font:14px sans-serif;max-width:340px';
 const status=document.createElement('div');
 const start=document.createElement('button');start.textContent='ФФ: старт / продолжить';
 const check=document.createElement('button');check.textContent='Проверить связь';
 const pause=document.createElement('button');pause.textContent='Пауза после кадра';
 root.append(status,check,start,pause);document.body.append(root);
 const tell=text=>status.textContent=text;
 const save=()=>GM_setValue(queueKey(state.projectId,state.flowProject),state);
 tell(state?.error || 'Ожидание инструмента Flow с адаптером v1');
 function api(method,path,data) {
  return new Promise((resolve,reject)=>GM_xmlhttpRequest({method,url:'http://127.0.0.1:8765'+path,
   headers:{'Content-Type':'application/json','X-Client':'google-flow'},data:data===undefined?undefined:JSON.stringify(data),timeout:30000,
   onload:r=>{try{const value=JSON.parse(r.responseText);if(r.status<200||r.status>=300)throw Error(value.error||`HTTP ${r.status}`);resolve(value);}catch(e){reject(e);}},
   onerror:()=>reject(Error('ФФ недоступен')),ontimeout:()=>reject(Error('Таймаут ФФ: результат сохранён локально, проверьте ФФ перед повтором'))
  }));
 }
 const allowedOrigin=origin=>{try{const u=new URL(origin);return u.protocol==='https:'&&u.hostname.endsWith('.scf.usercontent.goog');}catch{return false;}};
 window.addEventListener('message',e=>{
  const m=e.data;if(!m||m.channel!==CHANNEL||!allowedOrigin(e.origin))return;
  if(m.type==='ready'&&!running&&!pending){const changed=peer!==e.source;peer=e.source;peerOrigin=e.origin;if(changed&&!state?.error)tell('Инструмент подключён. Можно запустить ФФ.');return;}
  if(!pending||e.source!==peer||e.origin!==peerOrigin||m.requestId!==pending.id)return;
  if(m.type==='pong'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve({pong:true});}
  if(m.type==='result'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve(m.result);}
  if(m.type==='scene_video_result'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve({mediaId:m.flow_media_id||m.mediaId,base64:m.base64,mimeType:m.mimeType||'video/mp4'});}
  if(m.type==='error'||m.type==='scene_video_error'){clearTimeout(pending.timer);const p=pending;pending=null;p.reject(Error(m.error));}
  if(m.type==='scene_video_progress'){tell(`ФФ: Veo генерация кадра ${m.scene_id||''} (${m.status||'в процессе'})`);}
 });
 function generate(job,type='generate') {
  return new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>{pending=null;reject(Error('Flow не ответил. Автоповтор отключён; проверьте результат в Flow.'));},240000);
   pending={id:job.requestId,resolve,reject,timer};
   peer.postMessage({channel:CHANNEL,type,...job},peerOrigin);
  });
 }
 async function uploadSaved() {
  if(!state.result)return;
  const {kind,id,scene_id,idempotency_key,scene_revision,source_image_asset_id}=state.job;
  const sid=scene_id||id;
  const result=state.result;
  if(kind==='scene_video'){
   if(!result.base64||!/^video\/(mp4|webm)$/.test(result.mimeType))throw Error('Некорректный результат Flow видео');
   const filename=result.mimeType==='video/mp4'?'video.mp4':'video.webm';
   await api('POST',`/api/scenes/${encodeURIComponent(sid)}/video`,{
    video:`data:${result.mimeType};base64,${result.base64}`,
    filename,
    flow_media_id:result.mediaId||null,
    idempotency_key:idempotency_key||null,
    expected_scene_revision:scene_revision||null,
    source_image_asset_id:source_image_asset_id||null
   });
   if(result.mediaId)state.refs[`scene_video:${sid}`]=result.mediaId;
   const doneKey=idempotency_key?`scene_video:${sid}:${idempotency_key}`:`scene_video:${sid}`;
   if(!state.done.includes(doneKey))state.done.push(doneKey);
   state.job=null;state.result=null;save();
   return;
  }
  if(!result.base64||!result.mediaId||!/^image\/(png|jpeg|webp)$/.test(result.mimeType))throw Error('Некорректный результат Flow');
  await api('POST',`/api/${kind}/${encodeURIComponent(id)}/upload`,{image:`data:${result.mimeType};base64,${result.base64}`,lock:true});
  state.refs[`${kind}:${id}`]=result.mediaId;
  state.done.push(`${kind}:${id}`);state.job=null;state.result=null;save();
 }
 async function run() {
  if(running||pending)return;
  if(!peer){tell('Откройте инструмент с адаптером v1');return;}
  running=true;pauseRequested=false;
  try {
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)throw Error('В ФФ нет активного проекта');
   const flowProject=location.pathname.split('/')[2];
   state=GM_getValue(queueKey(project_id,flowProject),null);
   if(!state){
    const legacy=GM_getValue(STORE,null);
    if(legacy?.projectId===project_id&&legacy?.flowProject===flowProject)state=legacy;
   }
   if(!state)state={projectId:project_id,flowProject,refs:{},done:[],job:null,result:null};
   save();
   if(state.job&&!state.result){
    const staleStaticSync=state.job.kind!=='scene_video'&&state.refs[state.job.kind+':'+state.job.id];
    const retryable=Boolean(staleStaticSync)||state.error&&(
      /Expected object response with media fields|Flow video error|Flow returned invalid video payload|Flow\.generate\.video/i.test(state.error)
      || /Предыдущая генерация не подтверждена|Flow не ответил/i.test(state.error)
    );
    if(!retryable)throw Error('Предыдущая генерация не подтверждена. Проверьте Flow; повтор автоматически не запускается.');
    state.job=null;state.result=null;state.error=null;save();
    tell('Сбрасываю завершившуюся ошибкой задачу Flow и запускаю её заново');
   }
   await uploadSaved();
   for(const kind of ['characters','locations','props','scenes']) {
    const data=await api('GET',`/api/projects/${project_id}/prompts`);
    for(const item of (data[kind]||[]).filter(i=>kind!=='props'||!i.importance||i.importance==='story')) {
     if(pauseRequested)break;
     const id=kind==='scenes'?item.scene_id:item.id;
     if(kind==='scenes' ? (item.has_image||state.refs['scenes:'+id]) : item.locked)continue;
     if(kind==='scenes'&&item.ready_for_flow===false){
      throw Error(item.flow_error || `Кадр ${item.position}: отсутствует закреплённый референс персонажа. Зафиксируйте портрет, затем запустите ФФ снова.`);
     }
     const active=await api('GET','/api/projects/current');
     if(active.project_id!==project_id)throw Error('Активный проект изменился');
     const referenceImageMediaIds=(item.references||[]).map(ref=>{
      const key=`${ref.type==='location'?'locations':ref.type==='prop'?'props':'characters'}:${ref.id}`;
      if(!state.refs[key])throw Error('Нет mediaId закреплённого референса в Flow: '+ref.name);
      return state.refs[key];
     });
     const prompt=item.flow_prompt||item.prompt;
     if(!id||!prompt)throw Error('Задание без ID или промпта');
     const isSquare=(kind==='characters'||kind==='props');
     state.job={requestId:crypto.randomUUID(),kind,id,prompt,aspectRatio:isSquare?'1:1':'16:9',referenceImageMediaIds};save();
     tell(`ФФ ${project_id}: ${kind} ${item.name||item.position}; сохранено ${state.done.length}`);
     state.result=await generate(state.job);save();
     await uploadSaved();
    }
    if(pauseRequested)break;
   }
   if(!pauseRequested) {
    const animData=await api('GET',`/api/projects/${project_id}/animation/jobs`);
    const veoJobs=(animData.jobs||[]);
    if(animData.plan_mode==='economy'&&veoJobs.length>5){
     throw Error('Превышен лимит 5 Veo jobs в экономном режиме');
    }
    const maxVeo=animData.plan_mode==='economy'?5:veoJobs.length;
    let processedVeo=0;
    for(const job of veoJobs) {
     if(pauseRequested)break;
     if(processedVeo>=maxVeo)break;
     const doneKey=`scene_video:${job.scene_id}:${job.idempotency_key}`;
     if(state.done.includes(doneKey))continue;

     const active=await api('GET','/api/projects/current');
     if(active.project_id!==project_id)throw Error('Активный проект изменился');

     const sceneMediaId=state.refs[`scenes:${job.scene_id}`]||(job.referenceImageMediaIds&&job.referenceImageMediaIds[0]);
     if(!sceneMediaId){
      throw Error(`Кадр ${job.position}: отсутствует mediaId исходного изображения в Flow. Сначала сгенерируйте кадр.`);
     }

     state.job={
      requestId:crypto.randomUUID(),
      kind:'scene_video',
      scene_id:job.scene_id,
      id:job.scene_id,
      scene_revision:job.scene_revision||job.revision||1,
      source_image_asset_id:job.source_image_asset_id||null,
      model:'veo-3.1-lite',
      prompt:job.prompt,
      duration_seconds:job.duration_seconds||5,
      aspect_ratio:job.aspect_ratio||'16:9',
      firstFrameImageMediaId:sceneMediaId,
      idempotency_key:job.idempotency_key
     };
     save();
     tell(`ФФ ${project_id}: Veo 3.1 Lite кадр ${job.position}; сохранено видео ${state.done.filter(k=>k.startsWith('scene_video:')).length}`);
     state.result=await generate(state.job);
     save();
     await uploadSaved();
     processedVeo++;
    }
   }
   tell(!pauseRequested?'Очередь завершена':'Пауза. Текущий кадр сохранён');
  }catch(e){if(state){state.error=e.message;save();}tell(e.message);}finally{running=false;}
 }
 async function heartbeat() {
  if(heartbeatBusy)return;
  heartbeatBusy=true;
  try {
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)return;
   const response=await api('POST','/api/bridge/v1/heartbeat',{
    project_id,flow_project_id:location.pathname.split('/')[2],
    adapter_connected:Boolean(peer),running,status:status.textContent,ack_command_id:ackCommandId
   });
   const command=response.command;
   if(command&&command.id!==processedCommandId&&command.project_id===project_id){
    processedCommandId=command.id;ackCommandId=command.id;
    if(command.action==='start')void run();
    if(command.action==='pause'&&running){pauseRequested=true;tell('Пауза запрошена из ФФ: сохраняю текущий кадр');}
   }
  }catch(e){if(!running)tell('ФФ недоступен: '+e.message);}
  finally{heartbeatBusy=false;}
 }
 setInterval(heartbeat,2500);
 void heartbeat();
 check.addEventListener('click',async()=>{if(running||pending)return;if(!peer){tell('Адаптер Flow ещё не подключён');return;}try{await generate({requestId:crypto.randomUUID()},'ping');const current=await api('GET','/api/projects/current');tell('Связь Flow ↔ мост ↔ ФФ проверена. Проект: '+(current.project_id||'не выбран'));}catch(e){tell(e.message);}});
 start.addEventListener('click',run);
 pause.addEventListener('click',()=>{if(running){pauseRequested=true;tell('Пауза запрошена: сохраняю текущий кадр');}});
})();
