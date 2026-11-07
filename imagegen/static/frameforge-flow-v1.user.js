// ==UserScript==
// @name FrameForge Flow Bridge (new)
// @namespace frameforge.local.bridge
// @version 0.1.89
// @match https://flow.google.com/project/*
// @match https://flow.google.com/u/*/project/*
// @grant GM_xmlhttpRequest
// @grant GM_getValue
// @grant GM_setValue
// @grant GM_deleteValue
// @connect 127.0.0.1
// @updateURL http://127.0.0.1:8765/frameforge-flow-v1.user.js
// @downloadURL http://127.0.0.1:8765/frameforge-flow-v1.user.js
// @noframes
// ==/UserScript==
(() => {
 'use strict';
 const CHANNEL='frameforge.bridge.v1';
 const STORE='ff-flow-v1-state';
 // Flow adds /u/N when a Google account is selected. Resolve the project by
 // its route, never by a fixed segment (which would bind every account to N).
 const getFlowProjectId=(pathname=location.pathname)=>
  /^\/(?:u\/\d+\/)?project\/([^/]+)(?:\/|$)/.exec(pathname)?.[1]||null;
 const MAX_SCENE_RETRIES=1;
 const MAX_SDK_SCENE_RETRIES=2;
 const SDK_PROFILE_FAILURE_LIMIT=2;
 const BRIDGE_STATE_VERSION=2;
 let peer=null, peerOrigin=null, running=false, pauseRequested=false;
 const pending=new Map();
 let adapterImageConcurrency=1;
 const referenceImports=new Map();
 let heartbeatBusy=false, ackCommandId=null, processedCommandId=null;
 const adapterInstanceId=crypto.randomUUID();
 let state=null;
 const queueKey=(projectId,flowProject)=>`${STORE}:${projectId}:${flowProject}`;
 // A circuit must identify the exact SDK request, not every scene that happens
 // to have the same number of references. The opaque IDs are hashed so they do
 // not appear in the user-visible error or state diagnostics.
 const referenceSignature=refs=>{
  let hash=2166136261;
  for(const value of (Array.isArray(refs)?refs:[])){
   const text=String(value);
   for(let i=0;i<text.length;i++){hash^=text.charCodeAt(i);hash=Math.imul(hash,16777619);}
   hash^=124;hash=Math.imul(hash,16777619);
  }
  return (hash>>>0).toString(16).padStart(8,'0');
 };
 const requestProfile=job=>`${job.kind||'image'}|${job.modelDisplayName||job.model||'default'}|${job.aspectRatio||job.aspect_ratio||'16:9'}|refs:${job.isCompositeReference?`composite(${job.compositeCount||(job.referenceImageMediaIds||[]).length})`:(job.referenceImageMediaIds||[]).length}:${referenceSignature(job.referenceImageMediaIds)}|${(job.referenceKinds||[]).join(',')||'none'}|p:${referenceSignature([job.prompt||''])}`;
 const formatStageError=(item,phase,error,job=null)=>{
  const sceneNum=item?.position!=null?`Кадр ${item.position}`:(item?.name?`${item.name}`:(item?.id?`Элемент ${item.id}`:'Задание'));
  const reqId=job?.requestId||state?.job?.requestId||'нет';
  const rawMsg=error?.flow?.message||error?.message||String(error||'Неизвестная ошибка');
  return `${sceneNum} [этап: ${phase}]: ${rawMsg} (requestId: ${reqId})`;
 };
 const addDiagnostic=(job,error,phase=null)=>{
  if(!state)return;
  state.diagnostics=Array.isArray(state.diagnostics)?state.diagnostics:[];
  state.sdkFailures=state.sdkFailures&&typeof state.sdkFailures==='object'?state.sdkFailures:{};
  state.sdkCircuits=state.sdkCircuits&&typeof state.sdkCircuits==='object'?state.sdkCircuits:{};
  const profile=job?.requestProfile||requestProfile(job||{});
  const entry={at:new Date().toISOString(),profile,kind:job?.kind||'',id:job?.id||job?.scene_id||'',phase:phase||job?.phase||'unknown',requestId:job?.requestId||'',referenceCount:(job?.referenceImageMediaIds||[]).length,name:error?.flow?.name||error?.name||'Error',message:error?.flow?.message||String(error?.message||error),code:error?.flow?.code||error?.code||null};
  state.diagnostics.push(entry);
  state.diagnostics=state.diagnostics.slice(-40);
  if(error?.flow&&/sdk_call|Flow\.generate\.image|generate\.image/i.test(entry.message)){
   const count=(state.sdkFailures[profile]?.count||0)+1;
   state.sdkFailures[profile]={count,last:entry};
   // ERR_GEN_FAILED is the SDK's final rejection of this exact request. A
   // second identical call cannot improve it and may consume another credit.
   const terminalSdkFailure=entry.code==='ERR_GEN_FAILED'||/\bERR_GEN_FAILED\b/i.test(entry.message);
   if(terminalSdkFailure||count>=SDK_PROFILE_FAILURE_LIMIT){
    state.sdkCircuits[profile]={openedAt:entry.at,last:entry,count,terminal:terminalSdkFailure};
   }
  }
  save();
 };
 const assertProfileAvailable=job=>{
  const profile=job.requestProfile||requestProfile(job);
  const circuit=state?.sdkCircuits?.[profile];
  if(circuit){
   const reason=circuit.terminal?'Flow вернул ERR_GEN_FAILED':'повторились одинаковые ошибки SDK';
   throw Error(`Предохранитель остановил повтор этого точного запроса: ${reason}. Референсы не отброшены и модель не заменена. Измените кадр, набор референсов или модель, затем запустите снова.`);
  }
 };
 // The published Custom Tool can lag behind its editor build.  This bounded
 // client-side failover recognizes the host's actual rejection shape and
 // retries the identical scene once on Nano Banana 2.
 const isRetriableProHostFailure=error=>{
  const flow=error?.flow&&typeof error.flow==='object'?error.flow:{};
  return [flow.code,flow.message,flow.cause,error?.message,error?.cause]
   .some(value=>/ERR_GEN_FAILED|Expected object response with media fields/i.test(String(value||'')));
 };
 const generateImageWithModelFailover=async (job,context={})=>{
  const currentState=context.state||state, currentSave=context.save||save;
  try{return await generate(job,'generate',currentState);}
  catch(error){
   const failedModel=job?.modelDisplayName||'🍌 Nano Banana 2';
   // Lite and Pro can both render a card in Flow's gallery while their SDK call
   // still fails before returning media. Retry once on the stable standard
   // image model so a successful result has a mediaId and can be saved to FF.
   if(failedModel==='🍌 Nano Banana 2'||job?.modelFallbackAttempted||!isRetriableProHostFailure(error))throw error;
   const fallbackJob={...job,requestId:crypto.randomUUID(),modelDisplayName:'🍌 Nano Banana 2',modelFallbackAttempted:true};
   fallbackJob.requestProfile=requestProfile(fallbackJob);
   if(currentState){
    currentState.job=fallbackJob;
    currentState.diagnostics=Array.isArray(currentState.diagnostics)?currentState.diagnostics:[];
    currentState.diagnostics.push({at:new Date().toISOString(),requestId:fallbackJob.requestId,phase:'image_model_fallback',fromModel:failedModel,modelDisplayName:'🍌 Nano Banana 2',reason:`ERR_GEN_FAILED (${failedModel} → 🍌 Nano Banana 2)`,referenceCount:(fallbackJob.referenceImageMediaIds||[]).length});
    currentState.diagnostics=currentState.diagnostics.slice(-40);
    currentSave();
   }
   tell(`Flow отклонил ${failedModel}; повторяю тот же кадр на Nano Banana 2...`);
   return await generate(fallbackJob,'generate',currentState);
  }
 };
 const root=document.createElement('aside');
 root.style.cssText='position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#171923;color:white;padding:16px;border:1px solid #888;border-radius:12px;font:14px sans-serif;max-width:380px;display:flex;flex-direction:column;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,0.5)';
 const status=document.createElement('div');
 status.style.cssText='word-break:break-word;font-size:13px;line-height:1.4';
 const start=document.createElement('button');start.textContent='ФФ: старт / продолжить';
 const check=document.createElement('button');check.textContent='Проверить связь';
 const pause=document.createElement('button');pause.textContent='Пауза';
 const reset=document.createElement('button');reset.textContent='Сброс кэша';
 const btnRow=document.createElement('div');
 btnRow.style.cssText='display:flex;flex-wrap:wrap;gap:6px';
 for(const b of [check,start,pause,reset]){
  b.style.cssText='padding:6px 10px;border-radius:6px;border:1px solid #555;background:#2d3748;color:#fff;cursor:pointer;font-size:12px';
 }
 start.style.background='#2b6cb0';start.style.borderColor='#3182ce';start.style.fontWeight='bold';
 btnRow.append(check,start,pause,reset);
 root.append(status,btnRow);document.body.append(root);
 const tell=text=>{console.log('[FF Bridge Status]',text);status.textContent=text;};
 const save=()=>{if(state)GM_setValue(queueKey(state.projectId,state.flowProject),state);};
 // Semantic visual failures are retryable: they are bad candidates, never a
 // finished storyboard frame.  Transport/API failures still stop so that a
 // broken bridge does not spin forever.
 const isRejectedComposition=error=>!/Смысловая проверка|Лимит Gemini/i.test(String(error?.message||error))&&/кадр не принят|недостаточно похож|лицо в кадре|несколько сцен|несколько панел|нет всех персонажей|количество людей|больше рук|субтитр|облачко|диалогов|текст|надпис|анатом|животн|split screen|diptych|triptych|collage|contact sheet|поля|рамк|паспарту|letterbox|pillarbox|холст 16:9|встроенные поля/i.test(String(error?.message||error));
 tell('Ожидание инструмента Flow с адаптером v1');
 function api(method,path,data) {
  const route=path.split('?')[0];
  const manifestRead=method==='GET' && route.endsWith('/prompts');
  const resultUpload=method==='POST' && /\/(?:image|upload|video)$/.test(route);
  const timeout=manifestRead ? 180000 : resultUpload ? 90000 : 30000;
  const timeoutMessage=manifestRead
    ? 'Таймаут ФФ при подготовке промптов. Повторите запуск после восстановления связи.'
    : resultUpload
      ? 'Таймаут сохранения в ФФ: результат сохранён в мосте. При продолжении сначала повторится его сохранение.'
      : 'Таймаут ответа ФФ. Проверьте связь и повторите действие.';
  return new Promise((resolve,reject)=>GM_xmlhttpRequest({method,url:'http://127.0.0.1:8765'+path,
   headers:{'Content-Type':'application/json','X-Client':'google-flow'},data:data===undefined?undefined:JSON.stringify(data),timeout,
   onload:r=>{try{const value=JSON.parse(r.responseText);if(r.status<200||r.status>=300){const error=Error(value.error||`HTTP ${r.status}`);error.code=value.code;error.retry=value.retry;throw error;}resolve(value);}catch(e){reject(e);}},
   onerror:()=>reject(Error('ФФ недоступен')),ontimeout:()=>reject(Error(timeoutMessage))
  }));
 }
  async function createClientCompositeReference(item, selectedRefs) {
  const n=selectedRefs.length;
  if(n<2)return null;
  const loaded=[];
  for(const {ref} of selectedRefs) {
   const url=ref.url||`/api/assets/${ref.asset_id||ref.id}`;
   try {
    const dataUrl=await loadSourceImageDataUrl(url);
    if(dataUrl) {
     const img=await new Promise((res,rej)=>{
      const im=new Image();
      im.onload=()=>res(im);
      im.onerror=rej;
      im.src=dataUrl;
     });
     const label=ref.type==='character'?`Character: @${ref.name}`:ref.type==='location'?`Location: ${ref.name}`:`Prop: ${ref.name}`;
     loaded.push({img,label});
    }
   } catch(e) {
    console.warn('[FF Bridge] Failed to load ref for composite canvas:', ref.name, e);
   }
  }
  if(loaded.length<2)return null;

  const targetW=1536, targetH=864, pad=12, bannerH=32;
  const canvas=document.createElement('canvas');
  canvas.width=targetW; canvas.height=targetH;
  const ctx=canvas.getContext('2d');
  ctx.fillStyle='#12161e';
  ctx.fillRect(0,0,targetW,targetH);

  let boxes=[];
  if(loaded.length===2) {
   const w=Math.floor((targetW-3*pad)/2);
   const h=targetH-2*pad;
   boxes=[[pad,pad,w,h],[2*pad+w,pad,w,h]];
  } else if(loaded.length===3) {
   const hTop=Math.floor((targetH-3*pad)/2);
   const hBot=targetH-3*pad-hTop;
   const wTop=targetW-2*pad;
   const wBot=Math.floor((targetW-3*pad)/2);
   boxes=[
    [pad,pad,wTop,hTop],
    [pad,2*pad+hTop,wBot,hBot],
    [2*pad+wBot,2*pad+hTop,wBot,hBot]
   ];
  } else if(loaded.length===4) {
   const w=Math.floor((targetW-3*pad)/2);
   const h=Math.floor((targetH-3*pad)/2);
   boxes=[
    [pad,pad,w,h],[2*pad+w,pad,w,h],
    [pad,2*pad+h,w,h],[2*pad+w,2*pad+h,w,h]
   ];
  } else {
   const h=Math.floor((targetH-3*pad)/2);
   const wTop=Math.floor((targetW-4*pad)/3);
   const wBot=Math.floor((targetW-3*pad)/2);
   const botOffset=Math.floor((targetW-(2*wBot+pad))/2);
   boxes=[
    [pad+0*(wTop+pad),pad,wTop,h],
    [pad+1*(wTop+pad),pad,wTop,h],
    [pad+2*(wTop+pad),pad,wTop,h],
    [botOffset+0*(wBot+pad),2*pad+h,wBot,h],
    [botOffset+1*(wBot+pad),2*pad+h,wBot,h]
   ];
  }

  for(let i=0;i<loaded.length;i++) {
   const {img,label}=loaded[i];
   const [x,y,w,h]=boxes[i];
   ctx.fillStyle='#1c2230';
   ctx.strokeStyle='#37445f';
   ctx.lineWidth=2;
   ctx.fillRect(x,y,w,h);
   ctx.strokeRect(x,y,w,h);

   ctx.fillStyle='#2a344b';
   ctx.fillRect(x,y,w,bannerH);
   ctx.fillStyle='#e6f0ff';
   ctx.font='bold 16px sans-serif';
   ctx.fillText(label.slice(0,50),x+10,y+22);

   const innerW=w-8, innerH=h-bannerH-8;
   const scale=Math.min(innerW/img.width, innerH/img.height);
   const dw=Math.floor(img.width*scale);
   const dh=Math.floor(img.height*scale);
   const dx=x+4+Math.floor((innerW-dw)/2);
   const dy=y+bannerH+4+Math.floor((innerH-dh)/2);
   ctx.drawImage(img,dx,dy,dw,dh);
  }

  const dataUrl=canvas.toDataURL('image/png');
  let hash=2166136261;
  for(let i=0;i<dataUrl.length;i+=32){hash^=dataUrl.charCodeAt(i);hash=Math.imul(hash,16777619);}
  const sha256=(hash>>>0).toString(16).padStart(8,'0')+String(dataUrl.length);
  return {
   has_composite:true,
   count:loaded.length,
   sha256,
   data_url:dataUrl,
   names:loaded.map(l=>l.label)
  };
 }
 function loadSourceImageDataUrl(relativeUrl, expectedSha256=null) {
   if(!relativeUrl) return Promise.resolve(null);
   const targetUrl=relativeUrl.startsWith('http://')||relativeUrl.startsWith('https://')?relativeUrl:'http://127.0.0.1:8765'+relativeUrl;
   console.log('[FF Bridge] Fetching source image asset:', targetUrl);
   return new Promise((resolve,reject)=>GM_xmlhttpRequest({
    method:'GET',url:targetUrl,responseType:'arraybuffer',timeout:30000,
    onload:async r=>{try{
     if(r.status<200||r.status>=300)throw Error(`Не удалось прочитать исходный кадр (HTTP ${r.status})`);
     const mime=(r.responseHeaders?.match(/content-type:\s*([^;\r\n]+)/i)||[])[1]||'image/png';
     if(!/^image\/(png|jpeg|webp)$/i.test(mime)) throw Error(`Неподдерживаемый формат референса: ${mime}`);
     const bytes=new Uint8Array(r.response||[]);
     if(bytes.length===0) throw Error('Пустой файл референса (0 байт)');
     if(bytes.length>10*1024*1024) throw Error(`Файл референса слишком большой (${bytes.length} байт, макс 10 МБ)`);
     if(expectedSha256){
      if(!crypto.subtle?.digest)throw Error('Невозможно проверить целостность исходного кадра. Видео не отправлено.');
      const digest=await crypto.subtle.digest('SHA-256',bytes);
      const actual=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
      if(actual!==expectedSha256.toLowerCase())throw Error('Исходный кадр изменился при загрузке: SHA-256 не совпадает. Видео не отправлено.');
     }
     let binary='';
     for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,i+0x8000));
     resolve(`data:${mime};base64,${(typeof btoa!=='undefined'?btoa(binary):(typeof Buffer!=='undefined'?Buffer.from(binary,'binary').toString('base64'):''))}`);
    }catch(error){reject(error);}},
    onerror:()=>reject(Error('Не удалось прочитать исходный кадр из FrameForge')),
    ontimeout:()=>reject(Error('Таймаут чтения исходного кадра из FrameForge'))
   }));
 }
 async function ensureReferenceMediaId(ref, position, options = {}) {
   if(!ref) return null;
   const currentState = options.state || state;
   const currentTell = options.tell || tell;
   const currentSave = options.save || save;
   const currentLoadSourceImageDataUrl = options.loadSourceImageDataUrlFn || loadSourceImageDataUrl;
   const currentGenerate = options.generateFn || generate;
   const currentChannel = options.CHANNEL || CHANNEL;

   const kindPrefix = ref.type === 'location' ? 'locations' : ref.type === 'prop' ? 'props' : 'characters';
   const key = `${kindPrefix}:${ref.id}`;
   const assetId = ref.asset_id || ref.portrait || ref.environment || ref.prop || null;
   const scopedKey = (assetId && currentState?.projectId && currentState?.flowProject) ? `ref:${currentState.projectId}:${currentState.flowProject}:${assetId}` : null;

   if(!currentState) return null;
   currentState.refs = currentState.refs && typeof currentState.refs === 'object' ? currentState.refs : {};
   currentState.refAssets = currentState.refAssets && typeof currentState.refAssets === 'object' ? currentState.refAssets : {};

   let mediaId = scopedKey ? (currentState.refs[scopedKey] || null) : (currentState.refs[key] || null);
   if(mediaId && assetId && currentState.refAssets[key] && currentState.refAssets[key] !== assetId) {
    mediaId = null;
   }

   if(mediaId) {
    if(scopedKey && !currentState.refs[scopedKey]) currentState.refs[scopedKey] = mediaId;
    if(assetId) currentState.refAssets[key] = assetId;
    return mediaId;
   }

   const refType = ref.type || (kindPrefix === 'locations' ? 'location' : kindPrefix === 'props' ? 'prop' : 'character');
   const refLabel = refType === 'character' ? `портрета ${ref.name}` : refType === 'location' ? `локации ${ref.name}` : `предмета ${ref.name}`;
   currentTell(`Импорт закреплённого ${refLabel} в проект Flow...`);
   console.log('[FF Bridge] Auto-importing missing/stale reference into Flow:', ref);

   const refUrl = ref.url || (assetId ? `/api/assets/${assetId}` : (ref.portrait_url || ref.environment_url || ref.prop_url || `/api/assets/${ref.id}`));
   let dataUrl = null;
   try {
    dataUrl = await currentLoadSourceImageDataUrl(refUrl);
   } catch(e) {
    console.warn('[FF Bridge] Failed to load reference data URL from FF:', ref.name, e);
    throw e;
   }

   const failMsg = refType === 'character'
    ? `Не удалось заново импортировать портрет ${ref.name} в текущий проект Flow`
    : (refType === 'location'
      ? `Не удалось заново импортировать локацию ${ref.name} в текущий проект Flow`
      : `Не удалось заново импортировать предмет ${ref.name} в текущий проект Flow`);

   if(!dataUrl) {
    throw Error(failMsg);
   }

   let importRes = null;
   try {
    importRes = await currentGenerate({
     requestId: crypto.randomUUID(),
     channel: currentChannel,
     type: 'import_source_image',
     scene_id: ref.id,
     source_image_asset_id: assetId || ref.id,
     imageDataUrl: dataUrl,
     sourceImageDataUrl: dataUrl,
     base64: dataUrl.split(',')[1]
    }, 'import_source_image');
   } catch(e) {
    console.warn('[FF Bridge] Flow tool import_source_image failed for reference:', ref.name, e);
    throw e;
   }

   mediaId = importRes?.mediaId || importRes?.id || null;
   if(!mediaId) {
    throw Error(failMsg);
   }

   currentState.refs[key] = mediaId;
   if(scopedKey) currentState.refs[scopedKey] = mediaId;
   if(assetId) currentState.refAssets[key] = assetId;
   currentSave();
   currentTell(`Референс ${ref.name} успешно подготовлен в Flow.`);
   return mediaId;
 }

 function confirmedRemoteFailure(error,legacy=false) {
   const flow=error?.flow||{};
   if(!legacy && flow.remoteReply!==true)return false;
   const text=String(error?.message||error||'');
   // A network/decode failure can hide a completed paid image. Keep it unknown.
   if(/timeout|timed out|таймаут|не ответил|no transferable|decode|network|transport|медиа.*не найден/i.test(text))return false;
   return /ERR_UNSUPPORTED_MODEL|ERR_TOO_MANY_REFS|FLOW_BUSY|PreflightError|Сервис создания изображений недоступен/i.test(text+' '+String(flow.code||''));
 }
 function recordFailedScene(root,job,error) {
   const failure={requestId:job.requestId,sceneId:job.id,position:job.position,
     code:error?.flow?.code||job.remoteFailure?.code||'REMOTE_REJECTED',
     reason:String(error?.message||error||''),admissionEnabled:job.admissionEnabled,status:'failed_remote',at:new Date().toISOString()};
   root.failedScenes=root.failedScenes||{};
   root.failedScenes[`scenes:${job.id}`]=failure;
   return failure;
 }

 function admissionFailureCode(failure) {
   const reason=String(failure.reason||failure.message||'');
   if(/timeout|timed out|таймаут|не ответил|no transferable|decode|network|transport/i.test(reason))return null;
   if(/Сервис создания изображений недоступен/i.test(reason))return 'FLOW_SERVICE_UNAVAILABLE';
   for(const code of ['ERR_UNSUPPORTED_MODEL','ERR_TOO_MANY_REFS','FLOW_BUSY'])
     if(failure.code===code||reason.includes(code))return code;
   if(/PreflightError/i.test(reason))return 'PREFLIGHT_REJECTED';
   return null;
 }
 async function releaseSceneAdmission(options,job,failure,codeOverride) {
   if(job.admissionEnabled===false||failure.admissionReleased)return false;
   const code=codeOverride||admissionFailureCode(failure);
   if(!code)return false;
   if(!options.api) {
     if(job.admissionEnabled===true)throw Error('Не удалось подтвердить освобождение разрешения Flow. Попытка сохранена.');
     return false;
   }
   await options.api('POST','/api/bridge/v1/admission/failure',{
     scene_id:job.id||failure.sceneId,request_id:job.requestId||failure.requestId,code});
   failure.admissionReleased=true;options.save();
   return true;
 }
 const allowedOrigin=origin=>{try{const u=new URL(origin);return u.protocol==='https:'&&(u.hostname.endsWith('.usercontent.goog')||u.hostname.endsWith('.googleusercontent.com')||u.hostname==='flow.google.com');}catch{return false;}};
 window.addEventListener('message',e=>{
  const m=e.data;if(!m||m.channel!==CHANNEL||!allowedOrigin(e.origin))return;
  if(m.type==='ready'&&!running&&!pending.size){
   adapterImageConcurrency=Number.isInteger(m.imageConcurrency)?Math.max(1,Math.min(4,m.imageConcurrency)):1;
   const changed=peer!==e.source;peer=e.source;peerOrigin=e.origin;
   if(changed&&!state?.error)tell('Инструмент подключён. Можно запустить ФФ.');
   return;
  }
  const request=pending.get(m.requestId);
  if(e.source!==peer||e.origin!==peerOrigin)return;
  if(!request){
   if(captureLateVideoResult(state,m)){save();tell('Поздний ответ на видеозапрос сохранён. Нажмите продолжение для проверки и загрузки в ФФ.');}
   return;
  }
  console.log('[FF Bridge] Received reply from custom tool:', m.type, m);
  if(m.type==='pong'){adapterImageConcurrency=Number.isInteger(m.imageConcurrency)?Math.max(1,Math.min(4,m.imageConcurrency)):adapterImageConcurrency;clearTimeout(request.timer);const p=request;pending.delete(m.requestId);p.resolve({pong:true});}
  if(m.type==='source_image_imported'){
   clearTimeout(request.timer);const p=request;pending.delete(m.requestId);
   p.resolve({
    mediaId:m.mediaId,
    source_image_asset_id:m.source_image_asset_id,
    source_image_sha256:m.source_image_sha256,
    scene_revision:m.scene_revision,
    mimeType:m.mimeType||'image/png'
   });
  }
  if(m.type==='result'){
   clearTimeout(request.timer);const p=request;pending.delete(m.requestId);
   const result=m.result;
   // A custom tool must never label an SDK rejection as a result.  This guard
   // keeps an outdated tool from converting ERR_GEN_FAILED into the misleading
   // "Expected object response with media fields" upload error.
   if(!result||typeof result!=='object'||result.error){
    const embedded=result&&typeof result==='object'?result.error||result:result;
    const scalar=value=>typeof value==='string'||typeof value==='number'?String(value):'';
    const embeddedObject=embedded&&typeof embedded==='object'?embedded:{};
    const errorText=scalar(embedded)||scalar(embeddedObject.message)||scalar(embeddedObject.name)||'Инструмент Flow вернул результат без медиа-объекта';
    const error=Error(errorText);
    error.flow={
     name:scalar(result?.name)||scalar(embeddedObject.name)||'FlowProtocolError',
     message:errorText,
     code:result?.code||embedded?.code||'FLOW_PROTOCOL_INVALID',
     cause:scalar(result?.cause)||scalar(embeddedObject.cause)
    };
    p.reject(error);
   }else {
    if(p.timedOut&&p.owner?.job?.requestId===m.requestId){
     p.owner.result=result;save();
     tell('Поздний результат Flow сохранён в мосте. Нажмите продолжение для загрузки в ФФ.');
    }
    p.resolve(result);
   }
  }
  if(m.type==='scene_video_result'){
   clearTimeout(request.timer);const p=request;pending.delete(m.requestId);
   const result=m.result||{
    mediaId:m.flow_media_id||m.mediaId,
    base64:m.base64,
    mimeType:m.mimeType||'video/mp4',
    source_image_asset_id:m.source_image_asset_id,
    source_image_sha256:m.source_image_sha256,
    scene_revision:m.scene_revision,
    idempotency_key:m.idempotency_key
   };
   if(p.timedOut&&p.owner?.job?.requestId===m.requestId){p.owner.result=result;save();tell('Поздний результат видео сохранён. Нажмите продолжение для загрузки в ФФ.');}
   p.resolve(result);
  }
  if(m.type==='error'||m.type==='scene_video_error'){
   clearTimeout(request.timer);const p=request;pending.delete(m.requestId);
   // The Custom Tool can return a structured SDK exception.  Never let the
   // browser coerce it to "[object Object]"; retain only safe diagnostic fields.
   const safeError=value=>{
    if(typeof value==='string')return value;
    if(!value||typeof value!=='object')return value==null?'':String(value);
    const fields=[value.message,value.name,value.code||value.status,value.reason].filter(v=>typeof v==='string'||typeof v==='number');
    return fields.length?fields.join(': '):'Структурированная ошибка Flow без текстового сообщения';
   };
   const errorText=safeError(m.error);
   const details=[safeError(m.name),errorText,m.code?`код ${m.code}`:'',m.cause?`причина: ${safeError(m.cause)}`:''].filter(Boolean).join('; ');
   const error=Error(details||'Неизвестная ошибка Flow');
   const structured=m.error&&typeof m.error==='object'?m.error:{};
   error.flow=p.jobKind==='scene_video'?normalizeVideoFlowError(m.error,m):{name:safeError(m.name||structured.name),message:errorText,code:m.code||structured.code||null,cause:safeError(m.cause||structured.cause),remoteReply:true};
   if(p.jobKind==='scene_video')error.message=error.flow.message;
   if(confirmedRemoteFailure(error) && p.owner?.job?.requestId===m.requestId) {
     p.owner.job.remoteFailure={requestId:m.requestId,code:error.flow.code,message:error.message};
     p.owner.job.submissionPhase='rejected';save();
   }
   p.reject(error);
  }
  if(m.type==='scene_video_progress'){tell(`ФФ: Veo генерация кадра ${m.scene_id||''} (${m.status||'в процессе'})...`);}
  if(m.type==='diagnostic'){
   if(state){
    state.diagnostics=Array.isArray(state.diagnostics)?state.diagnostics:[];
    state.diagnostics.push({at:new Date().toISOString(),requestId:m.requestId,phase:m.phase||'unknown',modelDisplayName:m.modelDisplayName||'',aspectRatio:m.aspectRatio||'',referenceCount:m.referenceCount??null,referenceKinds:m.referenceKinds||[],activeCount:m.activeCount??null,imageConcurrency:m.imageConcurrency??null});
    state.diagnostics=state.diagnostics.slice(-40);save();
   }
  }
 });
 function videoBridgeJob(job){
  const omni=job.model==='gemini-omni-1.1-flash'||job.engine==='omni_flash'||job.modelDisplayName==='Omni 1.1 Flash';
  return {...job,modelDisplayName:omni?'Omni 1.1 Flash':'Veo 3.1 - Lite',
   duration_seconds:job.duration_seconds||(omni?6:5),
   aspectRatio:job.aspectRatio||job.aspect_ratio||'16:9'};
 }
 function generate(job,type='generate',owner=state) {
  if(type==='scene_video'||job.kind==='scene_video')job=videoBridgeJob(job);
  return new Promise((resolve,reject)=>{
   if(!peer||!peerOrigin){
    reject(Error('Инструмент Flow не подключён.'));
    return;
   }
   if(type==='generate'&&String(job.prompt||'').length>4000){
    if(owner?.job?.requestId===job.requestId){owner.job.submissionPhase='not_sent';save();}
    reject(Error('Промпт превышает предел Flow 4000 символов. Запрос не отправлен; условия сохранены.'));return;
   }
   if(pending.has(job.requestId)){reject(Error('Этот запрос уже ожидает ответ Flow.'));return;}
   console.log('[FF Bridge] Sending message to tool:', type, job);
   const timeoutMs=type==='ping'?5000:type==='import_source_image'?30000:240000;
   const timer=setTimeout(()=>{
    const request=pending.get(job.requestId);
    if(request)request.timedOut=true;
    if(type==='ping')reject(Error('Адаптер Flow не ответил на ping (проверьте код инструмента)'));
    else if(type==='import_source_image')reject(Error('Flow не ответил на импорт исходного кадра (30 сек)'));
    else reject(Error('Flow не ответил за 4 минуты. Проверьте результат в Flow.'));
   },timeoutMs);
   pending.set(job.requestId,{id:job.requestId,resolve,reject,timer,owner,timedOut:false,type,jobKind:job.kind});
   if(type==='generate')tell(`ФФ: запросов Flow в работе ${pending.size} / ${adapterImageConcurrency}; кадр ${job.position||job.id||''}`);
   try {
    if(['generate','scene_video'].includes(type)&&owner?.job?.requestId===job.requestId){owner.job.submissionPhase='dispatching';save();}
    peer.postMessage({channel:CHANNEL,type,...job},peerOrigin);
    if(['generate','scene_video'].includes(type)&&owner?.job?.requestId===job.requestId){owner.job.submissionPhase='sent';save();}
   } catch(error) {
    clearTimeout(timer);pending.delete(job.requestId);
    if(['generate','scene_video'].includes(type)&&owner?.job?.requestId===job.requestId){owner.job.submissionPhase='not_sent';save();}
    reject(error);
   }
  });
 }
 async function uploadSaved(currentState=state) {
  if(!currentState?.result||!currentState?.job)return;
  const {kind,id,scene_id,idempotency_key,scene_revision,source_image_asset_id,source_image_sha256}=currentState.job;
  const sid=scene_id||id;
  const result=currentState.result;
  console.log('[FF Bridge] Uploading saved result for kind:', kind, sid);
  if(kind==='scene_video'){
   if(!result.base64||!/^video\/(mp4|webm)$/.test(result.mimeType))throw Error('Некорректный результат Flow видео');
   const filename=result.filename||(result.mimeType==='video/mp4'?'video.mp4':'video.webm');
   await api('POST',`/api/scenes/${encodeURIComponent(sid)}/video`,{
    video:`data:${result.mimeType};base64,${result.base64}`,
    filename,
    flow_media_id:result.mediaId||null,
    idempotency_key:idempotency_key||null,
    expected_scene_revision:scene_revision||null,
    source_image_asset_id:source_image_asset_id||null,
    source_image_sha256:source_image_sha256||null
   });
   if(result.mediaId)currentState.refs[`scene_video:${sid}`]=result.mediaId;
   const doneKey=idempotency_key?`scene_video:${sid}:${idempotency_key}`:`scene_video:${sid}`;
   if(!currentState.done.includes(doneKey))currentState.done.push(doneKey);
   currentState.job=null;currentState.result=null;save();
   return;
  }
  if(!result.base64||!result.mediaId||!/^image\/(png|jpeg|webp)$/.test(result.mimeType))throw Error('Некорректный результат Flow');
  const ext=result.mimeType==='image/jpeg'?'jpg':result.mimeType==='image/webp'?'webp':'png';
  const filename=result.filename||`${kind}_${id}.${ext}`;
  let uploadRes;
  try {uploadRes=await api('POST',`/api/${kind}/${encodeURIComponent(id)}/upload`,{
   image:`data:${result.mimeType};base64,${result.base64}`,
   flow_media_id:result.mediaId,
   generation_id:currentState.job.casting_generation_id||null,
   request_id:currentState.job.requestId,
   lock:currentState.job.casting_generation_id?false:true,
   fidelity_candidate_id:currentState.job.requestId,
   admission_request_id:currentState.job.admissionEnabled?currentState.job.requestId:null,
   expected_scene_revision:currentState.job.scene_revision||null,
   filename
  });} catch(error) {
    if(kind==='characters' && error?.code==='PORTRAIT_REJECTED') {
      currentState.rejectedPortraits=currentState.rejectedPortraits||{};
      currentState.rejectedPortraits[id]={requestId:currentState.job.requestId,generation_id:currentState.job.casting_generation_id||null,reason:String(error.message),at:new Date().toISOString()};
      currentState.job=null;currentState.result=null;currentState.error=null;save();
    }
    throw error;
  }
  // A rejected composition must never be cached as a completed Flow reference.
  currentState.refs[`${kind}:${id}`]=result.mediaId;
  if(uploadRes?.asset_id){
   currentState.refAssets=currentState.refAssets&&typeof currentState.refAssets==='object'?currentState.refAssets:{};
   currentState.refAssets[`${kind}:${id}`]=uploadRes.asset_id;
   if(currentState.projectId&&currentState.flowProject){
    currentState.refs[`ref:${currentState.projectId}:${currentState.flowProject}:${uploadRes.asset_id}`]=result.mediaId;
   }
  }
  if(!currentState.done.includes(`${kind}:${id}`))currentState.done.push(`${kind}:${id}`);currentState.job=null;currentState.result=null;save();
 }
  function handleSourceRejection(error, state, save, tell, position) {
    if(error?.code !== 'SOURCE_IMAGE_REJECTED' || !error.retry || !state.job || state.job.kind !== 'scenes') return false;
    const retry = error.retry;
    const key = `scenes:${state.job.scene_id || state.job.id}`;
    state.result = null;
    if(retry.exhausted) {
      state.blockedScenes = state.blockedScenes || {};
      state.blockedScenes[key] = {position, reason: error.message, attempts: retry.attempts, at: new Date().toISOString()};
      state.job = null; state.error = null; save();
      tell(`Кадр ${position}: ${retry.attempts} вариантов не прошли смысловую проверку. Пропускаю и продолжаю очередь.`);
      return true;
    }
    state.job.originalPrompt = state.job.originalPrompt || state.job.prompt;
    state.job.prompt = 'SOURCE FIDELITY CORRECTION (visual instructions, never printed in the image): '
      + String(retry.correction || '').slice(0,1800) + '\n' + state.job.originalPrompt;
    state.job.sourceAttempts = retry.attempts;
    state.job.retryPending = true;
    state.job.requestId = crypto.randomUUID();
    state.job.requestProfile = requestProfile(state.job);
    state.error = null; save();
    tell(`Кадр ${position}: исправлен промпт по причине отказа Gemini, создаю вариант №${retry.attempts + 1} из ${retry.max_attempts}.`);
    return true;
  }

  async function processBridgeItem(item, kind, options = {}) {
    const {
      project_id,
      sceneIds,
      state,
      save,
      tell,
      api,
      generate,
      ensureReferenceMediaIdFn = ensureReferenceMediaId,
      createClientCompositeReferenceFn = createClientCompositeReference,
      generateImageFn = generateImageWithModelFailover,
      uploadSavedFn = uploadSaved,
      CHANNEL = 'default',
      MAX_SCENE_RETRIES = 2,
      MAX_SDK_SCENE_RETRIES = 1,
      sleepFn = ms => new Promise(resolve => setTimeout(resolve,ms)),
      isPaused = () => false
    } = options;

    if (isPaused()) return { status: 'paused' };
    const id = kind === 'scenes' ? item.scene_id : item.id;
    if (kind === 'scenes' && sceneIds && !sceneIds.includes(id)) return { status: 'skipped_filtered' };

    const alreadyDone = state.done.includes(kind + ':' + id) && !item.casting_generation_id
      && !(options.batchStage === 'portraits' && kind === 'characters');
    if (kind === 'scenes') {
      if (item.has_image) return { status: 'already_has_image' };
      if (alreadyDone || state.refs['scenes:' + id]) {
        state.done=state.done.filter(done=>done!==kind+':'+id);
        delete state.refs['scenes:'+id];
        save();
      }
    } else {
      if (item.locked) {
        try {
          await ensureReferenceMediaIdFn({
            id, type: kind === 'characters' ? 'character' : kind === 'locations' ? 'location' : 'prop',
            name: item.name, asset_id: item.portrait || item.environment || item.prop,
            url: item.portrait_url || item.environment_url || item.prop_url
          });
        } catch(e) {
          console.warn('[FF Bridge] Locked reference auto-import deferred to scene execution:', e);
        }
        return { status: 'reference_locked' };
      }
      if (alreadyDone) return { status: 'already_done' };
    }

    if (kind === 'scenes' && item.source_fidelity_retry?.exhausted) {
      state.blockedScenes = state.blockedScenes || {};
      state.blockedScenes[`scenes:${id}`] = item.source_fidelity_retry;
      save();
      return {status: 'rejected_max_retries', reason: item.source_fidelity_retry.reason};
    }
    if (kind === 'scenes' && item.ready_for_flow === false) {
      const reason = item.flow_error || `Кадр ${item.position}: некорректный контракт состава людей или отсутствует закреплённый референс персонажа.`;
      const sceneKey = `scenes:${id}`;
      state.blockedScenes = state.blockedScenes || {};
      state.blockedScenes[sceneKey] = { position: item.position, reason, at: new Date().toISOString() };
      save();
      tell(`[Пропуск] ${reason}. Перехожу к следующему кадру...`);
      console.warn('[FF Bridge] Skipping scene and continuing queue:', item.position, reason);
      return { status: 'blocked', reason };
    }

    const active = await api('GET', '/api/projects/current');
    if (active.project_id !== project_id) throw Error('Активный проект изменился');

    const refPriority = { character: 0, location: 1, prop: 2 };
    const allRefs = (item.has_image ? [] : (item.references || []))
      .map((ref, index) => ({ ref, index, priority: refPriority[ref.type] ?? 3 }))
      .sort((a, b) => a.priority - b.priority || a.index - b.index);
    if (allRefs.length > 5) throw Error(`Кадр ${item.position}: требуется ${allRefs.length} референсов, а подтверждённый профиль поддерживает не более 5. Сцена не отправлена и персонажи не отброшены.`);

    const characterRefs = allRefs.filter(item => item.ref.type === 'character');
    const requiresComposite=kind==='scenes'&&allRefs.length>2;
    const selectedRefs = requiresComposite
      ? allRefs
      : allRefs.slice(0,2);
    let referenceImageMediaIds = [];
    let referenceKinds = [];
    let isCompositeReference = false;
    let compositeCount = 0;
    let compositeNames = [];

    if(requiresComposite && selectedRefs.length>=2 && kind==='scenes') {
      tell(`Кадр ${item.position}: подготовка составного референса (${selectedRefs.length} сущностей)...`);
      let comp = null;
      try {
        comp = await api('GET', `/api/scenes/${encodeURIComponent(id)}/composite-reference`);
      } catch(e) {
        console.warn('[FF Bridge] Server composite-reference endpoint unavailable, trying client canvas:', e);
      }
      if (!comp || !comp.has_composite || !comp.data_url) {
        comp = await createClientCompositeReferenceFn(item, selectedRefs);
      }
      if (comp && comp.has_composite && comp.data_url) {
        const compKey = `composite_ref:${id}:${comp.sha256}`;
        let compMediaId = state.refs[compKey];
        if (!compMediaId) {
          tell(`Кадр ${item.position}: импорт составного холста в Flow...`);
          const importRes = await generate({
            requestId: crypto.randomUUID(),
            channel: CHANNEL,
            type: 'import_source_image',
            prompt: 'import_composite_reference',
            scene_id: id,
            scene_revision: item.revision || item.current_revision || 1,
            source_image_asset_id: compKey,
            source_image_sha256: comp.sha256,
            imageDataUrl: comp.data_url,
            sourceImageDataUrl: comp.data_url,
            base64: comp.data_url.split(',')[1]
          }, 'import_source_image');
          compMediaId = importRes?.mediaId || importRes?.id || null;
          if (!compMediaId) {
            throw Error(`Кадр ${item.position}: не удалось получить mediaId составного холста в Flow`);
          }
          state.refs[compKey] = compMediaId;
          save();
          tell(`Кадр ${item.position}: составной референс получен (${comp.count} сущностей).`);
        }
        referenceImageMediaIds = [compMediaId];
        referenceKinds = ['composite'];
        isCompositeReference = true;
        compositeCount = comp.count;
        compositeNames = comp.names || selectedRefs.map(r => r.ref.name);
      }
    }

    if (!isCompositeReference) {
      referenceImageMediaIds = [];
      let refImportFailed = false;
      for (const { ref } of selectedRefs) {
        try {
          const mid = options.ensureReferenceMediaIdFn ? await options.ensureReferenceMediaIdFn(ref, item.position) : await ensureReferenceMediaId(ref, item.position);
          if (mid) referenceImageMediaIds.push(mid);
        } catch(err) {
          const formatted = formatStageError(item, 'reference import', err, state?.job);
          addDiagnostic(state?.job, err, 'reference import');
          if (kind === 'scenes') {
            refImportFailed = true;
            state.failedScenes = state.failedScenes || {};
            state.failedScenes[`scenes:${id}`] = { position: item.position, code: 'REF_IMPORT_FAILED', reason: formatted, at: new Date().toISOString() };
            save();
            tell(`Кадр ${item.position}: ошибка импорта референса (${err.message}). Пропускаю и продолжаю очередь...`);
            return { status: 'ref_import_failed', error: formatted };
          }
          throw Error(formatted);
        }
      }
      if (refImportFailed) return { status: 'ref_import_failed' };
      referenceKinds = selectedRefs.map(({ ref }) => ref.type || 'unknown');
    }

    let prompt = item.flow_prompt || item.prompt;
    if (isCompositeReference && compositeNames.length) {
      prompt = `COMPOSITE REFERENCE: Match subjects to labeled panels (${compositeNames.join('; ')}). Maintain identity and setting from sheet.\n` + prompt;
    }
    if (kind === 'scenes' && item.flow_prompt_version !== 'source-scene-wire/1') {
      prompt = String(prompt || '')
        .replace(/ABSOLUTE NO-TEXT RULE[^.]*\./gi, '')
        .replace(/ABSOLUTE NO-TEXT RULE:\s*No readable text anywhere\.[\s\S]*?Never reproduce story text in the image\.?/gi, '')
        .replace(/Render no letters, numbers, captions, subtitles, speech bubbles, dialogue, timestamps,[\s\S]*?Follow the event description only; never render any source prose, quotation or narration as text\./gi, '')
        .replace(/No readable text anywhere\.[\s\S]*?Never reproduce story text in the image\.?/gi, '')
        .trim();
      if (!/In-world text is allowed only when the source explicitly calls for it/i.test(prompt))
        prompt += '\nIn-world text is allowed only when the source explicitly calls for it (for example, a period-appropriate sign, document, letter, map, label or date). Every permitted word must be physically printed, handwritten or engraved on that named object, follow its surface perspective, and use material ink or paint. Never render words as floating, glowing or superimposed typography. Never add subtitles, captions, narration overlays, speech bubbles, dialogue balloons or UI windows.';
      if (!/Never draw sound waves, voice trails/i.test(prompt))
        prompt += '\nRepresent voices, music and other sounds only through visible character reactions or physical effects in the environment. Never draw sound waves, voice trails, audio symbols or decorative graphic echoes.';
      if (!/Full-bleed 16:9 image/i.test(prompt))
        prompt += '\nFull-bleed 16:9 image. The scene must fill the entire canvas edge to edge. No white borders, margins, matting, frames, rounded corners, inset portrait, letterbox or pillarbox.';
    }
    if (prompt && prompt.length > 4000) throw Error('Промпт превышает предел Flow 4000 символов. Условия не обрезаны; запрос не отправлен.');
    if (!id || !prompt) throw Error('Задание без ID или промпта');
    const isSquare = (kind === 'characters' || kind === 'props' || item.aspect_ratio === '1:1');
    const sourceImageAssetId = kind === 'scenes' ? item.image_asset_id || null : null;
    const sourceImageData = sourceImageAssetId ? await loadSourceImageDataUrl(item.image_url) : null;
    const modelDisplayName = state.flowImageModel;
    const resumeSourceRetry = state.job?.kind === kind && state.job?.id === id && (state.job.retryPending || state.job.admissionPending) && !state.result;
    if(!resumeSourceRetry) state.job = { requestId: crypto.randomUUID(), kind, id, position: item.position, prompt, aspectRatio: isSquare ? '1:1' : '16:9', modelDisplayName, referenceImageMediaIds, referenceKinds, sourceImageAssetId, sourceImageDataUrl: sourceImageData, isCompositeReference, compositeCount, retries: 0, submissionPhase:'prepared' };
    state.job.requestProfile = requestProfile(state.job);
    state.job.casting_generation_id = item.casting_generation_id || null;
    state.job.admissionEnabled=kind==='scenes' && item.google_queue_enabled===true;
    state.job.scene_revision=item.revision || item.current_revision || 1;
    try {assertProfileAvailable(state.job);} catch(error) {state.job.submissionPhase='not_sent';save();throw error;}
    save();
    tell(`ФФ ${project_id}: ${kind} ${item.name || item.position}; сохранено ${state.done.length}`);

    for (;;) {
      if(isPaused()) {save(); return {status: 'paused'};}
      if(kind==='characters') {
        if(!state.job.casting_generation_id) {
          const cast=await api('POST',`/api/characters/${id}/cast`,{count:1});
          if(cast.generation_ids?.length!==1)throw Error('Не удалось получить одно задание портрета. Генерация не отправлена.');
          state.job.casting_generation_id=cast.generation_ids[0];save();
        }
        const claimed=await api('POST',`/api/characters/${id}/cast-start`,{generation_id:state.job.casting_generation_id,request_id:state.job.requestId});
        // The server freezes the actual portrait prompt at claim time; never
        // send the old queue manifest or a cached scene-style prefix instead.
        if(claimed.prompt)state.job.prompt=claimed.prompt;
        save();
      }
      try {
        if(state.job.admissionEnabled){
          state.job.admissionPending=true;save();
          const admissionDeadline=Date.now()+60000;
          for(;;){
            if(isPaused()){save();return {status:'paused'};}
            const slot=await api('POST','/api/bridge/v1/admission',{scene_id:id,request_id:state.job.requestId,
              revision:state.job.scene_revision,prompt:state.job.prompt});
            if(slot.allowed)break;
            if(Date.now()>=admissionDeadline)throw Error(`Кадр ${item.position}: ${slot.reason||'Нет свободного разрешения Flow.'} Ожидание остановлено, кадр не пропущен.`);
            if(['fail','uncertain','needs_resolution','error_response','error_access','error_request','transport_unknown'].includes(slot.state))throw Error(slot.reason||'Промпт требует решения. Генерация не запущена.');
            if(['uploaded','rejected','failed','superseded'].includes(slot.state))throw Error('Это задание Flow уже завершено. Новый запрос не отправлен.');
            tell(slot.reason||'Ожидается проверка предыдущих кадров…');
            await sleepFn(2500);
          }
          state.job.admissionPending=false;
        }
        // Commit before submitting: a browser crash during generation must not
        // replay an in-flight corrected request as if it had never been sent.
        state.job.retryPending = false; state.job.submissionPhase='dispatching'; save();
        state.result = await generateImageFn(state.job,{state,save,tell,generate});
      } catch (error) {
        const sdkTerminal = error?.flow?.code === 'ERR_GEN_FAILED' || /\bERR_GEN_FAILED\b/i.test(String(error?.message || error));
        if(kind==='characters' && state.job?.casting_generation_id && sdkTerminal){
          await api('POST',`/api/characters/${id}/cast-failed`,{generation_id:state.job.casting_generation_id,request_id:state.job.requestId,reason:String(error?.message||error)});
          state.job=null;state.result=null;save();
          throw Error('Портрет не создан: Flow отклонил запрос. Нажмите «Перегенерировать» для одного нового варианта.');
        }
        if (kind === 'scenes' && sdkTerminal && state.job.admissionEnabled) {
          await api('POST','/api/bridge/v1/admission/failure',{
            scene_id:id,request_id:state.job.requestId,code:'ERR_GEN_FAILED'});
        }
        if (kind === 'scenes' && sdkTerminal && (state.job.sdkRetries || 0) < MAX_SDK_SCENE_RETRIES && !isPaused()) {
          state.job.sdkRetries = (state.job.sdkRetries || 0) + 1;
          state.job.requestId = crypto.randomUUID();
          state.job.requestProfile = requestProfile(state.job);
          state.result = null; save();
          tell(`Flow отклонил кадр ${item.position}; повторяю тот же запрос (${state.job.sdkRetries}/${MAX_SDK_SCENE_RETRIES}).`);
          await sleepFn(3000);
          continue;
        }
        if (kind === 'scenes' && sdkTerminal) {
          state.failedScenes = state.failedScenes || {};
          state.failedScenes[`scenes:${id}`] = { position: item.position, code: error?.flow?.code || 'ERR_GEN_FAILED', at: new Date().toISOString(), requestId: state.job.requestId };
          state.error = null; state.job = null; state.result = null; save();
          throw Error(`Кадр ${item.position} не создан: Flow вернул ERR_GEN_FAILED после повторов. Очередь остановлена на этом кадре; он не пропущен. Нажмите продолжение для новой попытки.`);
        }
        if(kind==='scenes'&&confirmedRemoteFailure(error)) {
          const failed=recordFailedScene(state,state.job,error);
          state.job.remoteFailure={requestId:state.job.requestId,code:failed.code,message:failed.reason};save();
          await releaseSceneAdmission({api,save},state.job,failed);
          state.job=null;state.result=null;state.error=null;save();
          throw Error(`Кадр ${item.position}: Flow отклонил запрос: ${failed.reason}. Попытка завершена; повтор возможен по кнопке «Старт / продолжить».`);
        }
        const formatted = formatStageError(item, 'Flow generate', error, state.job);
        const wrapped = Error(formatted);
        wrapped.flow = error?.flow;
        throw wrapped;
      }
      save();
      try {
        await uploadSavedFn(state);
        delete state.blockedScenes?.[`scenes:${id}`];
        delete state.failedScenes?.[`scenes:${id}`];
        return { status: 'succeeded', id };
      } catch (error) {
        if(kind==='characters' && error?.code==='PORTRAIT_REJECTED'){state.job=null;state.result=null;save();throw error;}
        if(kind === 'scenes' && handleSourceRejection(error, state, save, tell, item.position)) {
          if(!state.job) return {status: 'rejected_max_retries', reason: error.message};
          continue;
        }
        if (kind !== 'scenes' || !isRejectedComposition(error)) {
          const formatted = formatStageError(item, 'upload to FF', error, state.job);
          throw Error(formatted);
        }
        if (state.job.admissionEnabled) {
          await api('POST','/api/bridge/v1/admission/failure',{
            scene_id:id,request_id:state.job.requestId,code:'COMPOSITION_REJECTED'});
        }
        const reason = String(error?.message || error);
        console.warn('[FF Bridge] Scene candidate rejected:', reason);
        state.result = null;
        if (isPaused()) { state.job = null; save(); return { status: 'paused' }; }
        if (state.job.retries >= MAX_SCENE_RETRIES) {
          const sceneKey = `scenes:${state.job.scene_id || state.job.id}`;
          state.blockedScenes = state.blockedScenes || {};
          state.blockedScenes[sceneKey] = { position: item.position, reason, attempts: state.job.retries + 1, at: new Date().toISOString() };
          state.job = null; save();
          tell(`Кадр ${item.position} пропущен после ${MAX_SCENE_RETRIES + 1} отклонённых вариантов. Продолжаю очередь; кадр можно перегенерировать отдельно после проверки причины.`);
          return { status: 'rejected_max_retries', reason };
        }
        state.job.retries++;
        state.job.requestId = crypto.randomUUID();
        const retryGuidance = ' Keep the shot continuous, preserve the specified characters and their reference appearance, omit subtitle overlays, and ensure the 16:9 scene fills the entire canvas edge to edge with no white borders, frames or matting.';
        if (!state.job.prompt.includes(retryGuidance)) state.job.prompt += retryGuidance;
        state.result = null; save();
        tell(`Кадр ${item.position}: вариант не прошёл проверку, создаю следующий вариант №${state.job.retries + 1}...`);
      }
    }
  }


  function sceneTaskState(root, item) {
    root.sceneTasks=root.sceneTasks||{};
    const key=`${item.scene_id}:${item.revision||item.current_revision||1}`;
    const task=root.sceneTasks[key]||(root.sceneTasks[key]={job:null,result:null,error:null});
    // Only these fields are per-request. References and completion history are
    // shared synchronously; persisting the root never serializes this proxy.
    const local=new Proxy(root,{
      get(target,key){return ['job','result','error'].includes(key)?task[key]:target[key];},
      set(target,key,value){if(['job','result','error'].includes(key))task[key]=value;else target[key]=value;return true;}
    });
    return {local,task,key};
  }
  async function recoverSceneTasks(options) {
    const {state,sceneIds,generationIds,save}=options;
    const recovered=[];
    // v79 could retire a confirmed failure locally without acknowledging its
    // server reservation. Reconcile the durable tombstones, by exact request,
    // before admitting any new work. Never infer failure merely from age.
    const retired=[...Object.values(state.sceneTasks||{}).map(t=>t.failedAttempt),...Object.values(state.failedScenes||{})];
    const seen=new Map();
    for(const failure of retired) {
      if(!failure?.requestId||failure.status!=='failed_remote')continue;
      if(seen.has(failure.requestId)){if(seen.get(failure.requestId).admissionReleased){failure.admissionReleased=true;save();}continue;}
      if(generationIds||(sceneIds&&!sceneIds.includes(failure.sceneId)))continue;
      seen.set(failure.requestId,failure);
      if(await releaseSceneAdmission(options,{id:failure.sceneId,requestId:failure.requestId,admissionEnabled:failure.admissionEnabled},failure))recovered.push(failure);
    }
    for(const task of Object.values(state.sceneTasks||{})) {
      const job=task.job;if(!job||task.result)continue;
      if((sceneIds&&!sceneIds.includes(job.id))||generationIds)continue;
      const acknowledged=job.remoteFailure?.requestId===job.requestId?job.remoteFailure.message:null;
      // Old versions saved explicit provider rejection as an unconfirmed job.
      // Migrate only narrowly recognizable errors, never an arbitrary timeout.
      if(acknowledged||confirmedRemoteFailure(task.error,true)) {
        const reason=acknowledged||task.error;
        task.failedAttempt=recordFailedScene(state,job,reason);save();
        await releaseSceneAdmission(options,job,task.failedAttempt);
        task.job=null;task.result=null;task.error=String(reason);recovered.push(task.failedAttempt);save();
      } else if(['prepared','not_sent'].includes(job.submissionPhase)) {
        task.unsentAttempt={requestId:job.requestId,reason:task.error||'Предыдущий запрос не отправлялся'};save();
        await releaseSceneAdmission(options,job,task.unsentAttempt,'NOT_SENT');
        task.job=null;task.result=null;task.error=null;save();
      }
    }
    if(recovered.length)throw Error(`Кадр ${recovered[0].position}: подтверждён отказ Flow: ${recovered[0].reason}. Старая попытка снята с ожидания; повтор не запускался. Нажмите «Старт / продолжить» для новой попытки.`);
    for(const task of Object.values(state.sceneTasks||{})) {
      if(!task.job)continue;
      if((sceneIds&&!sceneIds.includes(task.job.id))||generationIds)
        throw Error('В мосте сохранены незавершённые другие кадры. Сначала сохраните их результаты.');
    }
    // Save every known result before stopping on a different unknown request.
    for(const task of Object.values(state.sceneTasks||{})) {
      if(!task.job||!task.result)continue;
      const {local}=sceneTaskState(state,{scene_id:task.job.id,revision:task.job.scene_revision});
      try {await options.uploadSavedFn(local);task.error=null;save();}
      catch(error){
        if(handleSourceRejection(error,local,save,options.tell,task.job?.position))continue;
        task.error=String(error.message||error);save();throw error;
      }
    }
    for(const task of Object.values(state.sceneTasks||{})) {
      if(task.job&&!task.result&&!task.job.retryPending&&!task.job.admissionPending)
        throw Error(`Кадр ${task.job.position||task.job.id}: исход запроса Flow неизвестен. Найдите результат в Flow; автоматический повтор остановлен.`);
    }
  }
  async function processScenePool(items,options) {
    const {state,save,tell,isPaused=()=>false}=options;
    const selected=items.filter(item=>!options.sceneIds||options.sceneIds.includes(item.scene_id));
    if(new Set(selected.map(item=>item.scene_id)).size!==selected.length)
      throw Error('В очереди повторяется ID кадра. Генерация не запущена.');
    const limit=Math.max(1,Math.min(4,options.imageConcurrency||1));
    let cursor=0,firstError=null,active=0;
    const singleReference=(ref,position)=>{
      const key=[state.projectId,state.flowProject,ref.type,ref.asset_id||ref.portrait||ref.environment||ref.prop||ref.id].join(':');
      if(!referenceImports.has(key)){
        const promise=Promise.resolve().then(()=>options.ensureReferenceMediaIdFn(ref,position));
        referenceImports.set(key,promise);
        promise.finally(()=>referenceImports.delete(key)).catch(()=>{});
      }
      return referenceImports.get(key);
    };
    async function consume(){
      while(!firstError&&!isPaused()&&cursor<selected.length){
        const item=selected[cursor++];
        const {local,task}=sceneTaskState(state,item);
        active++;tell(`ФФ: изображения в работе ${active} / ${limit}; сохранено ${state.done.length}`);
        try {
          task.error=null;save();
          const result=await processBridgeItem(item,'scenes',{...options,state:local,
            ensureReferenceMediaIdFn:singleReference});
          if(['blocked','sdk_failed','rejected_max_retries','reference_import_failed','ref_import_failed'].includes(result?.status))
            throw Error(`Кадр ${item.position}: ${result.reason||result.error||'Изображение не создано'}`);
        }catch(error){task.error=String(error.message||error);firstError=firstError||error;save();}
        finally{active--;}
      }
    }
    // Drain already launched requests on both pause and error. Never race a
    // rejecting Promise.all against live results that still need to be saved.
    await Promise.all(Array.from({length:Math.min(limit,selected.length)},()=>consume()));
    if(firstError)throw firstError;
  }

  async function processBridgeQueue(options = {}) {
    const {
      project_id,
      sceneIds,
      state,
      save,
      tell,
      api,
      generate,
      isPaused = () => false
    } = options;

    const processRequired = async (item,kind) => {
      const result=await processBridgeItem(item,kind,{...options,isPaused});
      if(['blocked','sdk_failed','rejected_max_retries','reference_import_failed'].includes(result?.status)) {
        throw Error(`Кадр ${item.position||item.name||item.id}: ${result.reason||'Изображение не создано'}. Очередь остановлена; кадр не пропущен.`);
      }
      return result;
    };
    const promptsPath = `/api/projects/${project_id}/prompts`;
    const initial = await api('GET', promptsPath + '?references_only=1');
    if(options.batchStage === 'portraits'){
      for(const item of initial.characters || []){
        if(isPaused())return;
        if(!item.portrait_confirmed && !item.portrait && !item.variants_count)
          await processRequired({...item, locked:false}, 'characters');
      }
      return;
    }
    const readScenes = async () => {
      tell?.('Готовлю промпты сцен в FrameForge…');
      return (await api('GET', promptsPath)).scenes || [];
    };
    if(Array.isArray(options.generationIds) && initial.profile_type!=='forgotten_cities'){
      const wanted=new Set(options.generationIds);
      const portraits=[...(initial.characters||[]),...(initial.manual_characters||[])].filter(i=>wanted.has(i.casting_generation_id));
      if(!portraits.length){tell?.('Портрет уже завершён или отменён. Обновите карточку персонажа.');return;}
      for(const item of portraits){if(isPaused())return;await processRequired(item,'characters');}
      return;
    }
    if (initial.profile_type === 'forgotten_cities') {
      const casting = Array.isArray(options.generationIds);
      const kind = casting ? 'reference_jobs' : 'scenes';
      if(isPaused())return;
      const items = casting ? (initial.reference_jobs || []).filter(i => options.generationIds.includes(i.id)) : await readScenes();
      if(kind==='scenes'&&options.imageConcurrency>1){await processScenePool(items,options);return;}
      for (const item of items) {
        if (isPaused()) return;
        await processRequired(item,kind);
      }
      return;
    }
    const kinds = options.batchStage === 'scenes' ? ['locations', 'props', 'scenes'] : ['characters', 'locations', 'props', 'scenes'];
    for (const kind of kinds) {
      if(isPaused())return;
      // Only explicitly queued manual portraits may supplement the automatic selection.
      const resources = kind === 'scenes' ? await readScenes()
        : kind === 'characters' ? [...(initial.characters || []), ...(initial.manual_characters || [])] : (initial[kind] || []);
      const items = resources.filter(i => kind !== 'props' || !i.importance || i.importance === 'story');
      if(kind==='scenes'&&options.imageConcurrency>1){await processScenePool(items,options);return;}
      for (const item of items) {
        if (isPaused()) return;
        await processRequired(item,kind);
      }
    }
  }

 function isolateCompletedSceneForPortrait(state,generationIds,previousError){
   if(!generationIds || state.job?.kind!=='scenes' || !state.result || !previousError)return false;
   // The image already returned. Preserve it for its own scene; do not regenerate
   // it or let its failed upload block a separately requested portrait.
   state.sceneTasks=state.sceneTasks||{};
   const key=`${state.job.scene_id||state.job.id}:${state.job.scene_revision||1}`;
   state.sceneTasks[key]={job:state.job,result:state.result,error:previousError};
   state.blockedScenes=state.blockedScenes||{};
   state.blockedScenes[`scenes:${state.job.id}`]={reason:previousError,at:new Date().toISOString()};
   state.job=null;state.result=null;
   return true;
 }

 function requestState(root,task){
   return new Proxy(root,{
     get(target,key){return ['job','result','error'].includes(key)?task[key]:target[key];},
     set(target,key,value){if(['job','result','error'].includes(key))task[key]=value;else target[key]=value;return true;}
   });
 }
 function moveRequestOwner(root,task){
   const local=requestState(root,task);
   for(const request of pending.values())if(request.owner===root&&request.id===task.job?.requestId)request.owner=local;
   return local;
 }
 function normalizeVideoFlowError(value,envelope={}){
   const obj=value&&typeof value==='object'?value:{};
   const message=String(obj.message||envelope.message||(typeof value==='string'?value:'Ошибка SDK Flow'));
   const adapterCode=envelope.code||obj.code||null;
   // The legacy hosted adapter invented FLOW_UNKNOWN_ERROR and INTERNAL.
   // These strings are not native evidence of an outage or content policy.
   const legacyFallback=adapterCode==='FLOW_UNKNOWN_ERROR';
   let code=legacyFallback?'FLOW_SDK_UNCLASSIFIED':adapterCode;
   const explicitPublicFigure=/правил[а-я]*[^.]*генераци[а-я]*[^.]*известных людей|policy[^.]*public figures|generation of (?:prominent|famous) people/i.test(message);
   if(explicitPublicFigure)code='FLOW_POLICY_PUBLIC_FIGURE';
   return {name:String(obj.name||envelope.name||'Error'),message,code:code||'FLOW_SDK_UNCLASSIFIED',
     adapterCode,cause:legacyFallback&&obj.cause==='INTERNAL'?'':String(obj.cause||envelope.cause||''),
     codeOrigin:explicitPublicFigure?'provider_message':legacyFallback?'adapter_fallback':obj.codeOrigin||'unreported',
     phase:obj.phase||null,elapsedMs:Number.isFinite(obj.elapsedMs)?obj.elapsedMs:null,remoteReply:true};
 }
 function confirmedVideoFailure(error){
   const text=String(error?.message||error?.flow?.message||error||'');
   if(/timeout|timed out|таймаут|не ответил|no transferable|decode|network|transport/i.test(text))return false;
   return /\b(ERR_GEN_FAILED|FLOW_CAPABILITY_UNSUPPORTED|ERR_UNSUPPORTED_MODEL|FLOW_POLICY_PUBLIC_FIGURE)\b/.test(text+' '+String(error?.flow?.code||''));
 }
 function videoFailureReason(job,error){
   const flow=error?.flow||{};
   const code=flow.code||'NO_PROVIDER_CODE';
   const message=String(flow.message||error?.message||error||'Неизвестная ошибка');
   // Generic SDK exceptions do not establish a moderation decision or a
   // service outage. Keep the uncertainty tied to this exact request.
   const detail=['FLOW_UNKNOWN_ERROR','FLOW_SDK_UNCLASSIFIED','NO_PROVIDER_CODE'].includes(code)?'SDK не передал точную причину. Это не подтверждает недоступность сервиса или отказ по правилам.':`Код: ${code}.`;
   return `Кадр ${job.position}: ${message}. ${detail} Исход видеозапроса не подтверждён; автоматический повтор не запускается.`;
 }
 function captureLateVideoResult(root,message){
   if(!root||!['result','scene_video_result','error','scene_video_error'].includes(message.type))return false;
   const tasks=[root,...Object.values(root.videoTasks||{}),root.deferredSceneWork].filter(Boolean);
   const task=tasks.find(t=>t.job?.kind==='scene_video'&&t.job.requestId===message.requestId);
   if(!task)return false;
   if(['error','scene_video_error'].includes(message.type)){
     const error={flow:{code:message.code||message.error?.code,message:message.error?.message||message.error||message.message,remoteReply:true}};
     task.lastResponse={requestId:message.requestId,code:error.flow.code||null,
       message:String(error.flow.message||''),at:new Date().toISOString()};
     if(!confirmedVideoFailure(error)){task.error=videoFailureReason(task.job,error);return true;}
     task.job.remoteFailure={requestId:message.requestId,code:error.flow.code,message:String(error.flow.message)};
     task.error=String(error.flow.message);return true;
   }
   const result=message.result||message;
   if(!result.base64||!/^video\/(mp4|webm)$/.test(result.mimeType)||result.error)return false;
   if(!task.result)task.result=result;
   return true;
 }
 async function recoverVideoTasks({state,sceneIds,save,api,uploadSavedFn,previousError}){
   state.videoTasks=state.videoTasks||{};
   const recovery=await api('GET',`/api/projects/${state.projectId}/animation/recovery`);
   const currentScenes=new Map((recovery.scenes||[]).map(scene=>[scene.scene_id,scene]));
   if(state.job?.kind==='scene_video'){
     const key=state.job.requestId;
     if(!key)throw Error('У сохранённого видео нет requestId. Проверьте результат в Flow.');
     if(state.videoTasks[key]?.job&&state.videoTasks[key].job!==state.job)throw Error('Найдено несколько записей одного видеозапроса. Результаты сохранены.');
     const task={job:state.job,result:state.result,error:previousError||state.error};
     state.videoTasks[key]=task;moveRequestOwner(state,task);
     state.job=null;state.result=null;state.error=null;save();
   }
   const unresolved=[];
   for(const task of Object.values(state.videoTasks)){
     const job=task.job;if(!job)continue;
     const sid=job.scene_id||job.id;
     if(sceneIds&&!sceneIds.includes(sid))continue;
     const current=currentScenes.get(sid);
     if(current?.position&&!job.position){job.position=current.position;save();}
     if(current?.video_asset_id||(current?.resolved_attempt_keys||[]).includes(job.idempotency_key)){
       task.resolvedAttempt={...job,reason:current.video_asset_id?'video_available':'confirmed_no_result'};
       if(task.result)task.resolvedResult=task.result;
       task.job=null;task.result=null;task.error=null;save();continue;
     }
     if(task.result){await uploadSavedFn(requestState(state,task));task.error=null;save();continue;}
     if(['prepared','not_sent'].includes(job.submissionPhase)){
       await api('POST',`/api/scenes/${sid}/animation/progress`,{
         status:job.model==='gemini-omni-1.1-flash'?'queued_omni':'queued_veo',
         expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
         source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256});
       task.unsentAttempt={...job};task.job=null;task.error=null;save();continue;
     }
     const rejected=job.remoteFailure?.requestId===job.requestId?job.remoteFailure.message:null;
     if(rejected||confirmedVideoFailure(task.error)){
       await api('POST',`/api/scenes/${sid}/animation/progress`,{status:'failed',error:rejected||task.error,
         expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
         source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256});
       task.failedAttempt={...job,error:rejected||task.error};task.job=null;save();continue;
     }
     // Unknown is not a rejection. Preserve this request and exclude its scene
     // from submission while other selected videos may proceed.
     if(task.uncertaintyReportedFor!==job.requestId){
       const evidence=job.lastResponse||task.lastResponse;
       const reason=videoFailureReason(job,evidence?{flow:evidence}:task.error||'Ответ Flow отсутствует');
       await api('POST',`/api/scenes/${sid}/animation/progress`,{
         status:job.model==='gemini-omni-1.1-flash'?'generating_omni':'generating_veo',error:reason,
         expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
         source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256});
       task.uncertaintyReportedFor=job.requestId;save();
     }
     unresolved.push({scene_id:sid,position:job.position,requestId:job.requestId,idempotency_key:job.idempotency_key});
   }
   return unresolved;
 }
 function scopeSavedWork(state,batchStage,animationsOnly=false){
   if(animationsOnly){
     if(!state.job&&state.deferredSceneWork?.job?.kind==='scene_video'){
       Object.assign(state,state.deferredSceneWork);delete state.deferredSceneWork;
     }
     if(state.job&&state.job.kind!=='scene_video'){
       if(state.deferredImageWork?.job)throw Error('Есть две незавершённые задачи изображений. Обе сохранены; проверьте очередь Flow.');
       state.deferredImageWork={job:state.job,result:state.result,error:state.error};moveRequestOwner(state,state.deferredImageWork);
       state.job=null;state.result=null;state.error=null;
     }
     return;
   }
   if(!state.job&&state.deferredImageWork){Object.assign(state,state.deferredImageWork);delete state.deferredImageWork;}
   if(batchStage==='portraits' && state.job && state.job.kind!=='characters'){
     if(state.deferredSceneWork)throw Error('В мосте сохранено несколько незавершённых задач. Проверьте очередь Flow.');
     state.deferredSceneWork={job:state.job,result:state.result,error:state.error};
     state.job=null;state.result=null;state.error=null;
   }else if(batchStage!=='portraits' && !state.job && state.deferredSceneWork){
     Object.assign(state,state.deferredSceneWork);
     delete state.deferredSceneWork;
   }
 }

 async function run(sceneIds=null,generationIds=null,imagesOnly=false,batchStage=null,expectedProjectId=null,animationsOnly=false) {
  if(running)return;
  if(!peer){tell('Откройте инструмент с адаптером v1');return;}
  running=true;pauseRequested=false;
  if(batchStage)tell(batchStage==='portraits'?'Подготовка портретов…':'Подготовка сцен…');
  console.log('[FF Bridge] Running generation queue...');
  try {
   await heartbeat(true);
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)throw Error('В ФФ нет активного проекта');
   if(expectedProjectId && project_id!==expectedProjectId)throw Error('Активный проект ФФ изменился. Повторите запуск из нужного проекта.');
   const flowSettings=await api('GET','/api/settings');
   const flowProject=getFlowProjectId();
   if(!flowProject)throw Error('Откройте проект Google Flow для подключения моста.');
   state=GM_getValue(queueKey(project_id,flowProject),null);
   if(!state){
    const legacy=GM_getValue(STORE,null);
    if(legacy?.projectId===project_id&&legacy?.flowProject===flowProject)state=legacy;
   }
   if(!state)state={projectId:project_id,flowProject,refs:{},refAssets:{},done:[],job:null,result:null,diagnostics:[],sdkFailures:{},sdkCircuits:{}};
   state.refs=state.refs&&typeof state.refs==='object'?state.refs:{};
   state.refAssets=state.refAssets&&typeof state.refAssets==='object'?state.refAssets:{};
   state.diagnostics=Array.isArray(state.diagnostics)?state.diagnostics:[];
   // v0.1.50 adds Lite → standard-model failover.  A circuit written by an
   // older bridge would block the request before that recovery can run.
   // Preserve references, completed frames and diagnostics; discard only its
   // obsolete SDK failure memory once during the state migration.
   if(state.bridgeStateVersion!==BRIDGE_STATE_VERSION){
    state.sdkFailures={};
    state.sdkCircuits={};
    state.bridgeStateVersion=BRIDGE_STATE_VERSION;
   }
   state.sdkFailures=state.sdkFailures&&typeof state.sdkFailures==='object'?state.sdkFailures:{};
   state.sdkCircuits=state.sdkCircuits&&typeof state.sdkCircuits==='object'?state.sdkCircuits:{};
   state.flowImageModel=['🍌 Nano Banana 2 Lite','🍌 Nano Banana 2','🍌 Nano Banana Pro'].includes(flowSettings?.flow_image_model)?flowSettings.flow_image_model:'🍌 Nano Banana 2';
   scopeSavedWork(state,batchStage,animationsOnly);
   // A failure belongs to the previous attempt.  Never show it as the state of
   // a fresh user-started run after the bridge or Flow app has been reloaded.
   const previousError=state.error;
    state.error=null;
    isolateCompletedSceneForPortrait(state,generationIds,previousError);
    const unresolvedVideos=(!batchStage&&!generationIds&&!imagesOnly)?await recoverVideoTasks({state,sceneIds,save,api,uploadSavedFn:uploadSaved,previousError}):[];
    if(state.job&&!state.result&&!state.job.retryPending&&!state.job.admissionPending){
     const staleStaticSync=state.job.kind!=='scene_video'&&state.refs[state.job.kind+':'+state.job.id];
     const retryable=Boolean(staleStaticSync);
     if(!retryable)throw Error('Предыдущая попытка не подтверждена. Проверьте Flow; повторно автоматически не запускаю.');
     state.job=null;state.result=null;save();
     tell('Смываю старую ошибку...');
    }
   save();
   if(!animationsOnly&&batchStage!=='portraits')await recoverSceneTasks({state,sceneIds,generationIds,save,tell,api,uploadSavedFn:uploadSaved});
   // A cached rejected image from an earlier browser session must be discarded.
   // The ordinary scene loop below will generate a fresh candidate.
   if(sceneIds&&state.job?.kind==='scenes'&&!sceneIds.includes(state.job.id)){
    throw Error('В мосте сохранён незавершённый другой кадр. Сначала завершите или разберите его в Flow.');
   }
   if(generationIds&&state.job&&!generationIds.includes(state.job.casting_generation_id||state.job.id))throw Error('В мосте есть другая незавершённая задача. Сначала завершите её.');
   try { await uploadSaved(); }
   catch(error){
    if(handleSourceRejection(error, state, save, tell, state?.job?.position)) {
     // Durable semantic decision: queue loop resumes the saved correction or skips the exhausted scene.
    } else if(state?.job?.kind==='scenes'&&isRejectedComposition(error)){
     const rejected=state.job;
     delete state.refs[`scenes:${rejected.scene_id||rejected.id}`];
     state.job=null;state.result=null;state.error=null;save();
     tell('Кадр отклонён проверкой. Запрашиваю новый вариант вместо остановки очереди...');
    } else throw error;
   }
    if(!animationsOnly)await processBridgeQueue({
      project_id,
      sceneIds,
      generationIds,
      batchStage,
      state,
      save,
      tell,
      api,
      generate,
      ensureReferenceMediaIdFn: ensureReferenceMediaId,
      createClientCompositeReferenceFn: createClientCompositeReference,
      generateImageFn: generateImageWithModelFailover,
      uploadSavedFn: uploadSaved,
      CHANNEL,
      MAX_SCENE_RETRIES,
      MAX_SDK_SCENE_RETRIES,
      imageConcurrency:adapterImageConcurrency,
      isPaused: () => pauseRequested
    });
   if(!pauseRequested&&!generationIds&&!imagesOnly&&!batchStage) {
    const animData=await api('GET',`/api/projects/${project_id}/animation/jobs`);
    const veoJobs=(animData.jobs||[]).filter(job=>(!sceneIds||sceneIds.includes(job.scene_id))&&!unresolvedVideos.some(t=>t.scene_id===job.scene_id));
    console.log('[FF Bridge] Animation jobs from server:', veoJobs);
    if(animData.plan_mode==='economy'&&veoJobs.length>5){
     throw Error('Превышен лимит 5 Veo jobs в экономном режиме');
    }
    const maxVeo=animData.plan_mode==='economy'?5:veoJobs.length;
    let processedVeo=0;
    const failedVideos=[];
    for(const job of veoJobs) {
     if(pauseRequested)break;
     if(processedVeo>=maxVeo)break;

     const active=await api('GET','/api/projects/current');
     if(active.project_id!==project_id)throw Error('Активный проект изменился');

     if(job.requires_direction_refresh===true||(job.prompt_contract&&job.prompt_contract!=='image-motion/3')){
      // Submitted/unknown scenes were excluded by recoverVideoTasks above.
      // Also exclude a saved successful result waiting to be uploaded. Only
      // an entirely unsent scene may receive a new prompt and request key.
      const saved=[state,...Object.values(state.videoTasks||{}),state.deferredSceneWork].filter(Boolean);
      if(saved.some(t=>t.job?.kind==='scene_video'&&(t.job.scene_id||t.job.id)===job.scene_id))
       throw Error(`Кадр ${job.position}: есть сохранённая попытка Flow. Промпт не изменён; сначала сохраните её результат.`);
      tell(`ФФ: готовлю описание движения кадра ${job.position}...`);
      const updated=await api('POST',`/api/projects/${project_id}/animation/prepare-direction`,{
       scene_id:job.scene_id,expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
       source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256,not_sent:true
      });
      if(updated.prompt_contract!=='image-motion/3'||!updated.prompt||!updated.idempotency_key||!Number.isInteger(updated.scene_revision))
       throw Error('ФФ не подтвердил актуальность описания движения. Видеозапрос не отправлен.');
      Object.assign(job,updated);
     }

     // Remote media IDs are handles, not durable source-image identities.
     // A new unsent video attempt always imports its exact current image.
     // Submitted/unknown attempts are recovered above with their original
     // request + media ID; they never pass through this fresh-import path.
     let sceneMediaId=null;
     if(!sceneMediaId){
      if(!job.source_image_asset_id||!job.source_image_sha256){
       throw Error(`Кадр ${job.position}: отсутствует исходное изображение для анимации.`);
      }
      tell(`ФФ: импорт исходного кадра ${job.position} в Flow...`);
      const sourceImageData=await loadSourceImageDataUrl(job.source_image_url||`/api/assets/${job.source_image_asset_id}`,job.source_image_sha256);
      if(!sourceImageData){
       throw Error(`Кадр ${job.position}: не удалось прочитать исходный PNG.`);
      }

      const importRes=await generate({
       requestId:crypto.randomUUID(),
       channel:CHANNEL,
       type:'import_source_image',
       scene_id:job.scene_id,
       scene_revision:job.scene_revision,
       source_image_asset_id:job.source_image_asset_id,
       source_image_sha256:job.source_image_sha256,
       imageDataUrl:sourceImageData,
       sourceImageDataUrl:sourceImageData,
       base64:sourceImageData.split(',')[1]
      },'import_source_image');

      if(!importRes||importRes.source_image_asset_id!==job.source_image_asset_id||importRes.source_image_sha256!==job.source_image_sha256||!importRes.mediaId){
       throw Error(`Ошибка: в Flow импортирован неактуальный исходный кадр ${job.position}`);
      }
      sceneMediaId=importRes.mediaId;
      state.diagnostics=Array.isArray(state.diagnostics)?state.diagnostics:[];
      state.diagnostics.push({at:new Date().toISOString(),phase:'video_source_imported',
       scene_id:job.scene_id,idempotency_key:job.idempotency_key,
       source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256,
       mediaId:sceneMediaId});
      state.diagnostics=state.diagnostics.slice(-40);save();
      tell(`ФФ: исходный кадр ${job.position} импортирован и подтверждён.`);
     }

     // 2. Strict I2V launch
     state.job={
      requestId:crypto.randomUUID(),
      kind:'scene_video',
      type:'scene_video',
      scene_id:job.scene_id,
      id:job.scene_id,
      position:job.position,
      submissionPhase:'prepared',
      scene_revision:job.scene_revision,
      source_image_asset_id:job.source_image_asset_id,
      source_image_sha256:job.source_image_sha256,
      firstFrameImageMediaId:sceneMediaId,
      referenceImageMediaIds:[sceneMediaId],
      model:job.model||'veo-3.1-lite',
      prompt:job.prompt,
      duration_seconds:job.duration_seconds||(job.model==='gemini-omni-1.1-flash'?6:5),
      resolution:job.resolution||(job.model==='gemini-omni-1.1-flash'?'720p':undefined),
      aspect_ratio:job.aspect_ratio||'16:9',
      idempotency_key:job.idempotency_key
     };
     save();
     const engineLabel=job.engine==='omni_flash'||job.model==='gemini-omni-1.1-flash'?'Omni 1.1 Flash':'Veo 3.1 Lite';
     await api('POST',`/api/scenes/${job.scene_id}/animation/progress`,{
      status:job.engine==='omni_flash'||job.model==='gemini-omni-1.1-flash'?'generating_omni':'generating_veo',
      expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
      source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256
     });
     tell(`ФФ: запуск ${engineLabel} I2V для кадра ${job.position} (ожидание ~1 мин)...`);
     console.log('[FF Bridge] Calling generate for scene_video I2V:', state.job);
     try {
      state.result=await generate(state.job,'scene_video');
     } catch(error) {
      if(error?.flow?.code==='FLOW_BUSY'){
       state.job.submissionPhase='not_sent';save();
       await api('POST',`/api/scenes/${job.scene_id}/animation/progress`,{
        status:job.engine==='omni_flash'||job.model==='gemini-omni-1.1-flash'?'queued_omni':'queued_veo',
        expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
        source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256
       });
       throw Error('Flow занят предыдущим запросом. Новый видеозапрос не запущен; продолжите после его завершения.');
      }
      // A remote error is an adapter reply, not proof that no video exists.
      // Park this request; a transport timeout still stops for recovery.
      const code=error?.flow?.code||error?.code||(/\bERR_GEN_FAILED\b/.test(error?.flow?.message||error?.message||'')?'ERR_GEN_FAILED':null);
      if(pauseRequested)throw error;
      if(!confirmedVideoFailure(error)){
       const reason=videoFailureReason(job,error);
       if(state.job)state.job.lastResponse={requestId:state.job.requestId,code:code||null,
         message:String(error?.flow?.message||error?.message||error),remoteReply:error?.flow?.remoteReply===true,at:new Date().toISOString()};
       save();
       if(error?.flow?.remoteReply!==true){
        await api('POST',`/api/scenes/${job.scene_id}/animation/progress`,{
         status:job.engine==='omni_flash'||job.model==='gemini-omni-1.1-flash'?'generating_omni':'generating_veo',
         error:reason,expected_scene_revision:job.scene_revision,idempotency_key:job.idempotency_key,
         source_image_asset_id:job.source_image_asset_id,source_image_sha256:job.source_image_sha256
        });
        throw Error(reason);
       }
       const parked=await recoverVideoTasks({state,sceneIds:[job.scene_id],save,api,
         uploadSavedFn:uploadSaved,previousError:reason});
       unresolvedVideos.push(...parked);processedVeo++;
       tell(`ФФ: исход запроса кадра ${job.position} не подтверждён. Попытка сохранена; продолжаю остальные выбранные видео.`);
       continue;
      }
      addDiagnostic(state.job,error,'video_sdk_error');
      const reason=`${engineLabel}: ${error?.flow?.message||error?.message||code} (${code})`;
      await api('POST',`/api/scenes/${job.scene_id}/animation/progress`,{
       status:'failed',error:reason,expected_scene_revision:job.scene_revision,
       idempotency_key:job.idempotency_key,source_image_asset_id:job.source_image_asset_id,
       source_image_sha256:job.source_image_sha256
      });
      state.failedVideoJobs=state.failedVideoJobs||{};
      state.failedVideoJobs[job.idempotency_key]={scene_id:job.scene_id,position:job.position,code,at:new Date().toISOString()};
      state.job=null;state.result=null;state.error=null;save();
      failedVideos.push(job.position);processedVeo++;
      tell(`ФФ: кадр ${job.position} получил отказ ${code}. Продолжаю остальные видео.`);
      continue;
     }
     console.log('[FF Bridge] Video generation result received:', state.result);
     save();
     tell(`ФФ: сохранение видео кадра ${job.position} в FrameForge...`);
     await uploadSaved();
     processedVeo++;
     tell(`ФФ: кадр ${job.position} успешно сгенерирован и сохранён!`);
    }
    if(failedVideos.length){
     tell(`Очередь видео завершена. Не созданы кадры: ${failedVideos.join(', ')}. Готовые клипы сохранены.`);
     return;
    }
   }
   if(!pauseRequested){
    if(unresolvedVideos.length)tell(`Остальные выбранные видео обработаны. Неподтверждённых попыток: ${unresolvedVideos.length} (кадры ${unresolvedVideos.map(t=>t.position||t.scene_id).join(', ')}). Их повтор не запускался; проверьте результаты в Flow.`);
    else if(batchStage==='portraits')tell('Портреты готовы к проверке. Проверьте их в FrameForge и запустите сцены той же кнопкой.');
    else tell(generationIds?'Эталон сохранён. Мост подключён и ждёт следующую команду.':'Очередь завершена. Мост подключён и ждёт следующую команду.');
   }else{
    tell('Пауза. Текущий кадр сохранён');
   }
  }catch(e){
   console.error('[FF Bridge Error]', e);
   if(state){addDiagnostic(state.job,e);state.error=e.message;save();}
   tell(e.message);
  }finally{
   running=false;
   void heartbeat();
  }
 }
 async function heartbeat(claimAdapter=false) {
  if(heartbeatBusy)return;
  heartbeatBusy=true;
  try {
   const flowProject=getFlowProjectId();
   if(!flowProject)return;
   const {project_id}=await api('GET','/api/projects/current');
   if(!project_id)return;
   const response=await api('POST','/api/bridge/v1/heartbeat',{
    project_id,flow_project_id:flowProject,
    adapter_connected:Boolean(peer),running,status:status.textContent,ack_command_id:ackCommandId,
    adapter_instance_id:adapterInstanceId,script_version:'0.1.89',scoped_scene_start:true,reference_casting:true,character_casting:true,quota_admission:true,images_only:true,animations_only:true,animation_direction_refresh:true,animation_direction_contract:'image-motion/3',video_recovery:true,staged_image_batch:true,image_concurrency:adapterImageConcurrency,active_image_requests:[...pending.values()].filter(p=>p.type==='generate'&&p.jobKind!=='scene_video').length,claim_adapter:claimAdapter
   });
   const command=response.command;
   if(command&&command.id!==processedCommandId&&command.project_id===project_id
      && !(command.action==='start'&&running)){
    processedCommandId=command.id;ackCommandId=command.id;
    if(command.action==='start')void run(Array.isArray(command.scene_ids)?command.scene_ids:null,Array.isArray(command.generation_ids)?command.generation_ids:null,command.images_only===true,command.batch_stage,command.project_id,command.animations_only===true);
    if(command.action==='pause'&&running){pauseRequested=true;tell('Пауза запрошена из ФФ: сохраняю текущий кадр');}
    if(command.action==='check'){
     tell('Проверяю и привязываю мост к выбранному проекту ФФ...');
     setTimeout(()=>void heartbeat(true),0);
    }
   }
  }catch(e){if(!running)tell('ФФ недоступен: '+e.message);}
  finally{heartbeatBusy=false;}
 }
 // Refresh the adapter lease immediately when returning from reference selection.
 document.addEventListener?.('visibilitychange',()=>{if(!document.hidden)void heartbeat();});
 setInterval(heartbeat,2500);
 void heartbeat();
 check.addEventListener('click',async()=>{
  if(running)return;
  if(pending.size){tell('Ожидаются результаты Flow. Сначала сохраните незавершённые запросы.');return;}
  if(!peer){tell('Адаптер Flow ещё не подключён');return;}
  try{
   await heartbeat(true);
   await generate({requestId:crypto.randomUUID()},'ping');
   const current=await api('GET','/api/projects/current');
   tell('Связь Flow ↔ мост ↔ ФФ проверена. Проект: '+(current.project_id||'не выбран'));
  }catch(e){tell(e.message);}
 });
 start.addEventListener('click',()=>{
  if(running)return;
  if(pending.size){tell('Ожидаются результаты Flow. Сначала сохраните незавершённые запросы.');return;}
  return run();
 });
 pause.addEventListener('click',()=>{if(running){pauseRequested=true;tell('Пауза запрошена: сохраняю текущий кадр');}});
 reset.addEventListener('click',async()=>{
  if(running||pending.size||state?.job||state?.result||state?.deferredImageWork||state?.deferredSceneWork||Object.values(state?.videoTasks||{}).some(task=>task.job||task.result)||Object.values(state?.sceneTasks||{}).some(task=>task.job||task.result)){tell('Есть незавершённые запросы. Сохраните результаты перед сбросом.');return;}
  try{
   const {project_id}=await api('GET','/api/projects/current');
   const flowProject=getFlowProjectId();
   if(!flowProject)throw Error('Откройте проект Google Flow для подключения моста.');
   if(project_id)GM_setValue(queueKey(project_id,flowProject),null);
   GM_setValue(STORE,null);
   state=null;pending.clear();running=false;
   tell('Кэш моста сброшен. Нажмите «ФФ: старт / продолжить».');
  }catch(e){tell('Ошибка сброса: '+e.message);}
 });

  if (typeof globalThis !== 'undefined') {
    globalThis.FFBridgeQueue = {
      normalizeVideoFlowError,confirmedVideoFailure,videoFailureReason,
      scopeSavedWork,recoverVideoTasks,captureLateVideoResult,
      getFlowProjectId,
      sceneTaskState,
      recoverSceneTasks,
      confirmedRemoteFailure,
      processScenePool,
      ensureReferenceMediaId,
      loadSourceImageDataUrl,
      handleSourceRejection,
      processBridgeItem,
      processBridgeQueue,
      isolateCompletedSceneForPortrait,
      isRejectedComposition,
      formatStageError,
      videoBridgeJob
    };
  }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      normalizeVideoFlowError,confirmedVideoFailure,videoFailureReason,
      scopeSavedWork,recoverVideoTasks,captureLateVideoResult,
      getFlowProjectId,
      sceneTaskState,
      recoverSceneTasks,
      confirmedRemoteFailure,
      processScenePool,
      ensureReferenceMediaId,
      loadSourceImageDataUrl,
      handleSourceRejection,
      processBridgeItem,
      processBridgeQueue,
      isolateCompletedSceneForPortrait,
      isRejectedComposition,
      formatStageError,
      videoBridgeJob
    };
  }

})();
