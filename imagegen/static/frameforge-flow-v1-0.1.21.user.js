// ==UserScript==
// @name FrameForge Flow Bridge (new)
// @namespace frameforge.local.bridge
// @version 0.1.21
// @match https://flow.google.com/project/*
// @grant GM_xmlhttpRequest
// @grant GM_getValue
// @grant GM_setValue
// @grant GM_deleteValue
// @connect 127.0.0.1
// @noframes
// ==/UserScript==
(() => {
 'use strict';
 const CHANNEL='frameforge.bridge.v1';
 const STORE='ff-flow-v1-state';
 let peer=null, peerOrigin=null, running=false, pauseRequested=false, pending=null;
 let heartbeatBusy=false, ackCommandId=null, processedCommandId=null;
 const adapterInstanceId=crypto.randomUUID();
 let state=null;
 const queueKey=(projectId,flowProject)=>`${STORE}:${projectId}:${flowProject}`;
 const root=document.createElement('aside');
 root.style.cssText='position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#171923;color:white;padding:16px;border:1px solid #888;border-radius:12px;font:14px sans-serif;max-width:360px;display:flex;flex-direction:column;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,0.5)';
 const status=document.createElement('div');
 status.style.cssText='word-break:break-word;font-size:13px;line-height:1.4';
 const btnRow=document.createElement('div');
 btnRow.style.cssText='display:flex;flex-wrap:wrap;gap:6px';
 const start=document.createElement('button');start.textContent='ФФ: старт / продолжить';
 const check=document.createElement('button');check.textContent='Проверить связь';
 const pause=document.createElement('button');pause.textContent='Пауза';
 const reset=document.createElement('button');reset.textContent='Сброс кэша';
 for(const b of [check,start,pause,reset]){
  b.style.cssText='padding:6px 10px;border-radius:6px;border:1px solid #555;background:#2d3748;color:#fff;cursor:pointer;font-size:12px';
 }
 start.style.background='#2b6cb0';start.style.borderColor='#3182ce';start.style.fontWeight='bold';
 btnRow.append(check,start,pause,reset);
 root.append(status,btnRow);document.body.append(root);
 const tell=text=>{console.log('[FF Bridge Status]',text);status.textContent=text;};
 const save=()=>{if(state)GM_setValue(queueKey(state.projectId,state.flowProject),state);};
 tell('Ожидание инструмента Flow с адаптером v1');
 function api(method,path,data) {
  return new Promise((resolve,reject)=>GM_xmlhttpRequest({method,url:'http://127.0.0.1:8765'+path,
   headers:{'Content-Type':'application/json','X-Client':'google-flow'},data:data===undefined?undefined:JSON.stringify(data),timeout:30000,
   onload:r=>{try{const value=JSON.parse(r.responseText);if(r.status<200||r.status>=300)throw Error(value.error||`HTTP ${r.status}`);resolve(value);}catch(e){reject(e);}},
   onerror:()=>reject(Error('ФФ недоступен')),ontimeout:()=>reject(Error('Таймаут ФФ: результат сохранён локально, проверьте ФФ перед повтором'))
  }));
 }
 function loadSourceImageDataUrl(relativeUrl) {
  if(!relativeUrl) return Promise.resolve(null);
  console.log('[FF Bridge] Fetching source image asset:', relativeUrl);
  return new Promise((resolve,reject)=>GM_xmlhttpRequest({
   method:'GET',url:'http://127.0.0.1:8765'+relativeUrl,responseType:'arraybuffer',timeout:30000,
   onload:r=>{try{
    if(r.status<200||r.status>=300)throw Error(`Не удалось прочитать исходный кадр (HTTP ${r.status})`);
    const mime=(r.responseHeaders.match(/content-type:\s*([^;\r\n]+)/i)||[])[1]||'image/png';
    const bytes=new Uint8Array(r.response);let binary='';
    for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
    resolve(`data:${mime};base64,${btoa(binary)}`);
   }catch(error){reject(error);}},
   onerror:()=>reject(Error('Не удалось прочитать исходный кадр из FrameForge')),
   ontimeout:()=>reject(Error('Таймаут чтения исходного кадра из FrameForge'))
  }));
 }
 const allowedOrigin=origin=>{try{const u=new URL(origin);return u.protocol==='https:'&&(u.hostname.endsWith('.usercontent.goog')||u.hostname.endsWith('.googleusercontent.com')||u.hostname.includes('flow.google.com'));}catch{return false;}};
 window.addEventListener('message',e=>{
  const m=e.data;if(!m||m.channel!==CHANNEL||!allowedOrigin(e.origin))return;
  if(m.type==='ready'&&!running&&!pending){
   const changed=peer!==e.source;peer=e.source;peerOrigin=e.origin;
   if(changed&&!state?.error)tell('Инструмент подключён. Можно запустить ФФ.');
   return;
  }
  if(!pending||m.requestId!==pending.id)return;
  console.log('[FF Bridge] Received reply from custom tool:', m.type);
  if(m.type==='pong'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve({pong:true});}
  if(m.type==='result'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve(m.result);}
  if(m.type==='scene_video_result'){clearTimeout(pending.timer);const p=pending;pending=null;p.resolve({mediaId:m.flow_media_id||m.mediaId,base64:m.base64,mimeType:m.mimeType||'video/mp4'});}
  if(m.type==='error'||m.type==='scene_video_error'){clearTimeout(pending.timer);const p=pending;pending=null;p.reject(Error(m.error));}
  if(m.type==='scene_video_progress'){tell(`ФФ: Veo генерация кадра ${m.scene_id||''} (${m.status||'в процессе'})...`);}
 });
 function generate(job,type='generate') {
  return new Promise((resolve,reject)=>{
   if(!peer||!peerOrigin){
    reject(Error('Инструмент Flow не подключён.'));
    return;
   }
   console.log('[FF Bridge] Sending message to tool:', type, job);
   const timeoutMs=type==='ping'?5000:240000;
   const timer=setTimeout(()=>{
    pending=null;
    if(type==='ping')reject(Error('Адаптер Flow не ответил на ping (проверьте код инструмента)'));
    else reject(Error('Flow не ответил за 4 минуты. Проверьте результат в Flow.'));
   },timeoutMs);
   pending={id:job.requestId,resolve,reject,timer};
   peer.postMessage({channel:CHANNEL,type,...job},peerOrigin);
  });
 }
 async function uploadSaved() {
  if(!state?.result||!state?.job)return;
  const {kind,id,scene_id,idempotency_key,scene_revision,source_image_asset_id}=state.job;
  const sid=scene_id||id;
  const result=state.result;
  console.log('[FF Bridge] Uploading saved result for kind:', kind, sid);
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
  if(kind==='scenes'&&state.job.sourceImageAssetId&&result.mediaId){
   state.refs[`scenes:${id}`]=result.mediaId;
   if(!state.done.includes(`scenes:${id}`))state.done.push(`scenes:${id}`);
   state.job=null;state.result=null;save();
   return;
  }
  if(!result.base64||!result.mediaId||!/^image\/(png|jpeg|webp)$/.test(result.mimeType))throw Error('Некорректный результат Flow');
  state.refs[`${kind}:${id}`]=result.mediaId;
  const filename=result.mimeType==='image/jpeg'?'image.jpg':result.mimeType==='image/webp'?'image.webp':'image.png';
  await api('POST',`/api/${kind}/${encodeURIComponent(id)}/upload`,{
   image:`data:${result.mimeType};base64,${result.base64}`,
   flow_media_id:result.mediaId,filename
  });
  state.done.push(`${kind}:${id}`);state.job=null;state.result=null;save();
 }
 async function run() {
  if(running)return;
  if(!peer){tell('Откройте инструмент с адаптером v1');return;}
  running=true;pauseRequested=false;
  console.log('[FF Bridge] Running generation queue...');
  try {
   await heartbeat(true);
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)throw Error('В ФФ нет активного проекта');
   const flowProject=location.pathname.split('/')[2];
   state=GM_getValue(queueKey(project_id,flowProject),null);
   if(!state){
    const legacy=GM_getValue(STORE,null);
    if(legacy?.projectId===project_id&&legacy?.flowProject===flowProject)state=legacy;
   }
   if(!state)state={projectId:project_id,flowProject,refs:{},done:[],job:null,result:null};
   if(state.job&&!state.result){
    console.log('[FF Bridge] Clearing uncompleted pending job from state:', state.job);
    state.job=null;state.result=null;state.error=null;
   }
   save();
   await uploadSaved();
   for(const kind of ['characters','locations','props','scenes']) {
    const data=await api('GET',`/api/projects/${project_id}/prompts`);
    for(const item of (data[kind]||[]).filter(i=>kind!=='props'||!i.importance||i.importance==='story')) {
     if(pauseRequested)break;
     const id=kind==='scenes'?item.scene_id:item.id;
     if(kind==='scenes' ? (item.has_image||state.refs['scenes:'+id]) : item.locked)continue;
     if(kind==='scenes'&&item.ready_for_flow===false){
      throw Error(item.flow_error || `Кадр ${item.position}: отсутствует закреплённый референс персонажа.`);
     }
     const active=await api('GET','/api/projects/current');
     if(active.project_id!==project_id)throw Error('Активный проект изменился');
     const referenceImageMediaIds=(item.has_image?[]:(item.references||[])).map(ref=>{
      const key=`${ref.type==='location'?'locations':ref.type==='prop'?'props':'characters'}:${ref.id}`;
      if(!state.refs[key])throw Error('Нет mediaId закреплённого референса в Flow: '+ref.name);
      return state.refs[key];
     });
     const prompt=item.flow_prompt||item.prompt;
     if(!id||!prompt)throw Error('Задание без ID или промпта');
     const isSquare=(kind==='characters'||kind==='props');
     const sourceImageAssetId=kind==='scenes'?item.image_asset_id||null:null;
     const sourceImageData=sourceImageAssetId?await loadSourceImageDataUrl(item.image_url):null;
     state.job={requestId:crypto.randomUUID(),kind,id,prompt,aspectRatio:isSquare?'1:1':'16:9',referenceImageMediaIds,sourceImageAssetId,sourceImageDataUrl:sourceImageData};save();
     tell(`ФФ ${project_id}: ${kind} ${item.name||item.position}; сохранено ${state.done.length}`);
     state.result=await generate(state.job);save();
     await uploadSaved();
    }
    if(pauseRequested)break;
   }
   if(!pauseRequested) {
    const animData=await api('GET',`/api/projects/${project_id}/animation/jobs`);
    const veoJobs=(animData.jobs||[]);
    console.log('[FF Bridge] Animation jobs from server:', veoJobs);
    if(animData.plan_mode==='economy'&&veoJobs.length>5){
     throw Error('Превышен лимит 5 Veo jobs в экономном режиме');
    }
    const maxVeo=animData.plan_mode==='economy'?5:veoJobs.length;
    let processedVeo=0;
    for(const job of veoJobs) {
     if(pauseRequested)break;
     if(processedVeo>=maxVeo)break;

     const active=await api('GET','/api/projects/current');
     if(active.project_id!==project_id)throw Error('Активный проект изменился');

     let sceneMediaId=state.refs[`scenes:${job.scene_id}`]||(job.referenceImageMediaIds&&job.referenceImageMediaIds[0])||null;
     let sourceImageData=null;
     if(job.source_image_asset_id){
      try{
       sourceImageData=await loadSourceImageDataUrl(`/api/assets/${job.source_image_asset_id}`);
      }catch(err){
       console.warn('[FF Bridge] Could not load source image data URL:',err);
      }
     }
     if(!sceneMediaId&&!sourceImageData){
      throw Error(`Кадр ${job.position}: отсутствует mediaId или исходное изображение в Flow.`);
     }

     state.job={
      requestId:crypto.randomUUID(),
      kind:'scene_video',
      scene_id:job.scene_id,
      id:job.scene_id,
      scene_revision:job.scene_revision||job.revision||1,
      source_image_asset_id:job.source_image_asset_id||null,
      sourceImageDataUrl:sourceImageData,
      model:'veo-3.1-lite',
      prompt:job.prompt,
      duration_seconds:job.duration_seconds||5,
      aspect_ratio:job.aspect_ratio||'16:9',
      referenceImageMediaIds:sceneMediaId?[sceneMediaId]:[],
      firstFrameImageMediaId:sceneMediaId,
      idempotency_key:job.idempotency_key
     };
     save();
     tell(`ФФ: запуск Veo 3.1 Lite для кадра ${job.position} (ожидание ~1 мин)...`);
     console.log('[FF Bridge] Calling generate for scene_video:', state.job);
     state.result=await generate(state.job);
     console.log('[FF Bridge] Video generation result received:', state.result);
     save();
     tell(`ФФ: сохранение видео кадра ${job.position} в FrameForge...`);
     await uploadSaved();
     processedVeo++;
     tell(`ФФ: кадр ${job.position} успешно сгенерирован и сохранён!`);
    }
   }
   if(!pauseRequested){
    tell('Очередь завершена');
   }else{
    tell('Пауза. Текущий кадр сохранён');
   }
  }catch(e){
   console.error('[FF Bridge Error]', e);
   if(state){state.error=e.message;save();}
   tell(e.message);
  }finally{
   running=false;
  }
 }
 async function heartbeat(claimAdapter=false) {
  if(heartbeatBusy)return;
  heartbeatBusy=true;
  try {
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)return;
   const response=await api('POST','/api/bridge/v1/heartbeat',{
    project_id,flow_project_id:location.pathname.split('/')[2],
    adapter_connected:Boolean(peer),running,status:status.textContent,ack_command_id:ackCommandId,
    adapter_instance_id:adapterInstanceId,claim_adapter:claimAdapter
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
 check.addEventListener('click',async()=>{
  if(running)return;
  if(pending){clearTimeout(pending.timer);pending=null;}
  if(!peer){tell('Адаптер Flow ещё не подключён');return;}
  try{
   await heartbeat(true);
   await generate({requestId:crypto.randomUUID()},'ping');
   const current=await api('GET','/api/projects/current');
   tell('Связь Flow ↔ мост ↔ ФФ проверена. Проект: '+(current.project_id||'не выбран'));
  }catch(e){tell(e.message);}
 });
 start.addEventListener('click',()=>{
  if(pending){clearTimeout(pending.timer);pending=null;}
  run();
 });
 pause.addEventListener('click',()=>{if(running){pauseRequested=true;tell('Пауза запрошена: сохраняю текущий кадр');}});
 reset.addEventListener('click',async()=>{
  try{
   const {project_id}=await api('GET','/api/projects/current');
   const flowProject=location.pathname.split('/')[2];
   if(project_id)GM_setValue(queueKey(project_id,flowProject),null);
   GM_setValue(STORE,null);
   state=null;pending=null;running=false;
   tell('Кэш моста сброшен. Нажмите «ФФ: старт / продолжить».');
  }catch(e){tell('Ошибка сброса: '+e.message);}
 });
})();
