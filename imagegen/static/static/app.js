import {STYLE_PRESETS, MODES, titleFromStory, clampSceneCount, presetForStyle, MAX_SCENES, SECONDS_PER_FRAME, recommendedSceneCount, storyboardTiming, countStoryWords, readingDurationSeconds} from './ui-config.mjs';

const main=document.querySelector('#main'), controls=document.querySelector('#controls');
const notice=document.querySelector('#notice'), dialog=document.querySelector('#editor');
const projectSelect=document.querySelector('#project-select');
let token='',current=null,project=null,view='studio',refreshing=false,currentSettings=null,sessionRefreshPromise=null;
let selectedPreset='realism',selectedMode='characters',selectedCameraMode='cinema',selectedCameraPacing='steady',selectedAspectRatio='16:9',draftStory='',draftTarget=12,draftTargetAuto=true,fixedTiming=false,selectedSubject=null,selectedLocation=null,selectedProp=null;
let lastHoveredCard=null;
let flowBridgeStatus=null;
const picked=new Map();
const labels={queued:'В очереди',preparing:'Подготовка',submitted:'Отправлено',running:'Генерируется',saving:'Сохранение',reconciling:'Проверка',succeeded:'Готово',failed:'Ошибка',cancelled:'Отменено',paused:'Пауза',completed:'Завершено',completed_with_errors:'Есть ошибки',resource_degraded:'Не хватает ресурсов'};
const legacyStoryImportances=new Set(['','story','standard','recurring','key','high','primary']);
const isStoryProp=prop=>legacyStoryImportances.has(String(prop?.importance||'story').trim().toLowerCase());


const CAMERA_MODES = [
  {
    id: 'cinema',
    name: 'Кино',
    icon: '🎬',
    hasNewBadge: false,
    description: 'Стандартный кино-цикл. Полный микс ракурсов по жанру — общие планы, крупные, средние, через плечо. Профессиональная кинематография.',
    bg: 'linear-gradient(rgba(15, 23, 42, 0.65), rgba(15, 23, 42, 0.95)), radial-gradient(circle at 80% 20%, rgba(59, 130, 246, 0.35), transparent 60%)'
  },
  {
    id: 'portrait',
    name: 'Портрет',
    icon: '👤',
    hasNewBadge: false,
    description: 'Фокус на лицах и эмоциях. Средние крупные планы и кадры через плечо доминируют. Для диалогов, персонажных историй, драм.',
    bg: 'linear-gradient(rgba(15, 23, 42, 0.65), rgba(15, 23, 42, 0.95)), radial-gradient(circle at 80% 20%, rgba(236, 72, 153, 0.35), transparent 60%)'
  },
  {
    id: 'panorama',
    name: 'Панорама',
    icon: '⛰️',
    hasNewBadge: false,
    description: 'Масштаб и мир. Широкие и сверхширокие планы доминируют, с крупными якорями для эмоциональной связи. Приключения, фэнтези, пейзажи.',
    bg: 'linear-gradient(rgba(15, 23, 42, 0.65), rgba(15, 23, 42, 0.95)), radial-gradient(circle at 80% 20%, rgba(16, 185, 129, 0.35), transparent 60%)'
  },
  {
    id: 'dynamic',
    name: 'Динамика',
    icon: '⚡',
    hasNewBadge: false,
    description: 'Энергия и быстрая смена. Все 7 типов кадров с драматичными ракурсами — голландский угол как акцент, нижний ракурс для мощи, нарастание к сверхкрупному. Экшн, клипы.',
    bg: 'linear-gradient(rgba(15, 23, 42, 0.65), rgba(15, 23, 42, 0.95)), radial-gradient(circle at 80% 20%, rgba(245, 158, 11, 0.35), transparent 60%)'
  }
];

const CAMERA_PACINGS = [
  { id: 'slow', label: 'МЕДЛЕННЫЙ' },
  { id: 'steady', label: 'РОВНЫЙ' },
  { id: 'fast', label: 'БЫСТРЫЙ' }
];

const ASPECT_RATIO_OPTIONS = [
  { id: '16:9', label: '16:9' },
  { id: '21:9', label: 'КИНО' },
  { id: '9:16', label: '9:16' }
];

function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
function button(text,handler,cls='',testid=''){const node=el('button',text,cls);node.type='button';if(testid)node.dataset.testid=testid;node.addEventListener('click',async()=>{node.disabled=true;try{await handler();}catch(error){showNotice(error.message,true);}finally{node.disabled=false;}});return node;}
function showNotice(text,isError=false){notice.textContent=text;notice.classList.toggle('error',isError);if(text)setTimeout(()=>{if(notice.textContent===text)notice.textContent='';},6000);}
function sectionLabel(text,extra){const h=el('div',undefined,'section-label');h.append(el('span',text));if(extra)h.append(extra);return h;}
function workspace(title,icon='✦'){const box=el('section',undefined,'workspace-section'),head=el('h2',undefined,'workspace-title');head.append(el('i',icon),document.createTextNode(title));box.append(head);return box;}
function isActiveGeneration(generation){return generation&&!['succeeded','failed','cancelled','resource_degraded'].includes(generation.state);}
function activeBatches(){return (project?.batches||[]).filter(batch=>batch.state==='running'||batch.state==='paused');}
function batchControls(compact=false){const batches=activeBatches();if(!batches.length)return null;const actions=el('div',undefined,'inline-actions'+(compact?' compact-actions':''));for(const batch of batches){if(batch.state==='running')actions.append(button('Пауза после текущего кадра',()=>action(`/batches/${batch.id}/pause`,{},'Очередь приостановлена после текущего кадра.'),'small-button',`pause-batch-${batch.id}`));else actions.append(button('Продолжить очередь',()=>action(`/batches/${batch.id}/resume`,{},'Очередь продолжена.'),'small-button',`resume-batch-${batch.id}`));actions.append(button('Отменить очередь',()=>action(`/batches/${batch.id}/cancel`,{},'Очередь отменена.'),'small-button',`cancel-batch-${batch.id}`));}return actions;}
function progressInfo(generation){if(currentSettings?.image_backend==='flow'&&generation?.purpose==='scene'&&generation?.state==='queued')return{percent:0,stage:flowBridgeStatus?.running?'Генерация в Google Flow':'Ожидает запуска в Google Flow',indeterminate:true};const fallback={queued:5,preparing:10,submitted:28,running:38,saving:96,reconciling:22,succeeded:100,failed:0,cancelled:0,resource_degraded:0};const progress=generation?.progress||{};return{percent:Math.max(0,Math.min(100,Number(progress.percent??fallback[generation?.state]??0))),stage:progress.stage||labels[generation?.state]||'Подготовка',indeterminate:Boolean(progress.indeterminate)};}
function progressMeter(generation,compact=false){const info=progressInfo(generation),wrap=el('div',undefined,'progress-meter'+(compact?' compact':'')+(info.indeterminate?' indeterminate':''));wrap.style.margin=compact?'9px 0 3px':'10px 0';const copy=el('div',info.indeterminate?info.stage:`${info.stage} · ${info.percent}%`,'progress-copy');copy.style.cssText='color:#cbd1e0;font-size:12px;margin-bottom:6px';const track=el('div',undefined,'progress-track'),value=el('span');track.style.cssText='height:8px;overflow:hidden;border-radius:999px;background:#252b40;border:1px solid #343b52';value.style.cssText=`display:block;height:100%;min-width:5px;border-radius:inherit;background:linear-gradient(90deg,#6d3dd4,#af7bff);transition:width .5s ease;width:${info.indeterminate?45:info.percent}%`;if(info.indeterminate)value.style.opacity='.82';track.append(value);wrap.append(copy,track);return wrap;}
function renderOperationProgress(){const active=project?.generations.filter(isActiveGeneration)||[];if(!active.length)return;const latest=active[active.length-1],box=el('section',undefined,'operation-progress');box.dataset.testid='operation-progress';box.style.cssText='margin:0 0 16px;padding:15px 17px;border:1px solid #5b438d;border-radius:14px;background:linear-gradient(120deg,#17142b,#101522);box-shadow:0 8px 28px #0004';const info=progressInfo(latest),hint=el('small',info.indeterminate?'Оценка обновится после следующего этапа.':'Прогресс обновляется автоматически.');hint.style.cssText='display:block;margin-top:7px;color:#929bb3;font-size:12px';box.append(el('strong',active.length>1?`В работе задач: ${active.length}`:'Выполняется действие'),progressMeter(latest),hint);main.append(box);}
function modal(title){const body=document.querySelector('#editor-body');body.replaceChildren(el('h2',title));if(!dialog.open)dialog.showModal();return body;}
function field(label,value='',type='input'){const box=el('label'),input=el(type);box.append(el('span',label),input);input.value=value;return{box,input};}
function image(assetId,alt='Результат генерации'){const img=el('img');img.src='/api/assets/'+assetId;img.alt=alt;img.loading='lazy';return img;}
async function refreshSessionToken(){
  if(!sessionRefreshPromise){
    sessionRefreshPromise=fetch('/api/session').then(async response=>{
      if(!response.ok)throw Error('Не удалось восстановить сессию после перезапуска сервера.');
      const result=await response.json();
      if(!result.token)throw Error('Сервер не вернул новый токен сессии.');
      token=result.token;
    }).finally(()=>{sessionRefreshPromise=null;});
  }
  return sessionRefreshPromise;
}
async function api(path,data,retried=false){
  const response=await fetch('/api'+path,{
    method:data===undefined?'GET':'POST',
    headers:data===undefined?{}:{'Content-Type':'application/json','X-Session-Token':token},
    body:data===undefined?undefined:JSON.stringify(data)
  });
  const text=await response.text();
  let result;
  try{
    result=JSON.parse(text);
  }catch(e){
    if(!response.ok)throw Error(`HTTP ${response.status}: Сервер недоступен или перезагружается`);
    throw Error('Некорректный ответ сервера');
  }
  if(!response.ok){
    const message=result.error||`HTTP ${response.status}`;
    // Server restarts rotate the in-memory session token. Recover it once so
    // an animation click does not force the user to reload the whole project.
    if(response.status===403&&!retried&&/недействительная сессия/i.test(message)){
      await refreshSessionToken();
      return api(path,data,true);
    }
    throw Error(message);
  }
  return result;
}
async function refreshFlowBridgeStatus(){
  try{flowBridgeStatus=await api('/bridge/v1/status');}
  catch(error){flowBridgeStatus={connected:false,status:error.message};}
  const label=document.querySelector('#flow-bridge-status');
  if(label){
    const sameProject=flowBridgeStatus.connected&&flowBridgeStatus.project_id===current;
    label.textContent=sameProject
      ? `● Мост подключён${flowBridgeStatus.running?' · генерирует':''}${flowBridgeStatus.status?' · '+flowBridgeStatus.status:''}`
      : flowBridgeStatus.connected?'● Мост открыт для другого проекта ФФ':'○ Мост не подключён. Откройте инструмент Flow.';
  }
}
async function refresh(force=false){if(!current||refreshing)return;refreshing=true;try{const next=await api('/projects/'+current);if(force||JSON.stringify(next)!==JSON.stringify(project)){project=next;if(!selectedSubject||!project.characters.some(c=>c.id===selectedSubject))selectedSubject=project.characters[0]?.id||null;if(!selectedLocation||!project.locations?.some(l=>l.id===selectedLocation))selectedLocation=project.locations?.[0]?.id||null;if(!selectedProp||!project.props?.some(p=>p.id===selectedProp))selectedProp=project.props?.[0]?.id||null;render();}}finally{refreshing=false;}}
async function action(path,data,message='Готово'){const result=await api(path,data);showNotice(message);await refresh(true);return result;}

function readFileAsBase64(file){
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function makeUploadButton(label, uploadEndpoint, successMessage='Изображение успешно загружено!'){
  const fileInput = el('input', undefined, 'card-upload-input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  const btn = button(label, () => fileInput.click(), 'small-button');
  fileInput.addEventListener('change', async () => {
    if (!fileInput.files || !fileInput.files[0]) return;
    try {
      showNotice('⏳ Загрузка изображения...');
      const base64 = await readFileAsBase64(fileInput.files[0]);
      await action(uploadEndpoint, { image: base64 }, successMessage);
    } catch (e) {
      showNotice(e.message, true);
    }
  });
  const wrap = el('span');
  wrap.append(fileInput, btn);
  return wrap;
}

function attachDragAndDrop(element, uploadEndpoint, successMessage='Изображение успешно загружено!'){
  element.addEventListener('dragover', (e) => {
    e.preventDefault();
    element.classList.add('drag-over');
  });
  element.addEventListener('dragleave', () => {
    element.classList.remove('drag-over');
  });
  element.addEventListener('drop', async (e) => {
    e.preventDefault();
    element.classList.remove('drag-over');
    const files = e.dataTransfer?.files;
    if (files && files.length && files[0].type.startsWith('image/')) {
      try {
        showNotice('⏳ Загрузка перетащенного изображения...');
        const base64 = await readFileAsBase64(files[0]);
        await action(uploadEndpoint, { image: base64 }, successMessage);
      } catch (err) {
        showNotice(err.message, true);
      }
    }
  });
}

async function listProjects(){const list=await api('/projects');projectSelect.replaceChildren();const empty=el('option','Открыть проект');empty.value='';projectSelect.append(empty);for(const item of list){const option=el('option',item.title);option.value=item.id;option.selected=item.id===current;projectSelect.append(option);}}
function resetDraft(){current=null;project=null;view='studio';selectedSubject=null;selectedLocation=null;draftStory='';draftTarget=12;draftTargetAuto=true;selectedPreset='realism';selectedMode='characters';selectedCameraMode='cinema';selectedCameraPacing='steady';selectedAspectRatio='16:9';projectSelect.value='';render();}

function render(){document.querySelectorAll('.step').forEach(item=>item.classList.toggle('active',item.dataset.view===view));renderControls();main.replaceChildren();if(!project){renderWelcome();return;}renderOperationProgress();if(view==='queue')renderQueue();else if(view==='export')renderExport();else renderStudio();}

function renderControls(){controls.replaceChildren();const locked=Boolean(project);
 const backendBox=el('section',undefined,'control-section');backendBox.append(sectionLabel('Движок генерации'));
 const switcher=el('div',undefined,'mode-grid');
 const backend=currentSettings?.image_backend||'comfy';
 const btnComfy=button('',async()=>{currentSettings=await api('/settings',{image_backend:'comfy'});showNotice('Выбран локальный генератор (ComfyUI)');render();},'mode-card compact'+(backend==='comfy'?' selected':''),'backend-comfy');
 btnComfy.append(el('span','🖥️'),el('strong','Локальный'));
 const btnFlow=button('',async()=>{currentSettings=await api('/settings',{image_backend:'flow'});showNotice('Выбран Google Flow');render();},'mode-card compact'+(backend==='flow'?' selected':''),'backend-flow');
 btnFlow.append(el('span','🌐'),el('strong','Google Flow'));
 switcher.append(btnComfy,btnFlow);
 backendBox.append(switcher);

  if (backend === 'flow') {
    const flowBtnWrap=el('div');
    flowBtnWrap.style.cssText='margin-top:8px; display:flex; flex-direction:column; gap:8px; background:#101423; padding:10px; border-radius:10px; border:1px solid #283049;';

    const flowModelLabel=el('label',undefined,'flow-model-picker');
    flowModelLabel.append(el('span','Модель генерации'));
    const flowModelSelect=el('select',undefined,'flow-model-select');
    flowModelSelect.id='flow-image-model';
    flowModelSelect.dataset.testid='flow-image-model';
    const flowModels=['🍌 Nano Banana 2 Lite','🍌 Nano Banana 2','🍌 Nano Banana Pro'];
    for(const model of flowModels){
      const option=el('option',model);
      option.value=model;
      option.selected=model===(currentSettings?.flow_image_model||'🍌 Nano Banana 2');
      flowModelSelect.append(option);
    }
    flowModelSelect.addEventListener('change',async()=>{
      const selected=flowModelSelect.value;
      currentSettings=await api('/settings',{image_backend:'flow',flow_image_model:selected});
      showNotice(`Для новых кадров в Flow выбрана модель: ${selected}.`);
    });
    flowModelLabel.append(flowModelSelect);

    const bridgeStatus=el('div','○ Проверка связи с Flow...','muted');
    bridgeStatus.id='flow-bridge-status';
    bridgeStatus.style.cssText='font-size:12px;line-height:1.45';
    if(flowBridgeStatus){
      const sameProject=flowBridgeStatus.connected&&flowBridgeStatus.project_id===current;
      bridgeStatus.textContent=sameProject
        ? `● Мост подключён${flowBridgeStatus.running?' · генерирует':''}${flowBridgeStatus.status?' · '+flowBridgeStatus.status:''}`
        : flowBridgeStatus.connected?'● Мост открыт для другого проекта ФФ':'○ Мост не подключён. Откройте инструмент Flow.';
    }
    const checkBridgeBtn=button('🔌 Проверить мост',async()=>{
      if(!current)throw Error('Сначала откройте проект ФФ.');
      await refreshFlowBridgeStatus();
      if(!flowBridgeStatus.connected)throw Error('Мост Flow не подключён. Откройте FrameForge Bridge по кнопке ниже и дождитесь статуса «Инструмент подключён».');
      showNotice('Проверяю и перепривязываю мост к выбранному проекту ФФ…');
      const command=await api('/bridge/v1/command',{action:'check',project_id:current});
      let attached=false;
      for(let attempt=0;attempt<30;attempt++){
        await new Promise(resolve=>setTimeout(resolve,500));
        await refreshFlowBridgeStatus();
        attached=flowBridgeStatus.connected&&flowBridgeStatus.project_id===current&&flowBridgeStatus.last_ack_command_id===command.command_id;
        if(attached)break;
      }
      showNotice(attached?'Мост проверен и подключён к выбранному проекту ФФ.':'Flow не подтвердил команду проверки. Откройте FrameForge Bridge и дождитесь статуса «Инструмент подключён».',!attached);
    },'small-button','flow-bridge-check');
    const openFlowBtn=button('↗ Открыть FrameForge Bridge',()=>{
      window.open('https://flow.google.com/project/d72dddb5-c45a-4375-8026-d3e5db47d807/tool/5f63be1c-9e20-4da7-958e-f68b20edc979','_blank','noopener');
    },'small-button','flow-bridge-open');
    const startFlowBtn=button('▶ Запустить генерацию в Flow',async()=>{
      if(!current)throw Error('Сначала откройте проект ФФ.');
      await api('/bridge/v1/command',{action:'start',project_id:current});
      showNotice('Команда отправлена в Flow. Генерация начинается в открытом инструменте.');
      await refreshFlowBridgeStatus();
    },'small-button','flow-bridge-start');
    const pauseFlowBtn=button('⏸ Пауза после кадра',async()=>{
      if(!current)throw Error('Сначала откройте проект ФФ.');
      await api('/bridge/v1/command',{action:'pause',project_id:current});
      showNotice('Пауза запрошена: текущий кадр будет сохранён.');
    },'small-button','flow-bridge-pause');
    for(const item of [checkBridgeBtn,openFlowBtn,startFlowBtn,pauseFlowBtn])item.style.cssText='width:100%;padding:8px 10px;border-radius:7px;border:1px solid #465072;background:#232847;color:#fff;font-size:12px;cursor:pointer';
    flowBtnWrap.append(flowModelLabel,bridgeStatus,checkBridgeBtn,openFlowBtn,startFlowBtn,pauseFlowBtn);
    void refreshFlowBridgeStatus();
    backendBox.append(flowBtnWrap);
  }
  controls.append(backendBox);

 const textBackendBox=el('section',undefined,'control-section');textBackendBox.append(sectionLabel('Анализ сценария (ИИ)'));
 const textSwitcher=el('div',undefined,'mode-grid');
 const textBackend=currentSettings?.text_backend||'groq';
 const btnLocalText=button('',async()=>{currentSettings=await api('/settings',{text_backend:'local'});showNotice('Выбран локальный ИИ (Qwen 4B)');render();},'mode-card compact'+(textBackend==='local'?' selected':''),'text-backend-local');
 btnLocalText.append(el('span','🧠'),el('strong','Локальный'));
 const btnGroqText=button('',async()=>{currentSettings=await api('/settings',{text_backend:'groq'});showNotice('Выбран Groq Cloud');render();},'mode-card compact'+(textBackend==='groq'?' selected':''),'text-backend-groq');
 btnGroqText.append(el('span','⚡'),el('strong','Groq Cloud'));
 textSwitcher.append(btnLocalText,btnGroqText);
 textBackendBox.append(textSwitcher);
 controls.append(textBackendBox);

 const styleBox=el('section',undefined,'control-section');styleBox.append(sectionLabel('Выбрать стиль'));
 const styleSelect=el('select',undefined,'style-dropdown-select');
 styleSelect.id='style-select';
 styleSelect.dataset.testid='style-select';
 styleSelect.style.cssText='width:100%; padding:9px 12px; background:#121624; border:1px solid #343d56; border-radius:8px; color:#ffffff; font-size:13px; font-weight:600; cursor:pointer; outline:none; box-sizing:border-box;';
 styleSelect.disabled=locked;
 const activePreset=locked?presetForStyle(project.style).id:selectedPreset;
 for(const preset of STYLE_PRESETS){
   const opt=el('option',`${preset.name} (${preset.subtitle})`);
   opt.value=preset.id;
   opt.dataset.testid=`style-${preset.id}`;
   opt.selected=preset.id===activePreset;
   styleSelect.append(opt);
 }
 styleSelect.addEventListener('change',()=>{
   if(!locked){
     selectedPreset=styleSelect.value;
     render();
   }
 });
 styleBox.append(styleSelect);
 controls.append(styleBox);
 const modeBox=el('section',undefined,'control-section');modeBox.append(sectionLabel('Режим'));const modes=el('div',undefined,'mode-grid');const activeMode=locked?(project.mode||'characters'):selectedMode;
 for(const mode of MODES){const card=button('',()=>{if(!locked){selectedMode=mode.id;render();}},'mode-card'+(mode.id===activeMode?' selected':''),`mode-${mode.id}`);card.disabled=locked;card.append(el('span',mode.icon),el('strong',mode.name),el('small',mode.short));modes.append(card);}modeBox.append(modes,el('p',MODES.find(item=>item.id===activeMode).description,'mode-description'));controls.append(modeBox);
 const storyBox=el('section',undefined,'control-section');storyBox.append(sectionLabel('Сценарий'));const textarea=el('textarea',undefined,'story-input');textarea.placeholder='Вставьте историю или сценарий…';textarea.maxLength=160000;textarea.value=locked?project.story:draftStory;textarea.readOnly=locked;textarea.dataset.testid='story-input';const counter=el('span',`${textarea.value.length.toLocaleString('ru-RU')} / 160 000`,'counter');storyBox.append(textarea,counter);controls.append(storyBox);
 const shownTarget=locked&&project.scenes.length?project.scenes.length:draftTarget;const countBox=el('section',undefined,'control-section');countBox.append(sectionLabel('Количество кадров'));const rangeWrap=el('div',undefined,'range-wrap'),head=el('div',undefined,'range-head'),numInput=el('input');numInput.type='number';numInput.min='1';numInput.max=String(MAX_SCENES);numInput.value=String(shownTarget);numInput.disabled=locked;numInput.style.cssText='width:64px;background:#181c2b;border:1px solid #3d4560;border-radius:6px;color:#fff;text-align:center;font-weight:700;padding:2px 4px;font-size:14px;';numInput.dataset.testid='scene-count-number';head.append(el('span','Кадры (до '+MAX_SCENES+')'),numInput);const range=el('input');range.type='range';range.min='1';range.max=String(MAX_SCENES);range.value=String(shownTarget);range.disabled=locked;range.dataset.testid='scene-count';const auto=button('Авто',()=>{draftTargetAuto=true;draftTarget=recommendedSceneCount(draftStory);range.value=String(draftTarget);numInput.value=String(draftTarget);updateFrameInfo();},'small-button','auto-scene-count');auto.disabled=locked;const recommendation=el('small',undefined,'muted'),metrics=el('div',undefined,'timing-copy');metrics.style.cssText='margin-top:7px;font-size:12px;color:#aeb6cb';const updateFrameInfo=()=>{const story=locked?project.story:textarea.value;const timing=storyboardTiming(story,locked?shownTarget:draftTarget);counter.textContent=`${textarea.value.length.toLocaleString('ru-RU')} / 160 000`;recommendation.textContent=timing.sentences?`Авторасчёт: ${timing.frames} кадров по длительности сценария; визуализация охватывает историю последовательно.`:'Раскадровка появится после добавления сценария.';metrics.textContent=`Omni: ${SECONDS_PER_FRAME} сек/кадр · итоговое видео ${timing.seconds.toLocaleString('ru-RU')} сек · оценка чтения ${Math.ceil(timing.calculatedDuration).toLocaleString('ru-RU')} сек${timing.remainingSentences?` · ${timing.remainingSentences} исходных предложений будут распределены по кадрам`:''}`;numInput.value=String(locked?shownTarget:draftTarget);};textarea.addEventListener('input',()=>{draftStory=textarea.value;if(draftTargetAuto){draftTarget=recommendedSceneCount(draftStory);range.value=String(draftTarget);numInput.value=String(draftTarget);}updateFrameInfo();});range.addEventListener('input',()=>{draftTargetAuto=false;draftTarget=clampSceneCount(range.value);numInput.value=String(draftTarget);updateFrameInfo();});numInput.addEventListener('input',()=>{draftTargetAuto=false;const val=clampSceneCount(numInput.value);draftTarget=val;range.value=String(val);updateFrameInfo();});numInput.addEventListener('change',()=>{numInput.value=String(draftTarget);});rangeWrap.append(head,range,auto,recommendation,metrics);countBox.append(rangeWrap);controls.append(countBox);updateFrameInfo();
 if(!locked){controls.append(button('Сгенерировать',handleGenerateClick,'primary','generate-project'));}
 else if(!project.scenes.length&&!analysisActive()){controls.append(button('Повторить разбор',()=>action(`/projects/${current}/analyse`,{target:draftTarget},'Разбор поставлен в очередь'),'primary','analyse-project'));}
 else if(analysisActive()){const generation=[...project.generations].reverse().find(g=>g.purpose==='analysis'&&isActiveGeneration(g));controls.append(progressMeter(generation));}
}
function imagePath(src,alt){const img=el('img');img.src=src;img.alt=alt;return img;}
function analysisActive(){return project?.generations.some(g=>g.purpose==='analysis'&&isActiveGeneration(g));}
async function handleGenerateClick(){await createAndAnalyse();}
async function createAndAnalyse(){if(!draftStory.trim())throw Error('Добавьте текст сценария.');const preset=STYLE_PRESETS.find(item=>item.id===selectedPreset);const created=await api('/projects',{title:titleFromStory(draftStory),idea:'',story:draftStory,style:preset.prompt,mode:selectedMode});current=created.id;project=created;await api(`/projects/${current}/analyse`,{target:clampSceneCount(draftTarget)});await listProjects();await refresh(true);showNotice('Проект создан. Начинаю разбор сценария.');}

function renderWelcome(){const empty=el('section',undefined,'empty-state'),inner=el('div');inner.append(el('div','✦','orb'),el('h1','Создайте раскадровку'),el('p','Выберите стиль и режим, вставьте сценарий слева и нажмите «Сгенерировать».','muted'));empty.append(inner);main.append(empty);}
function renderHead(subtitle){const head=el('div',undefined,'main-head'),copy=el('div');copy.append(el('h1',project.title),el('div',subtitle,'muted'));head.append(copy);main.append(head);}
function renderStudio(){
  renderHead(`${project.scenes.length||'—'} кадров · ${MODES.find(item=>item.id===(project.mode||'characters'))?.name||'Персонажи'} · ${presetForStyle(project.style).name}`);
  if(!project.scenes.length){renderAnalysisState();return;}
  renderCastDiff();
  renderSubjects();
  renderLocations();
  renderProps();
  renderScenes();
}
function renderAnalysisState(){const box=workspace('Подготовка проекта','◌');const generation=[...project.generations].reverse().find(g=>g.purpose==='analysis');if(!generation)box.append(el('p','Запустите разбор сценария слева.','muted'));else{const line=el('div',undefined,'status-line');if(isActiveGeneration(generation))line.append(el('span',undefined,'pulse'));line.append(el('strong',labels[generation.state]||generation.state));box.append(line,progressMeter(generation));if(generation.error)box.append(el('p',generation.error,'error'));}main.append(box);}

function renderSubjects(){const isIllustration=project.mode==='illustration';const isObjects=project.mode==='objects';const title=isObjects?'Объекты и архитектура':'Персонажи';const box=workspace(title,isObjects?'◇':'♙');if(isIllustration||!project.characters.length){box.append(el('p',isIllustration?'В этом режиме постоянные референсы не используются — каждая сцена иллюстрируется самостоятельно.':'Анализ не нашёл постоянных персонажей или объектов.','muted'));main.append(box);return;}
 const tabs=el('div',undefined,'subject-tabs');for(const subject of project.characters){const tab=button('',()=>{selectedSubject=subject.id;render();},'subject-pill'+(subject.id===selectedSubject?' selected':'')+(subject.locked?' locked':''),`subject-${subject.id}`),avatar=el('span',undefined,'subject-avatar');const preview=subject.references.portrait||latestSubjectAsset(subject.id);if(preview)avatar.append(image(preview,subject.name));else avatar.textContent='＋';tab.append(avatar,el('span',subject.name));tabs.append(tab);}box.append(tabs);const subject=project.characters.find(item=>item.id===selectedSubject)||project.characters[0];box.append(renderSubjectDetail(subject,isObjects));main.append(box);}
function latestSubjectAsset(id){return [...project.generations].reverse().find(g=>g.character_id===id&&g.state==='succeeded'&&g.asset_id&&!g.snapshot.wardrobe_key)?.asset_id;}
function subjectAge(subject){return [...project.generations].reverse().find(g=>g.character_id===subject.id&&g.snapshot?.age_constraint)?.snapshot.age_constraint?.label||null;}
function subjectGender(subject){return [...project.generations].reverse().find(g=>g.character_id===subject.id&&g.snapshot?.gender_constraint)?.snapshot.gender_constraint?.label||null;}
function renderSubjectDetail(subject,isObjects){const detail=el('div',undefined,'subject-detail'),head=el('div',undefined,'detail-head'),copy=el('div'),age=subjectAge(subject),gender=subjectGender(subject);copy.append(el('h3',subject.name),el('p',subject.description,'muted'));if(age||gender)copy.append(el('p',`Проверить: ${gender||'персонаж'}${age?`, ${age}`:''}`,'age-hint'));head.append(copy);const actions=el('div',undefined,'inline-actions');
 actions.append(button('📋 Промпт',async()=>{
   const prompts = await api(`/projects/${current}/prompts`);
   const found = prompts.characters.find(c=>c.id===subject.id);
   if(found?.prompt){await navigator.clipboard.writeText(found.prompt);showNotice(`📋 Промпт для ${subject.name} скопирован в буфер!`);}
 },'small-button'));
 actions.append(makeUploadButton(isObjects?'📁 Загрузить объект':'📁 Загрузить портрет',`/characters/${subject.id}/upload`,'Портрет успешно загружен и зафиксирован!'));
 if(!subject.locked){actions.append(button('Изменить описание',()=>editCharacter(subject),'small-button',`edit-character-${subject.id}`));actions.append(button('Перегенерировать 2',()=>action(`/characters/${subject.id}/cast`,{count:2},'Два варианта поставлены в очередь'),'secondary',`regenerate-${subject.id}`));}
 else{actions.append(button('🔓 Сменить вариант',()=>action(`/characters/${subject.id}/unlock`,{},'Фиксация персонажа снята'),'small-button',`unlock-character-${subject.id}`));}
 head.append(actions);detail.append(head);
 attachDragAndDrop(detail, `/characters/${subject.id}/upload`, 'Портрет загружен и зафиксирован!');
 const active=project.generations.find(g=>g.character_id===subject.id&&isActiveGeneration(g));if(active)detail.append(progressMeter(active,true));
 if(subject.locked){const grid=el('div',undefined,'candidate-grid');const asset=subject.references.portrait;if(asset){const card=el('article',undefined,'candidate selected');card.append(image(asset,subject.name));const foot=el('footer');foot.append(el('span',isObjects?'Основной ракурс':'Внешность'),el('span','✓ Зафиксировано','success'));card.append(foot);grid.append(card);}detail.append(grid);return detail;}
 const choices=picked.get(subject.id)||{};picked.set(subject.id,choices);const portraits=project.generations.filter(g=>g.character_id===subject.id&&g.character_revision===subject.revision&&g.state==='succeeded'&&g.asset_id&&!g.snapshot.wardrobe_key).slice(-2);if(!portraits.length)detail.append(el('p','Создайте два варианта локально или загрузите изображение из Google Flow.','muted'));else{const grid=el('div',undefined,'candidate-grid');for(const [index,generation] of portraits.entries()){const card=button('',()=>{choices.portrait=generation.asset_id;render();},'candidate'+(choices.portrait===generation.asset_id?' selected':''),`candidate-${subject.id}-${index}`);card.append(image(generation.asset_id,`${subject.name}, вариант ${index+1}`));const foot=el('footer');foot.append(el('span',age||gender?`Вариант ${index+1} · ${gender||''}${gender&&age?' ':''}${age||''}`:`Вариант ${index+1}`),el('span',choices.portrait===generation.asset_id?'✓ Выбран':'Выбрать'));card.append(foot);grid.append(card);}detail.append(grid);}
 detail.append(button(isObjects?'Зафиксировать объект':'Зафиксировать персонажа',async()=>{if(!choices.portrait)throw Error('Выберите один из вариантов или загрузите файл.');await lockCharacter(subject,choices,isObjects);},'success-button',`lock-${subject.id}`));return detail;}

function lockCharacter(subject,choices,isObjects){
 return action(`/characters/${subject.id}/lock`,{revision:subject.revision,references:choices},'Референс зафиксирован');
}
function renderCastDiff(){
  const diff = project.cast_bible?.diff;
  if(!diff) return;
  const hasAdded = diff.added && diff.added.length > 0;
  const hasModified = diff.modified && diff.modified.length > 0;
  const hasRemoved = diff.removed && diff.removed.length > 0;
  if(!hasAdded && !hasModified && !hasRemoved) return;

  const box = workspace('Изменения в персонажах (Cast Bible Diff)', '⚖');
  const alertBox = el('div', undefined, 'diff-alert-box');
  alertBox.style.cssText = 'background:#1a2333;border:1px solid #3b82f6;border-radius:8px;padding:12px 16px;margin-bottom:16px;';
  alertBox.append(el('p', 'Обнаружены изменения персонажей после обновления текста сценария. Подтвердите или настройте сопоставление:', 'muted'));

  const list = el('div');
  list.style.cssText = 'display:flex;flex-direction:column;gap:10px;margin-top:10px;';

  if(hasAdded){
    for(const a of diff.added){
      const row = el('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;background:#131b26;padding:8px 12px;border-radius:6px;';
      const info = el('div');
      info.append(el('strong', `+ Добавлен: ${a.name} `), el('span', `(${a.reason || 'новый персонаж'})`, 'muted'));
      row.append(info);
      const actions = el('div', undefined, 'inline-actions');
      actions.append(button('Принять', async () => {
        await api(`/projects/${current}/cast-diff/resolve`, {action: 'accept'});
        await refresh(true);
        showNotice(`Персонаж ${a.name} принят`);
      }, 'small-button'));
      row.append(actions);
      list.append(row);
    }
  }

  if(hasModified){
    for(const m of diff.modified){
      const row = el('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;background:#131b26;padding:8px 12px;border-radius:6px;';
      const info = el('div');
      info.append(el('strong', `✎ Изменён: ${m.name} `), el('span', `(${(m.changes || []).join(', ')})`, 'muted'));
      row.append(info);
      const actions = el('div', undefined, 'inline-actions');
      actions.append(button('Принять', async () => {
        await api(`/projects/${current}/cast-diff/resolve`, {action: 'accept'});
        await refresh(true);
        showNotice(`Изменения для ${m.name} приняты`);
      }, 'small-button'));
      actions.append(button('Оставить прежним', async () => {
        await api(`/projects/${current}/cast-diff/resolve`, {action: 'keep_old', cast_id: m.cast_id});
        await refresh(true);
        showNotice(`Прежнее описание ${m.name} сохранено`);
      }, 'small-button'));
      row.append(actions);
      list.append(row);
    }
  }

  if(hasRemoved){
    for(const r of diff.removed){
      const row = el('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;background:#131b26;padding:8px 12px;border-radius:6px;';
      const info = el('div');
      info.append(el('strong', `✕ Не найден в новом тексте: ${r.name} `), el('span', `(${r.reason || 'требуется подтверждение удаления'})`, 'muted'));
      row.append(info);
      const actions = el('div', undefined, 'inline-actions');
      actions.append(button('Оставить персонажа', async () => {
        await api(`/projects/${current}/cast-diff/resolve`, {action: 'keep_old', cast_id: r.cast_id});
        await refresh(true);
        showNotice(`Персонаж ${r.name} сохранён`);
      }, 'small-button'));
      actions.append(button('Удалить', async () => {
        await api(`/projects/${current}/cast-diff/resolve`, {action: 'accept'});
        await refresh(true);
        showNotice(`Персонаж ${r.name} удалён`);
      }, 'small-button'));
      row.append(actions);
      list.append(row);
    }
  }

  alertBox.append(list);
  const foot = el('div');
  foot.style.cssText = 'margin-top:14px;display:flex;gap:10px;';
  foot.append(button('Принять все изменения', async () => {
    await api(`/projects/${current}/cast-diff/resolve`, {action: 'accept'});
    await refresh(true);
    showNotice('Все изменения Cast Bible приняты');
  }, 'primary'));
  alertBox.append(foot);
  box.append(alertBox);
  main.append(box);
}

function editCharacter(subject){
  const body=modal('Редактирование персонажа');
  const nameField=field('Имя персонажа',subject.name,'input');
  const description=field('Пол, возраст и внешность',subject.description,'textarea');
  body.append(nameField.box,el('p','Например: Woman, exactly 30 years old, brown hair, calm expression.','muted'),description.box,button('Сохранить',async()=>{
    await action(`/characters/${subject.id}/edit`,{revision:subject.revision,description:description.input.value,name:nameField.input.value},'Персонаж обновлён.');dialog.close();
  },'primary'));
}

function renderLocations(){const locations=project.locations||[];if(!locations.length)return;const box=workspace('Локации','⌖'),tabs=el('div',undefined,'subject-tabs');for(const location of locations){const tab=button('',()=>{selectedLocation=location.id;render();},'subject-pill'+(location.id===selectedLocation?' selected':'')+(location.locked?' locked':''),`location-${location.id}`),avatar=el('span',undefined,'subject-avatar'),preview=location.references.environment||latestLocationAsset(location.id);if(preview)avatar.append(image(preview,location.name));else avatar.textContent='⌂';tab.append(avatar,el('span',location.name));tabs.append(tab);}box.append(tabs);const location=locations.find(item=>item.id===selectedLocation)||locations[0];box.append(renderLocationDetail(location));main.append(box);}
function latestLocationAsset(id){return [...project.generations].reverse().find(g=>g.location_id===id&&g.state==='succeeded'&&g.asset_id)?.asset_id;}
function renderLocationDetail(location){const detail=el('div',undefined,'subject-detail'),head=el('div',undefined,'detail-head'),copy=el('div');copy.append(el('h3',location.name),el('p',location.description,'muted'));head.append(copy);const actions=el('div',undefined,'inline-actions');
 actions.append(button('📋 Промпт',async()=>{
   const prompts = await api(`/projects/${current}/prompts`);
   const found = prompts.locations.find(l=>l.id===location.id);
   if(found?.prompt){await navigator.clipboard.writeText(found.prompt);showNotice(`📋 Промпт локации ${location.name} скопирован в буфер!`);}
 },'small-button'));
 actions.append(makeUploadButton('📁 Загрузить референс',`/locations/${location.id}/upload`,'Локация успешно загружена и зафиксирована!'));
 if(!location.locked){actions.append(button('Изменить описание',()=>editLocation(location),'small-button',`edit-location-${location.id}`));actions.append(button('Перегенерировать 2',()=>action(`/locations/${location.id}/cast`,{count:2},'Два варианта локации поставлены в очередь'),'secondary',`regenerate-location-${location.id}`));}
 else{actions.append(button('🔓 Сменить вариант',()=>action(`/locations/${location.id}/unlock`,{},'Фиксация локации снята'),'small-button',`unlock-location-${location.id}`));}
 head.append(actions);detail.append(head);
 attachDragAndDrop(detail, `/locations/${location.id}/upload`, 'Локация загружена и зафиксирована!');
 const active=project.generations.find(g=>g.location_id===location.id&&isActiveGeneration(g));if(active)detail.append(progressMeter(active,true));
 if(location.locked){const grid=el('div',undefined,'candidate-grid'),asset=location.references.environment;if(asset){const card=el('article',undefined,'candidate selected');card.append(image(asset,location.name));const foot=el('footer');foot.append(el('span','Основной вид'),el('span','✓ Зафиксировано','success'));card.append(foot);grid.append(card);}detail.append(grid);return detail;}
 const choices=picked.get('location:'+location.id)||{};picked.set('location:'+location.id,choices);const candidates=project.generations.filter(g=>g.location_id===location.id&&g.location_revision===location.revision&&g.state==='succeeded'&&g.asset_id).slice(-2);if(!candidates.length)detail.append(el('p','Создайте варианты локально или загрузите изображение из Google Flow.','muted'));else{const grid=el('div',undefined,'candidate-grid');for(const [index,generation] of candidates.entries()){const card=button('',()=>{choices.environment=generation.asset_id;render();},'candidate'+(choices.environment===generation.asset_id?' selected':''),`location-candidate-${location.id}-${index}`);card.append(image(generation.asset_id,`${location.name}, вариант ${index+1}`));const foot=el('footer');foot.append(el('span',`Вариант ${index+1}`),el('span',choices.environment===generation.asset_id?'✓ Выбран':'Выбрать'));card.append(foot);grid.append(card);}detail.append(grid);}
 detail.append(button('Зафиксировать локацию',async()=>{if(!choices.environment)throw Error('Выберите вариант локации или загрузите файл.');await action(`/locations/${location.id}/lock`,{revision:location.revision,references:choices},'Локация зафиксирована');},'success-button',`lock-location-${location.id}`));return detail;}
function editLocation(location){const body=modal('Описание локации'),description=field('Постоянные детали: фасад, окна, двери, планировка',location.description,'textarea');body.append(description.box,button('Сохранить',async()=>{await action(`/locations/${location.id}/edit`,{revision:location.revision,description:description.input.value},'Описание локации обновлено. Создайте новые варианты.');dialog.close();},'primary'));}

function renderProps(){
  // Keep non-story records visible so the user can correct classification in
  // either direction. Only story props are sent to generation and references.
  const props=project.props||[];
  if(!props.length)return;
  const box=workspace('Реквизит и классификация','🏺'),tabs=el('div',undefined,'subject-tabs');
  for(const prop of props){
    const tab=button('',()=>{selectedProp=prop.id;render();},'subject-pill'+(prop.id===selectedProp?' selected':'')+(prop.locked?' locked':''),`prop-${prop.id}`);
    const avatar=el('span',undefined,'subject-avatar');
    const preview=prop.references?.item||latestPropAsset(prop.id);
    if(preview)avatar.append(image(preview,prop.name));
    else avatar.textContent='🏺';
    tab.append(avatar,el('span',prop.name));
    tabs.append(tab);
  }
  box.append(tabs);
  const prop=props.find(item=>item.id===selectedProp)||props[0];
  box.append(renderPropDetail(prop));
  main.append(box);
}
function latestPropAsset(id){
  return [...project.generations].reverse().find(g=>g.prop_id===id&&g.state==='succeeded'&&g.asset_id)?.asset_id;
}
function renderPropDetail(prop){
  const detail=el('div',undefined,'subject-detail'),head=el('div',undefined,'detail-head'),copy=el('div');
  const catLabel=prop.category?`Категория: ${prop.category}`:'';
  const story=isStoryProp(prop);
  const importanceLabels={story:'важный для непрерывности',standard:'важный для непрерывности',recurring:'важный для непрерывности',key:'важный для непрерывности',high:'важный для непрерывности',primary:'важный для непрерывности',incidental:'одноразовый',wardrobe:'гардероб',set_dressing:'часть локации'};
  const continuityBadge=el('span',importanceLabels[prop.importance]||importanceLabels.story,'status-pill active-badge');
  continuityBadge.style.cssText='display:inline-block;margin-left:8px;font-size:11px;background:#233554;color:#64ffda;padding:2px 8px;border-radius:10px;';
  const titleWrap=el('div');
  titleWrap.style.cssText='display:flex;align-items:center;';
  titleWrap.append(el('h3',prop.name), continuityBadge);
  copy.append(titleWrap,el('p',prop.description,'muted'));
  if(catLabel)copy.append(el('p',catLabel,'age-hint'));
  head.append(copy);
  const actions=el('div',undefined,'inline-actions');
  if(story)actions.append(button('📋 Промпт',async()=>{
    const prompts=await api(`/projects/${current}/prompts`);
    const found=prompts.props?.find(p=>p.id===prop.id);
    if(found?.prompt){await navigator.clipboard.writeText(found.prompt);showNotice(`📋 Промпт предмета «${prop.name}» скопирован в буфер!`);}
  },'small-button'));
  if(story)actions.append(makeUploadButton('📁 Загрузить предмет',`/props/${prop.id}/upload`,'Предмет успешно загружен и зафиксирован!'));
  if(!prop.locked){
    actions.append(button('Изменить описание',()=>editProp(prop),'small-button',`edit-prop-${prop.id}`));
    if(story)actions.append(button('Перегенерировать 2',()=>action(`/props/${prop.id}/cast`,{count:2},'Два варианта предмета поставлены в очередь'),'secondary',`regenerate-prop-${prop.id}`));
  }else if(story){
    actions.append(button('🔓 Сменить вариант',()=>action(`/props/${prop.id}/unlock`,{},'Фиксация предмета снята'),'small-button',`unlock-prop-${prop.id}`));
  }
  head.append(actions);detail.append(head);
  if(!story){
    detail.append(el('p','Этот объект не участвует в очереди референсов. Измените классификацию на «Сюжетный», чтобы использовать его как постоянный реквизит.','muted'));
    return detail;
  }
  attachDragAndDrop(detail,`/props/${prop.id}/upload`,'Предмет загружен и зафиксирован!');
  const active=project.generations.find(g=>g.prop_id===prop.id&&isActiveGeneration(g));if(active)detail.append(progressMeter(active,true));
  if(prop.locked){
    const grid=el('div',undefined,'candidate-grid'),asset=prop.references?.item;
    if(asset){
      const card=el('article',undefined,'candidate selected');
      card.append(image(asset,prop.name));
      const foot=el('footer');
      foot.append(el('span','Внешний вид предмета'),el('span','✓ Зафиксировано','success'));
      card.append(foot);
      grid.append(card);
    }
    detail.append(grid);return detail;
  }
  const choices=picked.get('prop:'+prop.id)||{};picked.set('prop:'+prop.id,choices);
  const candidates=project.generations.filter(g=>g.prop_id===prop.id&&g.prop_revision===prop.revision&&g.state==='succeeded'&&g.asset_id).slice(-2);
  if(!candidates.length)detail.append(el('p','Создайте варианты локально или загрузите изображение из Google Flow.','muted'));
  else{
    const grid=el('div',undefined,'candidate-grid');
    for(const [index,generation] of candidates.entries()){
      const card=button('',()=>{choices.item=generation.asset_id;render();},'candidate'+(choices.item===generation.asset_id?' selected':''),`prop-candidate-${prop.id}-${index}`);
      card.append(image(generation.asset_id,`${prop.name}, вариант ${index+1}`));
      const foot=el('footer');
      foot.append(el('span',`Вариант ${index+1}`),el('span',choices.item===generation.asset_id?'✓ Выбран':'Выбрать'));
      card.append(foot);
      grid.append(card);
    }
    detail.append(grid);
  }
  detail.append(button('Зафиксировать предмет',async()=>{
    if(!choices.item)throw Error('Выберите вариант предмета или загрузите файл.');
    await action(`/props/${prop.id}/lock`,{revision:prop.revision,references:choices},'Предмет зафиксирован');
  },'success-button',`lock-prop-${prop.id}`));
  return detail;
}
function editProp(prop){
  const body=modal('Свойства сюжетного реквизита'),description=field('Форма, цвет, материал, отличительные признаки',prop.description,'textarea');
  const impBox=el('div');
  impBox.style.cssText='margin-top:10px;margin-bottom:14px;';
  impBox.append(el('label','Важность реквизита:','field-label'));
  const impSel=el('select');
  impSel.style.cssText='display:block;width:100%;margin-top:4px;padding:6px 8px;background:#1e2337;border:1px solid #3d4560;border-radius:6px;color:#fff;font-size:12px;';
  const options=[
    {val:'story', label:'Сюжетный (story) — постоянный реквизит с референсом'},
    {val:'incidental', label:'Одноразовый (incidental) — текст сцены без референса'},
    {val:'wardrobe', label:'Гардероб (wardrobe) — элемент одежды персонажа'},
    {val:'set_dressing', label:'Декор / Мебель (set_dressing) — часть локации'}
  ];
  for(const opt of options){
    const o=el('option',opt.label);
    o.value=opt.val;
    o.selected=(prop.importance||'story')===opt.val;
    impSel.append(o);
  }
  impBox.append(impSel);
  body.append(description.box,impBox,button('Сохранить',async()=>{
    await action(`/props/${prop.id}/edit`,{revision:prop.revision,description:description.input.value,importance:impSel.value},'Реквизит обновлен.');dialog.close();
  },'primary'));
}

function renderScenes(){
  const box=workspace('Сгенерированные кадры','▣'),toolbar=el('div',undefined,'scene-toolbar'),info=el('div');
  const completed=project.scenes.filter(scene=>sceneDisplayGeneration(scene)?.asset_id).length;
  info.append(el('strong',`${completed} из ${project.scenes.length}`),el('div','Готовые изображения появляются здесь автоматически.','muted'));
  toolbar.append(info);
  const storyProps=(project.props||[]).filter(isStoryProp);
  const allLocked=project.characters.every(character=>character.locked)&&(project.locations||[]).every(location=>location.locked)&&storyProps.every(prop=>prop.locked);
  toolbar.append(button('Сгенерировать все кадры',async()=>{if(!allLocked)throw Error('Сначала зафиксируйте все референсы персонажей, локаций и реквизита.');await action(`/projects/${current}/batch`,{idempotency_key:crypto.randomUUID()},'Кадры поставлены в очередь');},'secondary','generate-scenes'));

  // Batch Animation Dropdown (Insaga style)
  const totalCount=project.scenes.length;
  const animatedCount=project.scenes.filter(s=>s.video_asset_id).length;
  const batchAnimateWrap=el('div',undefined,'batch-animate-container');
  const batchBtn=el('button',undefined,'batch-animate-btn');
  batchBtn.type='button';
  batchBtn.innerHTML=`<span>🎬 Анимировать кадры</span><span class="batch-animate-badge">${animatedCount} / ${totalCount}</span>`;

  const popover=el('div',undefined,'batch-popover');
  popover.style.display='none';
  const popHead=el('div',undefined,'batch-popover-head');
  const statusLeft=el('div');
  statusLeft.innerHTML=`Animated <span class="status-dot ${animatedCount>0?'active':''}"></span>`;
  const counterRight=el('div',undefined,'batch-popover-counter');
  counterRight.innerHTML=`<span class="done">${animatedCount}</span> / <span class="total">${totalCount}</span>`;
  popHead.append(statusLeft,counterRight);

  const optionsList=el('div',undefined,'batch-popover-items');
  const optionsData=[
    {id:'smart_hybrid',title:'🧠 Умный гибрид (Сюжетная линия)',count:totalCount,highlight:true,pattern:[1,1,1,0,1,0,1],tag:'Рекомендуется'},
    {id:'all',title:'Анимировать все',count:totalCount,highlight:false,pattern:[1,1,1,1,1,1]},
    {id:'every_2nd',title:'Каждый 2-й кадр',count:Math.ceil(totalCount/2),highlight:false,pattern:[1,0,1,0,1,0]},
    {id:'every_3rd',title:'Каждый 3-й кадр',count:Math.ceil(totalCount/3),highlight:false,pattern:[1,0,0,1,0,0]},
    {id:'first_40',title:'Первые 40 кадров',count:Math.min(totalCount,40),highlight:false,pattern:[1,1,1,1,1]},
    {id:'first_80',title:'Первые 80 кадров',count:Math.min(totalCount,80),highlight:false,pattern:[1,1,1,1,1]},
  ];
  for(const opt of optionsData){
    const item=el('div',undefined,'batch-option-item'+(opt.highlight?' highlight':''));
    const titleEl=el('div',undefined,'batch-option-title');
    titleEl.append(document.createTextNode(opt.title));
    if(opt.tag){
      const tag=el('span',opt.tag,'batch-hybrid-tag');
      tag.style.marginLeft='6px';
      titleEl.append(tag);
    }
    const subEl=el('div',undefined,'batch-option-sub');
    const dotsRow=el('div',undefined,'batch-dots-row');
    for(const p of opt.pattern){
      const dot=el('span',undefined,'batch-dot'+(p?' filled':''));
      dotsRow.append(dot);
    }
    const countLabel=el('span',`${opt.count} frames`,'batch-option-count');
    subEl.append(dotsRow,countLabel);
    item.append(titleEl,subEl);
    item.addEventListener('click',async(e)=>{
      e.stopPropagation();
      popover.style.display='none';
      await handleBatchAnimate(opt.id);
    });
    optionsList.append(item);
  }
  popover.append(popHead,optionsList);
  batchBtn.addEventListener('click',(e)=>{
    e.stopPropagation();
    popover.style.display=popover.style.display==='none'?'block':'none';
  });
  document.addEventListener('click',(e)=>{
    if(!batchAnimateWrap.contains(e.target)){
      popover.style.display='none';
    }
  });
  batchAnimateWrap.append(batchBtn,popover);
  toolbar.append(batchAnimateWrap);

  const controls=batchControls();if(controls)toolbar.append(controls);box.append(toolbar);if(!allLocked&&(project.characters.length||(project.locations||[]).length||(project.props||[]).length))box.append(el('p','Перед пакетной генерацией зафиксируйте референсы персонажей, локаций и реквизита (или загрузите их).','error'));
  
  // Animation planning: Omni is automatic; Veo remains a manual choice.
  const animSettings = project.animation_settings || {plan_mode:'economy', credit_limit:50, max_veo_frames:5};
  const isEconomy = animSettings.plan_mode !== 'manual';
  const veoFrames = (project.scenes||[]).filter(s => s.data?.animation?.selected_engine === 'veo_lite').length;
  const omniFrames = (project.scenes||[]).filter(s => s.data?.animation?.selected_engine === 'omni_flash').length;
  const kbFrames = (project.scenes||[]).filter(s => s.data?.animation?.selected_engine === 'ken_burns').length;
  const totalCredits = veoFrames * 10;

  const animBar = el('div', undefined, 'animation-planning-bar');
  const animLeft = el('div', undefined, 'animation-planning-left');
  
  const modeToggle = el('div', undefined, 'anim-mode-toggle');
  const btnEconomy = el('button', 'Экономный (до 50 кредитов)', 'anim-mode-btn' + (isEconomy ? ' active' : ''));
  const btnManual = el('button', 'Ручной выбор Veo', 'anim-mode-btn' + (!isEconomy ? ' active' : ''));
  btnEconomy.type = 'button';
  btnManual.type = 'button';
  btnEconomy.onclick = async () => {
    await action(`/projects/${current}/animation/plan`, {plan_mode: 'economy'}, 'Включён экономный режим анимации');
  };
  btnManual.onclick = async () => {
    await action(`/projects/${current}/animation/plan`, {plan_mode: 'manual'}, 'Включён ручной режим анимации');
  };
  modeToggle.append(btnEconomy, btnManual);

  const plannedScenes = (project.scenes||[]).filter(s => {
    const st = s.data?.animation?.status;
    return st === 'planned_veo' || st === 'planned_omni_flash' || st === 'planned_ken_burns';
  }).length;
  const queuedScenes = (project.scenes||[]).filter(s => {
    const st = s.data?.animation?.status;
    return st === 'queued_veo' || st === 'generating_veo' || st === 'queued_omni' || st === 'generating_omni';
  }).length;
  const completedScenes = (project.scenes||[]).filter(s => {
    const st = s.data?.animation?.status;
    return st === 'completed' || s.video_asset_id;
  }).length;
  const completedVeoManual = (project.scenes||[]).filter(s => {
    const anim = s.data?.animation;
    return (anim?.status === 'completed' || s.video_asset_id) && anim?.selected_engine === 'veo_lite' && anim?.source === 'flow_manual';
  }).length;
  const completedVeoAuto = (project.scenes||[]).filter(s => {
    const anim = s.data?.animation;
    return (anim?.status === 'completed' || s.video_asset_id) && anim?.selected_engine === 'veo_lite' && anim?.source !== 'flow_manual';
  }).length;
  const spentCredits = completedVeoAuto * 10;
  const waitingVeoScenes = (project.scenes||[]).filter(s => {
    const anim = s.data?.animation;
    return anim?.selected_engine === 'veo_lite' && !(anim?.status === 'completed' || s.video_asset_id);
  });

  const budgetProj = el('div', undefined, 'anim-budget-proj');
  budgetProj.innerHTML = `
    <div class="anim-breakdown" style="font-size:12px;margin-bottom:4px;color:#c9d1d9;">
      <span>Автоматически: <b>${omniFrames} Omni</b> через Google Flow</span> &bull;
      <span>Ручной Veo: <b>${veoFrames}${isEconomy ? ' из 5' : ''}</b> (${totalCredits} кр.)</span> &bull;
      <span>2.5D: <b>${kbFrames}</b></span>
    </div>
    <div class="anim-projection" style="font-size:13px;">
      <span>Прогноз:</span> <span class="credits-val">Omni: ${omniFrames} кадров (стоимость не задана)</span>; <span class="credits-val">Veo: ${veoFrames} × 10 = ${totalCredits} кр.</span>; <span class="kb-val">Ken Burns: ${kbFrames} кадров (0 кр.)</span>
      ${waitingVeoScenes.length ? `<span style="margin-left:8px;color:#fbbf24;">Ожидают видео: кадры <b>${waitingVeoScenes.map(s => s.position).join(', ')}</b></span>` : ''}
    </div>
  `;
  animLeft.append(modeToggle, budgetProj);

  const animActions = el('div', undefined, 'animation-planning-actions');
  const btnPlan = button('Собрать план', async () => {
    await action(`/projects/${current}/animation/plan`, {plan_mode: animSettings.plan_mode || 'economy'}, 'План анимации сформирован');
  }, 'primary', 'btn-animation-plan');
  const btnGenAnim = button('Запустить анимацию', async () => {
    await action(`/projects/${current}/animation/generate`, {}, 'Omni/Veo поставлены в очередь Flow; 2.5D создан локально');
    try {
      await action('/bridge/v1/command', {action: 'start', project_id: current}, 'Задачи Omni/Veo отправлены в Google Flow');
    } catch (error) {
      throw Error('Локальные 2.5D-видео созданы. Omni/Veo ждут подключения FrameForge Bridge в Google Flow; откройте мост и повторите запуск. ' + (error.message || error));
    }
  }, 'secondary', 'btn-animation-generate');
  const btnAssemble = button('Собрать MP4', async () => {
    const res = await action(`/projects/${current}/animation/assemble`, {}, 'Фильм собран!');
    if (res?.url) window.open(res.url, '_blank');
  }, 'secondary', 'btn-animation-assemble');
  animActions.append(btnPlan, btnGenAnim, btnAssemble);

  animBar.append(animLeft, animActions);
  box.append(animBar);

  const grid=el('div',undefined,'scene-grid');for(const scene of project.scenes)grid.append(renderSceneCard(scene));box.append(grid);main.append(box);
}
function sceneGenerations(scene){return project.generations.filter(g=>g.scene_id===scene.id&&g.scene_revision===scene.current_revision);}
function sceneDisplayGeneration(scene){const active=project.generations.find(g=>g.id===scene.active_generation_id);return active||[...sceneGenerations(scene)].reverse().find(g=>g.state==='succeeded'&&g.asset_id);}

let returnFocusElement = null;
function openFullscreenViewer(assetId, caption = '', sourceEl = null) {
  let viewer = document.querySelector('#fullscreen-viewer');
  if (!viewer) {
    viewer = el('div', undefined, 'fullscreen-viewer');
    viewer.id = 'fullscreen-viewer';
    viewer.setAttribute('role', 'dialog');
    viewer.setAttribute('aria-modal', 'true');
    viewer.setAttribute('aria-label', 'Полноэкранный просмотр');
    viewer.tabIndex = -1;

    const closeBtn = el('button', '✕', 'fullscreen-close');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Закрыть просмотр');
    closeBtn.onclick = (e) => { e.stopPropagation(); closeFullscreenViewer(); };

    const content = el('div', undefined, 'fullscreen-content');
    const img = el('img', undefined, 'fullscreen-img');
    img.onclick = (e) => { e.stopPropagation(); closeFullscreenViewer(); };
    const cap = el('div', undefined, 'fullscreen-caption');

    content.append(img, cap);
    viewer.append(closeBtn, content);
    viewer.onclick = (e) => {
      if (e.target === viewer || e.target === content) closeFullscreenViewer();
    };
    document.body.append(viewer);
  }
  returnFocusElement = sourceEl || document.activeElement;
  const img = viewer.querySelector('.fullscreen-img');
  const cap = viewer.querySelector('.fullscreen-caption');
  img.src = '/api/assets/' + assetId;
  img.alt = caption || 'Просмотр кадра';
  cap.textContent = caption;
  viewer.classList.add('open');
  const closeBtn = viewer.querySelector('.fullscreen-close');
  closeBtn?.focus();
}

function closeFullscreenViewer() {
  const viewer = document.querySelector('#fullscreen-viewer');
  if (viewer && viewer.classList.contains('open')) {
    viewer.classList.remove('open');
    if (returnFocusElement && typeof returnFocusElement.focus === 'function') {
      returnFocusElement.focus();
    }
    returnFocusElement = null;
  }
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const viewer = document.querySelector('#fullscreen-viewer');
    if (viewer && viewer.classList.contains('open')) {
      e.stopPropagation();
      closeFullscreenViewer();
    }
  }
});

const KB_MOTION_OPTIONS=[
  ['slow_zoom_in','Плавное приближение'],['slow_pull_back','Плавное отдаление'],['slow_panorama','Панорама слева направо'],
  ['subject_parallax','Мягкий параллакс'],['portrait_push_in','Портретный наезд'],['object_reveal','Раскрытие детали'],['balanced_two_shot','Движение для двух персонажей']
];
const VEO_MOTION_OPTIONS=[['subtle','Плавное движение'],['dynamic','Динамичное действие'],['reaction','Эмоция / реакция'],['pan','Кинонаезд / панорама']];
function selectOptions(options,value,testid,ariaLabel){
  const control=el('select',undefined,'scene-motion-select'); control.dataset.testid=testid; control.setAttribute('aria-label',ariaLabel);
  for(const [id,label] of options){const opt=el('option',label);opt.value=id;opt.selected=id===value;control.append(opt);} return control;
}
function animationBadge(scene,anim){
  const kb=Object.fromEntries(KB_MOTION_OPTIONS), veo=Object.fromEntries(VEO_MOTION_OPTIONS);
  const completed=Boolean(scene.video_asset_id||anim.status==='completed');
  if(completed){
    if(anim.source==='ken_burns'||anim.selected_engine==='ken_burns')return ['🎬 Видео: 2.5D · '+(kb[anim.ken_burns_preset]||'Ken Burns'),'badge-kb'];
    if(anim.selected_engine==='omni_flash')return [`⚡ Видео: Omni · ${(anim.omni||{}).resolution||'720p'} · ${(anim.omni||{}).duration_seconds||anim.duration_seconds||6} сек.`,'badge-omni'];
    if(anim.source==='flow_manual')return ['🎬 Видео: Flow · ручной импорт','badge-veo-manual'];
    return ['✨ Видео: Veo · '+(veo[(anim.veo||{}).camera_motion||anim.camera_motion]||'камера'),'badge-veo'];
  }
  const labels={
    planned_omni:`⚡ Omni · ${(anim.omni||{}).resolution||'720p'} · ${(anim.omni||{}).duration_seconds||anim.duration_seconds||6} сек.`,
    queued_omni:'⚡ Omni ожидает Flow Bridge',generating_omni:'⚡ Omni генерируется…',
    planned_veo:'✨ Veo запланирован',queued_veo:'✨ Veo ожидает Flow Bridge',generating_veo:'✨ Veo генерируется…',
    planned_ken_burns:'🎬 2.5D запланирован',waiting_source_image:'📷 Ожидает статичный кадр'
  };
  return [labels[anim.status]||'Анимация не выбрана',anim.selected_engine==='omni_flash'?'badge-omni':anim.selected_engine==='veo_lite'?'badge-veo':'badge-kb'];
}
function renderCompactAnimationControl(scene,display,anim){
  const box=el('section',undefined,'scene-animation-control');
  const row=el('div',undefined,'animation-control-row');
  const [badgeText,badgeClass]=animationBadge(scene,anim);
  const badge=el('span',badgeText,'scene-anim-badge '+badgeClass); badge.dataset.testid=`scene-anim-badge-${scene.position}`;
  const select=selectOptions([['none','Без анимации'],['ken_burns','2.5D Ken Burns'],['omni_flash','Omni через Google Flow'],['veo_lite','Veo через Google Flow']],anim.selected_engine||'none',`scene-animation-engine-${scene.position}`,'Движок анимации кадра');
  select.classList.add('animation-engine-select');
  select.addEventListener('change',async()=>{try{await action(`/scenes/${scene.id}/animation/select`,{engine:select.value, camera_motion:(anim.veo||{}).camera_motion||anim.camera_motion||'subtle',ken_burns_preset:anim.ken_burns_preset||'slow_zoom_in',omni_resolution:(anim.omni||{}).resolution||'720p',duration_seconds:(anim.omni||{}).duration_seconds||6},`Кадр ${scene.position}: движок анимации обновлён.`);}catch(error){showNotice(error.message||String(error),true);}});
  row.append(badge,select);
  if(scene.video_asset_id||anim.status==='completed'){
    const remove=el('button','Удалить видео','btn-delete-scene-video'); remove.type='button';remove.dataset.testid=`delete-video-btn-${scene.position}`;remove.setAttribute('aria-label',`Удалить видео кадра ${scene.position} и вернуть статичное изображение`);
    remove.onclick=async e=>{e.stopPropagation();try{await api(`/scenes/${scene.id}/animation/delete`,{});showNotice(`Видео кадра ${scene.position} удалено.`);await refresh(true);}catch(error){showNotice(`Ошибка удаления: ${error.message||error}`,true);}}; row.append(remove);
  }
  box.append(row);
  const disclosure=document.createElement('details'); disclosure.className='animation-disclosure'; disclosure.dataset.testid=`scene-animation-disclosure-${scene.position}`;
  const summary=el('summary','Настроить анимацию');summary.setAttribute('aria-label',`Настроить анимацию кадра ${scene.position}`); disclosure.append(summary);
  const params=el('div',undefined,'animation-parameters');
  const engine=anim.selected_engine||'none';
  if(engine==='ken_burns'){
    const preset=selectOptions(KB_MOTION_OPTIONS,anim.ken_burns_preset||anim.safe_crop?.actual_preset||'slow_zoom_in',`ken-burns-motion-${scene.position}`,'Движение 2.5D Ken Burns');
    preset.onchange=async()=>action(`/scenes/${scene.id}/animation/select`,{engine:'ken_burns',ken_burns_preset:preset.value},`Кадр ${scene.position}: движение 2.5D сохранено.`);
    const run=el('button','Анимировать 2.5D','btn-animate-kb');run.type='button';run.dataset.testid=`btn-animate-kb-${scene.position}`;
    run.onclick=async()=>{if(!display?.asset_id){showNotice('Сначала создайте статичное изображение кадра.',true);return;}run.disabled=true;try{const out=await renderKenBurnsVideoBlob(`/api/assets/${display.asset_id}`,5,30,scene.data?.shot||scene.data?.beat||'',scene.position,preset.value);await uploadSceneVideoBlob(scene.id,out.blob,out.mimeType,{source:'ken_burns',expected_scene_revision:scene.current_revision,source_image_asset_id:display.asset_id});showNotice(`Кадр ${scene.position}: 2.5D-видео готово.`);await refresh(true);}catch(error){showNotice(`Ошибка 2.5D: ${error.message||error}`,true);run.disabled=false;}};
    params.append(el('strong','2.5D Ken Burns'),preset,run);
  }else if(engine==='omni_flash'){
    const omni=anim.omni||{};const resolution=selectOptions([['360p','360p'],['720p','720p']],omni.resolution||'720p',`omni-resolution-${scene.position}`,'Качество Omni');
    const duration=selectOptions([[4,'4 сек.'],[6,'6 сек.'],[8,'8 сек.'],[10,'10 сек.']].map(([a,b])=>[String(a),b]),String(omni.duration_seconds||6),`omni-duration-${scene.position}`,'Длительность Omni');
    const persist=()=>api(`/scenes/${scene.id}/animation/select`,{engine:'omni_flash',omni_resolution:resolution.value,duration_seconds:Number(duration.value)});
    resolution.onchange=async()=>{try{await persist();showNotice('Параметры Omni сохранены.');}catch(error){showNotice(error.message||String(error),true);}}; duration.onchange=resolution.onchange;
    const run=el('button','Анимировать Omni','btn-animate-omni');run.type='button';run.dataset.testid=`btn-animate-omni-${scene.position}`;
    run.onclick=async()=>{if(!display?.asset_id){showNotice('Сначала создайте статичное изображение кадра.',true);return;}run.disabled=true;try{const bridge=await api('/bridge/v1/status');if(!bridge.connected)throw Error('Omni ожидает подключения FrameForge Bridge в Google Flow.');await api(`/scenes/${scene.id}/animation/queue_omni`,{resolution:resolution.value,duration_seconds:Number(duration.value)});await api('/bridge/v1/command',{action:'start',project_id:current,scene_ids:[scene.id]});showNotice(`Кадр ${scene.position}: Omni отправлен в Flow (${resolution.value}, ${duration.value} сек.).`);await refresh(true);}catch(error){showNotice(`Omni не запущен: ${error.message||error}`,true);run.disabled=false;}};
    params.append(el('strong','Omni через Google Flow'),resolution,duration,run,el('small','Использует подключённый FrameForge Bridge. Отдельный ключ не нужен.','muted'));
  }else if(engine==='veo_lite'){
    const motion=selectOptions(VEO_MOTION_OPTIONS,(anim.veo||{}).camera_motion||anim.camera_motion||'subtle',`veo-motion-${scene.position}`,'Движение камеры Veo');
    motion.onchange=async()=>action(`/scenes/${scene.id}/animation/select`,{engine:'veo_lite',camera_motion:motion.value},`Кадр ${scene.position}: движение Veo сохранено.`);
    const run=el('button','Анимировать Veo','btn-animate-veo');run.type='button';run.dataset.testid=`btn-animate-veo-${scene.position}`;
    run.onclick=async()=>{if(!display?.asset_id){showNotice('Сначала создайте статичное изображение кадра.',true);return;}run.disabled=true;try{const bridge=await api('/bridge/v1/status');if(!bridge.connected)throw Error('Veo ожидает подключения FrameForge Bridge в Google Flow.');await api(`/scenes/${scene.id}/animation/queue_veo`,{camera_motion:motion.value});await api('/bridge/v1/command',{action:'start',project_id:current,scene_ids:[scene.id]});showNotice(`Кадр ${scene.position}: Veo отправлен в Flow.`);await refresh(true);}catch(error){showNotice(`Veo не запущен: ${error.message||error}`,true);run.disabled=false;}};
    params.append(el('strong','Veo через Google Flow'),motion,run);
  }else params.append(el('p','Выберите движок, чтобы настроить анимацию этого кадра.','muted'));
  if(anim.status&&anim.status!=='not_planned'){
    const compact=el('p',`Статус: ${anim.status}`,'scene-anim-status-label');params.append(compact);
  }
  if(anim.error){const err=document.createElement('details');err.className='animation-error-details';err.append(el('summary','Ошибка — подробнее'),el('pre',anim.error,'error'));params.append(err);}
  const more=document.createElement('details');more.className='animation-more';more.append(el('summary','Дополнительно'));
  const upload=el('button','Загрузить MP4 вручную','small-button');upload.type='button';upload.onclick=e=>{e.preventDefault();triggerFlowMp4Upload(scene);};more.append(upload);params.append(more);
  disclosure.append(params);box.append(disclosure);return box;
}

function renderSceneCard(scene){
  const card=el('article',undefined,'scene-card');
  let animationControl;
  card.dataset.sceneId=scene.id;
  card.addEventListener('mouseenter',()=>{lastHoveredCard={type:'scene',id:scene.id};});
  const visual=el('div',String(scene.position),'scene-visual'),generation=sceneDisplayGeneration(scene),latest=[...sceneGenerations(scene)].reverse()[0],display=generation||latest;
  
  if(scene.video_asset_id){
    const video=el('video',undefined,'scene-video');
    video.src=`/api/assets/${scene.video_asset_id}`;
    video.autoplay=true;
    video.loop=true;
    video.muted=true;
    video.playsInline=true;
    video.addEventListener('click',(e)=>{e.stopPropagation();if(video.paused)video.play();else video.pause();});
    visual.replaceChildren(video);
  } else if(display?.asset_id){
    const imgNode = image(display.asset_id, `Сцена ${scene.position}`);
    imgNode.classList.add('scene-visual-clickable');
    imgNode.title = `Нажмите для полноэкранного просмотра кадра ${scene.position}`;
    imgNode.onclick = (e) => {
      e.stopPropagation();
      openFullscreenViewer(display.asset_id, `Кадр ${scene.position}`, imgNode);
    };
    visual.replaceChildren(imgNode);
  }
  const state=display?.state;visual.append(el('span',state?labels[state]||state:'Ожидает кадра','scene-status'));

  // Reference avatar badges (Bottom-Left)
  const badges=el('div',undefined,'scene-ref-badges');
  const locName=scene.data.location_ref||scene.data.location;
  if(locName){
    const loc=(project.locations||[]).find(l=>l.name.toLowerCase()===locName.toLowerCase()||l.name.toLowerCase().includes(locName.toLowerCase())||locName.toLowerCase().includes(l.name.toLowerCase()));
    const locAsset=loc?.references?.environment||(loc?latestLocationAsset(loc.id):null);
    if(locAsset){
      const locBadge=el('div',undefined,'scene-ref-badge scene-ref-loc');
      locBadge.title=`Локация: ${loc.name}`;
      locBadge.append(image(locAsset,loc.name));
      locBadge.addEventListener('click',(e)=>{e.stopPropagation();selectedLocation=loc.id;render();});
      badges.append(locBadge);
    }
  }
  const charNames=scene.data.characters||[];
  for(const charName of charNames){
    const char=(project.characters||[]).find(c=>c.name.toLowerCase()===charName.toLowerCase()||c.name.toLowerCase().includes(charName.toLowerCase())||charName.toLowerCase().includes(c.name.toLowerCase()));
    const charAsset=char?.references?.portrait||(char?latestSubjectAsset(char.id):null);
    if(charAsset){
      const charBadge=el('div',undefined,'scene-ref-badge scene-ref-char');
      charBadge.title=`Персонаж: ${char.name}`;
      charBadge.append(image(charAsset,char.name));
      charBadge.addEventListener('click',(e)=>{e.stopPropagation();selectedSubject=char.id;render();});
      badges.append(charBadge);
    }
  }
  const sceneProps=scene.data.props||[];
  for(const sp of sceneProps){
    if(sp.visible === false || sp.state === 'absent') continue;
    const prop=(project.props||[]).find(p=>p.name.toLowerCase()===sp.name.toLowerCase()||p.name.toLowerCase().includes(sp.name.toLowerCase())||sp.name.toLowerCase().includes(p.name.toLowerCase()));
    const propAsset=prop?.references?.item||(prop?latestPropAsset(prop.id):null);
    if(propAsset){
      const propBadge=el('div',undefined,'scene-ref-badge scene-ref-prop');
      propBadge.title=`Реквизит: ${prop.name} (${sp.state||'present'})`;
      propBadge.append(image(propAsset,prop.name));
      propBadge.addEventListener('click',(e)=>{e.stopPropagation();selectedProp=prop.id;render();});
      badges.append(propBadge);
    }
  }
  if(badges.children.length>0)visual.append(badges);

  // Only the two useful per-frame actions stay on the image.
  const topActions=el('div',undefined,'scene-top-actions');
  const animBtn=el('button',undefined,'scene-animate-btn'+(scene.video_asset_id?' animated':''));
  animBtn.type='button';
  animBtn.dataset.testid=`animate-scene-${scene.position}`;
  if(scene.video_asset_id){
    animBtn.innerHTML='<span>▶</span><span>Анимировано</span>';
  } else {
    animBtn.innerHTML='<span>▷</span><span>Анимировать</span>';
  }
  animBtn.addEventListener('click',(e)=>{e.stopPropagation();const disclosure=animationControl?.querySelector('.animation-disclosure');if(disclosure){disclosure.open=true;animationControl.scrollIntoView({behavior:'smooth',block:'nearest'});}else animateSceneModal(scene);});
  const regenerateBtn=el('button','↻ Перегенерировать','scene-regenerate-btn');
  regenerateBtn.type='button';
  regenerateBtn.dataset.testid=`regenerate-scene-${scene.position}`;
  regenerateBtn.addEventListener('click',async(e)=>{
    e.stopPropagation();
    if(regenerateBtn.disabled)return;
    regenerateBtn.disabled=true;
    try{
      if(currentSettings?.image_backend==='flow'){
        const bridge=await api('/bridge/v1/status');
        if(!bridge.connected)throw Error('Мост Google Flow не подключён. Откройте FrameForge Bridge.');
        if(bridge.scoped_scene_start!==true)throw Error('Обновите userscript FrameForge Flow Bridge и перезагрузите вкладку Flow: запуск отдельного кадра недоступен.');
        await api(`/projects/${current}/batch`,{idempotency_key:crypto.randomUUID(),scene_ids:[scene.id]});
        await api('/bridge/v1/command',{action:'start',project_id:current,scene_ids:[scene.id]});
        showNotice(`Кадр ${scene.position}: повторная генерация отправлена в Google Flow.`);
        await refresh(true);
      }else{
        await action(`/projects/${current}/batch`,{idempotency_key:crypto.randomUUID(),scene_ids:[scene.id]},`Кадр ${scene.position} поставлен на перегенерацию`);
      }
    }catch(error){
      showNotice(error.message||String(error),true);
    }finally{
      regenerateBtn.disabled=false;
    }
  });
  topActions.append(regenerateBtn,animBtn);
  visual.append(topActions);

  const body=el('div',undefined,'scene-body');
  body.append(el('h3',`Кадр ${scene.position}`));

  const animData = scene.data?.animation || {};
  const animInfo = renderCompactAnimationControl(scene, display, animData);
  animationControl = animInfo;
  if(scene.stale)animInfo.append(el('p', `⚠️ Устарело: ${scene.stale_reason || 'изменился исходный кадр или сценарий'}`, 'scene-stale-warning'));
  body.append(animInfo);

  // Variants row for current revision
  const variants = sceneGenerations(scene).filter(g => g.state === 'succeeded' && g.asset_id);
  if (variants.length > 0) {
    const variantsBox = el('div', undefined, 'scene-variants');
    variantsBox.dataset.testid = `scene-variants-${scene.position}`;
    variants.forEach((v, idx) => {
      const isActive = v.id === scene.active_generation_id;
      const vItem = el('div', undefined, 'scene-variant-item' + (isActive ? ' is-active' : ''));
      vItem.dataset.generationId = v.id;
      vItem.dataset.testid = `variant-item-${scene.position}-${idx + 1}`;

      const vImg = el('img', undefined, 'scene-variant-thumb');
      vImg.src = `/api/assets/${v.asset_id}`;
      vImg.alt = `Вариант ${idx + 1}`;
      vImg.title = `Вариант ${idx + 1} (нажмите для полноэкранного просмотра)`;
      vImg.onclick = (e) => {
        e.stopPropagation();
        openFullscreenViewer(v.asset_id, `Кадр ${scene.position} — Вариант ${idx + 1}`, vImg);
      };

      const vFooter = el('div', undefined, 'scene-variant-footer');
      const vLabel = el('span', `№${idx + 1}`, 'scene-variant-label');
      vFooter.append(vLabel);

      if (isActive) {
        const activeBadge = el('span', '✓ Выбран', 'scene-variant-badge');
        vFooter.append(activeBadge);
      } else {
        const selectBtn = el('button', 'Выбрать', 'scene-variant-select-btn');
        selectBtn.type = 'button';
        selectBtn.dataset.testid = `select-variant-${v.id}`;
        selectBtn.onclick = async (e) => {
          e.stopPropagation();
          await action(`/generations/${v.id}/select`, {}, `Кадр ${scene.position}: выбран вариант №${idx + 1}`);
        };
        vFooter.append(selectBtn);
      }

      vItem.append(vImg, vFooter);
      variantsBox.append(vItem);
    });
    body.append(variantsBox);
  }

  if(display?.error)body.append(el('p',display.error,'error'));
  if(isActiveGeneration(display))body.append(progressMeter(display,true));
  card.append(visual,body);
  attachDragAndDrop(card, `/scenes/${scene.id}/upload`, `Кадр ${scene.position} успешно сохранён!`);
  return card;
}

function editScene(scene){
  const body=modal(`Детали и настройки кадра ${scene.position}`);
  const data=scene.data||{};
  
  const beatField=field('Сюжетное действие (beat)', data.beat||'', 'textarea');
  beatField.box.style.marginBottom='10px';
  
  const shotField=field('Ракурс и тип кадра (shot)', data.shot||'', 'input');
  shotField.box.style.marginBottom='10px';

  const promptField=field('Ручной промпт (переопределяет автосоставление, если не пусто)', data.manual_prompt||'', 'textarea');
  promptField.box.style.marginBottom='14px';

  // Characters selector
  const charSection=el('div',undefined,'edit-scene-section');
  charSection.style.marginBottom='12px';
  charSection.append(el('strong','Персонажи в кадре:'));
  const charGrid=el('div');
  charGrid.style.cssText='display:flex;flex-wrap:wrap;gap:8px;margin-top:4px;';
  const selectedChars=new Set(data.characters||[]);
  for(const c of (project.characters||[])){
    const lbl=el('label');
    lbl.style.cssText='display:inline-flex;align-items:center;gap:4px;background:#1e2337;padding:4px 8px;border-radius:6px;font-size:12px;cursor:pointer;';
    const chk=el('input');
    chk.type='checkbox';
    chk.checked=selectedChars.has(c.name);
    chk.addEventListener('change',()=>{
      if(chk.checked)selectedChars.add(c.name);
      else selectedChars.delete(c.name);
    });
    lbl.append(chk,document.createTextNode(c.name));
    charGrid.append(lbl);
  }
  charSection.append(charGrid);

  // Location selector
  const locSection=el('div',undefined,'edit-scene-section');
  locSection.style.marginBottom='14px';
  locSection.append(el('strong','Локация:'));
  const locSelect=el('select');
  locSelect.style.cssText='display:block;width:100%;margin-top:4px;padding:6px 8px;background:#1e2337;border:1px solid #3d4560;border-radius:6px;color:#fff;font-size:12px;';
  const emptyLoc=el('option','(Без фиксированной локации)');
  emptyLoc.value='';
  locSelect.append(emptyLoc);
  const currentLoc=data.location_ref||data.location||'';
  for(const l of (project.locations||[])){
    const opt=el('option',l.name);
    opt.value=l.name;
    opt.selected=(l.name.toLowerCase()===currentLoc.toLowerCase());
    locSelect.append(opt);
  }
  locSection.append(locSelect);

  // Props and continuity state selector
  const propSection=el('div',undefined,'edit-scene-section');
  propSection.style.marginBottom='16px';
  propSection.append(el('strong','Реквизит и предметы в кадре:'));
  
  const scenePropsMap=new Map((data.props||[]).map(p=>[p.name, p]));
  const propStateInputs=[];

  const propStates = [
    {id:'worn_on_head', label:'На голове (worn on head)'},
    {id:'worn', label:'Надето (worn)'},
    {id:'held', label:'В руках (held)'},
    {id:'placed', label:'Лежит / установлен (placed)'},
    {id:'carried', label:'Несёт с собой (carried)'},
    {id:'present', label:'В кадре (present)'},
    {id:'absent', label:'Отсутствует (absent)'},
    {id:'given', label:'Передано (given)'},
    {id:'lost', label:'Потеряно (lost)'},
    {id:'destroyed', label:'Уничтожено (destroyed)'}
  ];

  const propList=(project.props||[]).filter(isStoryProp);
  if(!propList.length){
    propSection.append(el('p','В проекте нет постоянного реквизита.','muted'));
  }else{
    const propGrid=el('div');
    propGrid.style.cssText='display:flex;flex-direction:column;gap:8px;margin-top:6px;';
    for(const p of propList){
      const sp=scenePropsMap.get(p.name);
      const row=el('div');
      row.style.cssText='display:flex;flex-wrap:wrap;align-items:center;gap:8px;background:#181c2b;padding:8px;border-radius:8px;border:1px solid #2f374e;';
      
      const chk=el('input');
      chk.type='checkbox';
      chk.checked=sp ? (sp.state !== 'absent' && sp.visible !== false) : false;
      chk.dataset.testid=`edit-scene-prop-${p.id}`;

      const nameEl=el('strong',`🏺 ${p.name}`);
      nameEl.style.cssText='font-size:12px;min-width:110px;';

      const stateSel=el('select');
      stateSel.dataset.testid=`edit-scene-prop-state-${p.id}`;
      stateSel.style.cssText='padding:4px 6px;background:#10131e;border:1px solid #3d4560;border-radius:6px;color:#fff;font-size:12px;';
      for(const st of propStates){
        const opt=el('option',st.label);
        opt.value=st.id;
        opt.selected=sp ? (sp.state===st.id) : (st.id==='present');
        stateSel.append(opt);
      }

      const holderSel=el('select');
      holderSel.dataset.testid=`edit-scene-prop-holder-${p.id}`;
      holderSel.style.cssText='padding:4px 6px;background:#10131e;border:1px solid #3d4560;border-radius:6px;color:#fff;font-size:12px;';
      const noHolder=el('option','(Владелец: авто/нет)');
      noHolder.value='';
      holderSel.append(noHolder);
      for(const c of (project.characters||[])){
        const opt=el('option',`У: ${c.name}`);
        opt.value=c.name;
        opt.selected=sp ? (sp.holder===c.name) : (p.owner===c.name);
        holderSel.append(opt);
      }

      row.append(chk, nameEl, stateSel, holderSel);
      propGrid.append(row);

      propStateInputs.push({
        name: p.name,
        chk,
        stateSel,
        holderSel
      });
    }
    propSection.append(propGrid);
  }

  const actions=el('div',undefined,'inline-actions');
  actions.style.marginTop='16px';
  const saveBtn=button('Сохранить изменения',async()=>{
    const updatedProps = [];
    for(const item of propStateInputs){
      if(item.chk.checked){
        updatedProps.push({
          name: item.name,
          state: item.stateSel.value,
          holder: item.holderSel.value,
          visible: true,
          continuity: 'user_edited'
        });
      } else {
        updatedProps.push({
          name: item.name,
          state: 'absent',
          holder: '',
          visible: false,
          continuity: 'absent'
        });
      }
    }
    const updatedData = {
      ...scene.data,
      beat: beatField.input.value,
      shot: shotField.input.value,
      manual_prompt: promptField.input.value,
      characters: Array.from(selectedChars),
      location_ref: locSelect.value,
      location: locSelect.value || scene.data.location || '',
      props: updatedProps
    };
    await action(`/scenes/${scene.id}/edit`,{revision:scene.current_revision, data:updatedData},'Кадр обновлён');
    dialog.close();
  },'primary','save-scene-details');
  actions.append(saveBtn);

  body.append(beatField.box, shotField.box, promptField.box, charSection, locSection, propSection, actions);
}

async function renderKenBurnsVideoBlob(imgSrc,durationSec=5,fps=30,shotType='',sceneIndex=0,preset=null){
  return new Promise((resolve,reject)=>{
    const canvas=document.createElement('canvas');
    canvas.width=1024;canvas.height=576;
    const ctx=canvas.getContext('2d');
    const img=new Image();
    img.crossOrigin='anonymous';
    img.onload=()=>{
      try{
        const stream=canvas.captureStream(fps);
        let mimeType='video/webm;codecs=vp9';
        if(!MediaRecorder.isTypeSupported(mimeType)){
          mimeType=MediaRecorder.isTypeSupported('video/webm')?'video/webm':'video/mp4';
        }
        const recorder=new MediaRecorder(stream,{mimeType,videoBitsPerSecond:5000000});
        const chunks=[];
        recorder.ondataavailable=e=>{if(e.data.size>0)chunks.push(e.data);};
        recorder.onstop=()=>resolve({blob:new Blob(chunks,{type:mimeType}),mimeType});
        recorder.start();

        const totalFrames=durationSec*fps;
        const kbConfigs = {
          slow_zoom_in: { scale_start: 1.02, scale_end: 1.14, pan_x_start: 0.0, pan_x_end: 0.0, pan_y_start: 0.0, pan_y_end: 0.0 },
          slow_pull_back: { scale_start: 1.14, scale_end: 1.04, pan_x_start: 0.0, pan_x_end: 0.0, pan_y_start: 0.0, pan_y_end: 0.0 },
          slow_zoom_out: { scale_start: 1.14, scale_end: 1.02, pan_x_start: 0.0, pan_x_end: 0.0, pan_y_start: 0.0, pan_y_end: 0.0 },
          slow_panorama: { scale_start: 1.10, scale_end: 1.10, pan_x_start: -0.05, pan_x_end: 0.05, pan_y_start: 0.0, pan_y_end: 0.0 },
          subject_parallax: { scale_start: 1.06, scale_end: 1.14, pan_x_start: -0.03, pan_x_end: 0.03, pan_y_start: 0.0, pan_y_end: -0.01 },
          portrait_push_in: { scale_start: 1.05, scale_end: 1.15, pan_x_start: 0.0, pan_x_end: 0.0, pan_y_start: -0.02, pan_y_end: -0.04 },
          object_reveal: { scale_start: 1.04, scale_end: 1.14, pan_x_start: 0.0, pan_x_end: 0.0, pan_y_start: 0.02, pan_y_end: 0.0 },
          balanced_two_shot: { scale_start: 1.08, scale_end: 1.14, pan_x_start: 0.02, pan_x_end: -0.02, pan_y_start: 0.0, pan_y_end: 0.0 }
        };

        const shotText=(shotType||'').toLowerCase();
        let motionKind='preset';
        let activeCfg = preset && kbConfigs[preset] ? kbConfigs[preset] : null;

        if(!activeCfg){
          if(shotText.includes('wide')||shotText.includes('panorama')||shotText.includes('landscape')||shotText.includes('establishing')){
            motionKind=sceneIndex%2===0?'pan_right':'pan_left';
          }else if(shotText.includes('close')||shotText.includes('portrait')||shotText.includes('face')||shotText.includes('intimate')||shotText.includes('detail')){
            motionKind='face_push';
          }else if(shotText.includes('dutch')||shotText.includes('dynamic')||shotText.includes('low')||shotText.includes('action')||shotText.includes('tension')){
            motionKind='dynamic_drift';
          }else{
            motionKind=sceneIndex%2===0?'push_center':'pull_center';
          }
        }

        let frame=0;

        function drawNextFrame(){
          if(frame>totalFrames){
            recorder.stop();
            return;
          }
          const p=frame/totalFrames;
          const t=0.5-0.5*Math.cos(p*Math.PI);
          let scale=1.0,dx=0,dy=0,rotate=0;

          if(activeCfg){
            scale=activeCfg.scale_start+t*(activeCfg.scale_end-activeCfg.scale_start);
            const panX=activeCfg.pan_x_start+t*(activeCfg.pan_x_end-activeCfg.pan_x_start);
            const panY=activeCfg.pan_y_start+t*(activeCfg.pan_y_end-activeCfg.pan_y_start);
            dx=(canvas.width*(1-scale))/2+panX*canvas.width;
            dy=(canvas.height*(1-scale))/2+panY*canvas.height;
          }else if(motionKind==='pan_right'){
            scale=1.08;
            const pan=canvas.width*0.06;
            dx=-pan+2*pan*t;
            dy=(canvas.height*(1-scale))/2;
          }else if(motionKind==='pan_left'){
            scale=1.08;
            const pan=canvas.width*0.06;
            dx=pan-2*pan*t;
            dy=(canvas.height*(1-scale))/2;
          }else if(motionKind==='face_push'){
            scale=1.00+0.15*t;
            dx=(canvas.width*(1-scale))/2;
            dy=(canvas.height*(1-scale))*0.35;
          }else if(motionKind==='dynamic_drift'){
            scale=1.03+0.14*t;
            dx=-(canvas.width*0.05*t);
            dy=-(canvas.height*0.04*t);
            rotate=(Math.PI/180)*(-0.8+1.6*t);
          }else if(motionKind==='pull_center'){
            scale=1.12-0.10*t;
            dx=(canvas.width*(1-scale))/2;
            dy=(canvas.height*(1-scale))/2;
          }else{
            scale=1.00+0.12*t;
            dx=(canvas.width*(1-scale))/2;
            dy=(canvas.height*(1-scale))/2;
          }

          ctx.clearRect(0,0,canvas.width,canvas.height);
          ctx.save();
          if(rotate){
            ctx.translate(canvas.width/2,canvas.height/2);
            ctx.rotate(rotate);
            ctx.translate(-canvas.width/2,-canvas.height/2);
          }
          ctx.translate(dx,dy);
          ctx.scale(scale,scale);
          ctx.drawImage(img,0,0,canvas.width,canvas.height);
          ctx.restore();

          const grad=ctx.createRadialGradient(
            canvas.width/2,canvas.height/2,canvas.width*0.35,
            canvas.width/2,canvas.height/2,canvas.width*0.72
          );
          grad.addColorStop(0,'rgba(0,0,0,0)');
          grad.addColorStop(1,'rgba(0,0,0,0.3)');
          ctx.fillStyle=grad;
          ctx.fillRect(0,0,canvas.width,canvas.height);

          frame++;
          setTimeout(drawNextFrame,1000/(fps*2));
        }
        drawNextFrame();
      }catch(err){reject(err);}
    };
    img.onerror=()=>reject(new Error('Не удалось загрузить изображение кадра для анимации.'));
    img.src=imgSrc;
  });
}

async function uploadSceneVideoBlob(sceneId,blob,mimeType,options={}){
  const ext=(mimeType&&mimeType.includes('mp4'))||(blob.name&&blob.name.endsWith('.mp4'))?'video.mp4':'video.webm';
  const base64=await readFileAsBase64(blob);
  const payload={
    video:base64,
    filename:ext,
    source:options.source||'flow_manual',
    expected_scene_revision:options.expected_scene_revision,
    source_image_asset_id:options.source_image_asset_id,
    idempotency_key:options.idempotency_key
  };
  return await api(`/scenes/${sceneId}/video/import`,payload);
}

function showFlowInstructionsModal(scene){
  const container=modal(`Кадр ${scene.position}: Сюжетный контракт и инструкция Flow`);
  const loadingEl=el('p','Загрузка паспорта кадра...','loading-notice');
  container.append(loadingEl);

  api(`/scenes/${scene.id}/animation/passport`).then(passport=>{
    loadingEl.remove();
    const box=el('div',undefined,'flow-modal-box');

    if(passport.stale){
      const alertBox=el('div',undefined,'flow-alert-stale');
      alertBox.innerHTML=`⚠️ <b>Паспорт кадра устарел:</b> ${passport.stale_reason}<br><small>Рекомендуется сначала сгенерировать или обновить статичный кадр перед экспортом в Flow.</small>`;
      box.append(alertBox);
    }

    const secPassport=el('div',undefined,'flow-modal-section');
    secPassport.append(el('h4','📋 Паспорт анимации (Сюжетный контракт)'));
    const charsText=(passport.characters||[]).map(c=>`${c.name}${c.locked?' (портрет зафиксирован)':' (НЕТ ПОРТРЕТА)'}`).join(', ')||'Без персонажей';
    const propsText=(passport.active_props||[]).map(p=>`${p.name} (${p.state})`).join(', ')||'Без активного реквизита';

    secPassport.innerHTML+=`
      <div style="font-size:12px;display:flex;flex-direction:column;gap:6px;color:#cbd5e1;">
        <div><b>Цитата сценария:</b> <i>«${passport.source_quote||'—'}»</i></div>
        <div><b>Драматическое действие (beat):</b> ${passport.beat||'—'}</div>
        <div><b>Персонажи:</b> ${charsText}</div>
        <div><b>Локация и время:</b> ${passport.location?.name||'—'} (${passport.location?.time||'Day'})</div>
        <div><b>Реквизит:</b> ${propsText}</div>
        <div><b>Движение камеры:</b> <code>${passport.camera_motion}</code></div>
        <div style="background:rgba(0,0,0,0.25);padding:6px;border-radius:4px;margin-top:4px;">
          <b>Хронометраж микро-действий:</b><br>
          &bull; <b>0–2 с:</b> ${passport.micro_actions['0_2s']}<br>
          &bull; <b>2–5 с:</b> ${passport.micro_actions['2_5s']}<br>
          &bull; <b>5–8 с:</b> ${passport.micro_actions['5_8s']}
        </div>
      </div>
    `;
    box.append(secPassport);

    const secPrompt=el('div',undefined,'flow-modal-section');
    secPrompt.append(el('h4','✍ Готовый сюжетный промпт для Veo 3.1 Lite'));
    const txtArea=el('textarea',passport.prompt,'flow-prompt-textarea');
    secPrompt.append(txtArea);

    const copyRow=el('div',undefined,'flow-copy-row');
    const copyBtn=el('button','📋 Скопировать сюжетный промпт','btn primary flow-copy-btn');
    copyBtn.type='button';
    copyBtn.onclick=()=>{
      navigator.clipboard.writeText(txtArea.value).then(()=>{
        copyBtn.textContent='✅ Промпт скопирован!';
        setTimeout(()=>copyBtn.textContent='📋 Скопировать сюжетный промпт',2500);
      });
    };
    const flowParamsInfo=el('span','Veo 3.1 Lite · Frames to Video: First · 16:9 · 5s · 10 кр.','flow-params-badge');
    flowParamsInfo.style.fontSize='11px';
    flowParamsInfo.style.color='#94a3b8';
    copyRow.append(copyBtn,flowParamsInfo);
    secPrompt.append(copyRow);
    box.append(secPrompt);

    const secFlow=el('div',undefined,'flow-modal-section');
    secFlow.append(el('h4','🚀 Инструкция по генерации в Google Flow'));

    const stepsList=el('ol',undefined,'flow-steps-list');
    for(const step of (passport.flow_instructions||[])){
      stepsList.append(el('li',step));
    }
    secFlow.append(stepsList);

    const flowBtnsRow=el('div',undefined,'flow-copy-row');
    flowBtnsRow.style.marginTop='12px';

    if(passport.source_image_asset_id){
      const dlLink=el('a',`⬇ Скачать кадр ${scene.position} (PNG)`,'btn secondary');
      dlLink.href=`/api/assets/${passport.source_image_asset_id}`;
      dlLink.download=`frame_${scene.position}_first_frame.png`;
      flowBtnsRow.append(dlLink);
    }

    const openFlowLink=el('a','Открыть Google Flow ↗','btn secondary');
    openFlowLink.href='https://flow.google.com';
    openFlowLink.target='_blank';
    openFlowLink.rel='noopener';
    flowBtnsRow.append(openFlowLink);

    const uploadMp4Btn=el('button','⬆ Загрузить готовый MP4','btn primary');
    uploadMp4Btn.type='button';
    uploadMp4Btn.onclick=()=>{
      dialog.close();
      triggerFlowMp4Upload(scene);
    };
    flowBtnsRow.append(uploadMp4Btn);

    secFlow.append(flowBtnsRow);
    box.append(secFlow);

    container.append(box);
  }).catch(err=>{
    loadingEl.textContent='Ошибка загрузки паспорта: '+err.message;
    loadingEl.className='error';
  });
}

function triggerFlowMp4Upload(scene){
  const input=document.createElement('input');
  input.type='file';
  input.accept='video/mp4,video/webm';
  input.onchange=()=>{
    if(input.files&&input.files[0]){
      showVideoConfirmModal(scene,input.files[0]);
    }
  };
  input.click();
}

function showVideoConfirmModal(scene,file){
  const container=modal(`Подтверждение импорта видео: Кадр ${scene.position}`);
  const box=el('div',undefined,'flow-modal-box');

  const question=el('p',undefined,'flow-confirm-question');
  question.style.fontSize='14px';
  question.style.fontWeight='bold';
  question.innerHTML=`Это видео для кадра ${scene.position}?`;

  const details=el('div');
  details.style.fontSize='12px';
  details.style.color='#cbd5e1';
  details.innerHTML=`
    <div><b>Сюжетное действие:</b> ${scene.data?.beat||scene.data?.source_quote||'—'}</div>
    <div><b>Файл:</b> ${file.name} (${(file.size/(1024*1024)).toFixed(2)} МБ)</div>
  `;

  const video=document.createElement('video');
  video.controls=true;
  video.autoplay=true;
  video.loop=true;
  video.style.cssText='width:100%;max-height:320px;border-radius:8px;background:#000;margin:8px 0;';
  video.src=URL.createObjectURL(file);

  const warnExisting=scene.video_asset_id?el('p','⚠️ Этот кадр уже содержит видео. Импорт осознанно заменит текущий видеофайл.','flow-alert-stale'):null;

  const btnRow=el('div',undefined,'flow-copy-row');
  btnRow.style.marginTop='12px';

  const cancelBtn=el('button','Отмена','btn secondary');
  cancelBtn.type='button';
  cancelBtn.onclick=()=>dialog.close();

  const confirmBtn=el('button',`Подтвердить импорт в кадр ${scene.position}`,'btn primary');
  confirmBtn.type='button';
  confirmBtn.onclick=async()=>{
    confirmBtn.disabled=true;
    confirmBtn.textContent='Импорт и валидация видео...';
    try{
      await uploadSceneVideoBlob(scene.id,file,file.type,{
        source:'flow_manual',
        expected_scene_revision:scene.current_revision,
        source_image_asset_id:scene.active_generation_id||scene.asset_id
      });
      showNotice(`✅ Видео для кадра ${scene.position} успешно импортировано!`);
      dialog.close();
      await refresh(true);
    }catch(e){
      confirmBtn.disabled=false;
      confirmBtn.textContent=`Подтвердить импорт в кадр ${scene.position}`;
      alert('Ошибка импорта видео: '+(e.message||e));
    }
  };

  btnRow.append(cancelBtn,confirmBtn);

  box.append(question,details,video);
  if(warnExisting)box.append(warnExisting);
  box.append(btnRow);
  container.append(box);
}

async function runBatchKenBurns(scenes){
  showNotice(`🎬 Запуск 2.5D анимации ${scenes.length} кадров...`);
  for(let i=0;i<scenes.length;i++){
    const sc=scenes[i];
    const disp=sceneDisplayGeneration(sc);
    if(!disp?.asset_id)continue;
    showNotice(`🎬 Анимация кадра ${sc.position} (${i+1} из ${scenes.length})...`);
    try{
      const chosenPreset=sc.data?.animation?.ken_burns_preset||sc.data?.animation?.safe_crop?.actual_preset||'slow_zoom_in';
      const {blob,mimeType}=await renderKenBurnsVideoBlob(
        `/api/assets/${disp.asset_id}`,
        5,
        30,
        sc.data?.shot||sc.data?.beat||'',
        i,
        chosenPreset
      );
      await uploadSceneVideoBlob(sc.id,blob,mimeType,{
        source:'ken_burns',
        expected_scene_revision:sc.current_revision,
        source_image_asset_id:disp.asset_id
      });
    }catch(e){
      console.error('Animation error for scene',sc.position,e);
    }
  }
  showNotice(`✅ Анимация ${scenes.length} кадров успешно создана!`);
  await refresh(true);
}

async function handleBatchAnimate(strategy){
  let targetScenes=[];
  if(strategy==='all'||strategy==='smart_hybrid'){
    targetScenes=[...project.scenes];
  }else if(strategy==='every_2nd'){
    targetScenes=project.scenes.filter((_,idx)=>idx%2===0);
  }else if(strategy==='every_3rd'){
    targetScenes=project.scenes.filter((_,idx)=>idx%3===0);
  }else if(strategy==='first_40'){
    targetScenes=project.scenes.slice(0,40);
  }else if(strategy==='first_80'){
    targetScenes=project.scenes.slice(0,80);
  }
  const readyScenes=targetScenes.filter(s=>sceneDisplayGeneration(s)?.asset_id);
  if(!readyScenes.length){
    showNotice('Сначала сгенерируйте изображения для выбранных кадров.',true);
    return;
  }
  const bModal=modal('Пакетная 2.5D Ken Burns анимация');
  bModal.append(
    el('p',`Выбрано кадров для анимации: ${readyScenes.length} из ${targetScenes.length} доступных.`),
    el('p','Анимация создается прямо в браузере (Ken Burns Motion) мгновенно и без затрат GPU.','muted')
  );
  const actions=el('div',undefined,'inline-actions');
  actions.style.cssText='margin-top:16px;display:flex;flex-direction:column;gap:10px;';
  const btnKenBurns=button('🎬 Запустить 2.5D анимацию (Ken Burns)',async()=>{
    dialog.close();
    await runBatchKenBurns(readyScenes);
  },'primary');
  actions.append(btnKenBurns);
  bModal.append(actions);
}

async function animateSceneModal(scene){
  const disp=sceneDisplayGeneration(scene);
  if(!disp?.asset_id){
    showNotice('Сначала сгенерируйте изображение для этого кадра.',true);
    return;
  }
  const body=modal(`Анимация кадра ${scene.position}`);
  const animData=scene.data?.animation||{};
  const hasVideo=Boolean(scene.video_asset_id||animData.status==='completed');

  body.append(
    el('p',scene.data.beat||'Анимация выбранного кадра.')
  );

  if(hasVideo){
    const alertBox=el('div',undefined,'flow-modal-section');
    alertBox.style.cssText='display:flex;justify-content:space-between;align-items:center;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.4);border-radius:8px;padding:10px 12px;margin-bottom:12px;';
    const alertText=el('span',`🎬 Видео для кадра ${scene.position} уже создано (${animData.selected_engine==='ken_burns'?'2.5D':'Veo / Flow'}).`);
    alertText.style.fontSize='12.5px';
    alertText.style.color='#a7f3d0';
    const delBtn=button('🗑 Удалить анимацию',async()=>{
      dialog.close();
      try{
        await api(`/scenes/${scene.id}/delete_video`,{});
        showNotice(`Анимация кадра ${scene.position} удалена.`);
        await refresh(true);
      }catch(err){
        showNotice(`Ошибка удаления: ${err.message}`,true);
      }
    },'small-button');
    delBtn.dataset.testid=`modal-delete-video-${scene.position}`;
    alertBox.append(alertText,delBtn);
    body.append(alertBox);
  }

  // Section 1: 2.5D Ken Burns
  const kbSec=el('div',undefined,'flow-modal-section');
  kbSec.append(el('h4','📷 2.5D Ken Burns (Мгновенно в браузере · 0 кредитов)'));
  const kbPresetSelect=el('select',undefined,'scene-motion-select');
  kbPresetSelect.dataset.testid=`modal-kb-preset-${scene.position}`;
  const presets=[
    {id:'slow_zoom_in',label:'Плавное приближение'},
    {id:'slow_pull_back',label:'Плавное отдаление'},
    {id:'slow_panorama',label:'Панорама слева направо'},
    {id:'subject_parallax',label:'Мягкий параллакс'},
    {id:'portrait_push_in',label:'Портретный наезд'},
    {id:'object_reveal',label:'Раскрытие детали'},
    {id:'balanced_two_shot',label:'Движение для двух персонажей'}
  ];
  const curKb=animData.ken_burns_preset||animData.safe_crop?.actual_preset||'slow_zoom_in';
  for(const p of presets){
    const opt=el('option',p.label);
    opt.value=p.id;
    opt.selected=curKb===p.id;
    kbPresetSelect.append(opt);
  }
  const kbLabel=el('label','Движение 2.5D: ','scene-motion-label');
  kbLabel.style.display='block';
  kbLabel.style.marginBottom='4px';
  kbSec.append(kbLabel,kbPresetSelect);

  const kbRunBtn=button('🎬 Запустить 2.5D анимацию (Ken Burns)',async()=>{
    dialog.close();
    showNotice(`🎬 Создаю 2.5D видео для кадра ${scene.position}...`);
    try{
      const {blob,mimeType}=await renderKenBurnsVideoBlob(
        `/api/assets/${disp.asset_id}`,
        5,
        30,
        scene.data?.shot||scene.data?.beat||'',
        scene.position,
        kbPresetSelect.value
      );
      await uploadSceneVideoBlob(scene.id,blob,mimeType,{
        source:'ken_burns',
        expected_scene_revision:scene.current_revision,
        source_image_asset_id:disp.asset_id
      });
      showNotice(`✅ Кадр ${scene.position} успешно анимирован в 2.5D!`);
      await refresh(true);
    }catch(err){
      showNotice(`Ошибка анимации: ${err.message}`,true);
    }
  },'primary');
  kbRunBtn.style.marginTop='10px';
  kbRunBtn.dataset.testid=`modal-run-kb-${scene.position}`;
  kbSec.append(kbRunBtn);
  body.append(kbSec);

  // Section 2: Veo 3.1 Lite
  const veoSec=el('div',undefined,'flow-modal-section');
  veoSec.style.marginTop='12px';
  veoSec.append(el('h4','✨ Veo 3.1 Lite (Google Flow · 10 кредитов)'));
  const veoMotionSelect=el('select',undefined,'scene-motion-select');
  veoMotionSelect.dataset.testid=`modal-veo-motion-${scene.position}`;
  const motions=[
    {id:'subtle',label:'Плавное движение (subtle)'},
    {id:'dynamic',label:'Динамичное действие (dynamic)'},
    {id:'reaction',label:'Эмоция / реакция (reaction)'},
    {id:'pan',label:'Кинонаезд / панорама (pan)'}
  ];
  const curVeo=animData.camera_motion||'subtle';
  for(const m of motions){
    const opt=el('option',m.label);
    opt.value=m.id;
    opt.selected=curVeo===m.id;
    veoMotionSelect.append(opt);
  }
  const veoLabel=el('label','Камера Veo: ','scene-motion-label');
  veoLabel.style.display='block';
  veoLabel.style.marginBottom='4px';
  veoSec.append(veoLabel,veoMotionSelect);

  const veoBtnRow=el('div',undefined,'inline-actions');
  veoBtnRow.style.cssText='margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;';
  const btnAnimateVeoModal=button('✨ Отправить анимацию в Veo (10 кр.)',async()=>{
    dialog.close();
    const disp=sceneDisplayGeneration(scene);
    if(!disp?.asset_id){
      showNotice('Сначала сгенерируйте изображение для этого кадра.',true);
      return;
    }
    showNotice(`✨ Отправляю кадр ${scene.position} в Veo 3.1 Lite...`);
    try{
      await api(`/scenes/${scene.id}/animation/queue_veo`,{
        camera_motion:veoMotionSelect.value
      });
      let bridgeConnected=false;
      try{
        const flowStatus=await api('/flow/status');
        if(flowStatus&&flowStatus.connected){
          bridgeConnected=true;
          await api('/flow/command',{
            command:'animate_scene',
            scene_id:scene.id
          });
        }
      }catch(_){}
      if(bridgeConnected){
        showNotice(`✨ Кадр ${scene.position}: анимация отправлена в Google Flow (Veo 3.1 Lite)!`);
      }else{
        showNotice(`✨ Кадр ${scene.position}: поставлен в очередь Veo (10 кр.)!`);
      }
      await refresh(true);
    }catch(err){
      showNotice(`Ошибка запуска Veo: ${err.message}`,true);
    }
  },'primary');
  btnAnimateVeoModal.dataset.testid=`modal-btn-animate-veo-${scene.position}`;

  const openPassportBtn=button('📋 Сюжетный паспорт',()=>{
    dialog.close();
    showFlowInstructionsModal(scene);
  },'secondary');
  openPassportBtn.dataset.testid=`modal-open-veo-passport-${scene.position}`;

  const uploadFlowBtn=button('⬆ Загрузить MP4',()=>{
    dialog.close();
    triggerFlowMp4Upload(scene);
  },'secondary');
  uploadFlowBtn.dataset.testid=`modal-upload-veo-mp4-${scene.position}`;

  veoBtnRow.append(btnAnimateVeoModal,openPassportBtn,uploadFlowBtn);
  veoSec.append(veoBtnRow);
  body.append(veoSec);
}
function renderQueue(){renderHead('Состояние генерации');const box=workspace('Очередь задач','◌'),controls=batchControls(true),list=el('div',undefined,'queue-list');if(controls){box.append(controls,el('p','Пауза завершит сохранение текущего кадра и остановит следующие задачи.','muted'));}if(!project.generations.length)list.append(el('p','Задач пока нет.','muted'));for(const generation of [...project.generations].reverse()){const item=el('div',undefined,'queue-item'),scene=project.scenes.find(s=>s.id===generation.scene_id),subject=project.characters.find(c=>c.id===generation.character_id),location=project.locations?.find(item=>item.id===generation.location_id),title=scene?`Кадр ${scene.position}`:subject?subject.name:location?location.name:'Разбор сценария',copy=el('div'),failed=Boolean(generation.error);copy.append(el('strong',title),el('div',failed?generation.error:(generation.progress.stage||generation.purpose),failed?'error':'muted'));if(isActiveGeneration(generation))copy.append(progressMeter(generation,true));item.append(copy,el('span',labels[generation.state]||generation.state,generation.state==='failed'?'error':''));list.append(item);}box.append(list);main.append(box);}
function renderExport(){renderHead('Просмотр и сохранение результата');renderScenes();const box=workspace('Экспорт','⇩');box.append(el('p','ZIP содержит выбранные кадры, уменьшенные копии, контактный лист и manifest с параметрами.','muted'),button('Скачать готовые кадры (.zip)',downloadExport,'primary','download-export'));main.append(box);}
async function downloadExport(){const response=await fetch('/api/export/'+current);if(!response.ok){const error=await response.json();throw Error(error.error);}const url=URL.createObjectURL(await response.blob()),link=el('a');link.href=url;link.download='frames.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);}



async function settings(){
  const body=modal('Настройки FrameForge'),config=await api('/settings'),fields={};
  
  const textSection=el('div',undefined,'text-settings');
  textSection.style.cssText='margin:12px 0;padding:12px;background:#17162b;border-radius:10px;border:1px solid #493b70';
  textSection.append(el('h3','🧠 Анализ сценария (LLM)','settings-subtitle'));

  const textBackendBox=el('label');
  textBackendBox.append(el('span','Движок анализатора текста'));
  const textBackendSelect=el('select');
  const optLocal=el('option','Локальный ИИ (llama-server Qwen 4B offline)');
  optLocal.value='local';
  const optGroq=el('option','Groq Cloud (Fast API, Llama / GPT-OSS)');
  optGroq.value='groq';
  textBackendSelect.append(optLocal, optGroq);
  textBackendSelect.value=config.text_backend || 'local';
  textBackendBox.append(textBackendSelect);
  textSection.append(textBackendBox);

  fields['groq_api_key']=field('Groq API Key (gsk_...)', config.groq_api_key || '');
  fields['groq_extra_api_keys']=field('Дополнительные ключи Groq', config.groq_extra_api_keys || '');
  fields['groq_model']=field('Модель Groq', config.groq_model || 'openai/gpt-oss-120b');
  fields['google_api_key']=field('Google API Key для Gemini fallback', config.google_api_key || '');
  fields['google_text_model']=field('Модель Gemini для анализа', config.google_text_model || 'gemini-3.1-flash-lite');
  fields['text_model']=field('Локальная модель анализа', config.text_model || 'Qwen3-4B-Q4_K_M.gguf');
  fields['text_url']=field('Локальный URL анализатора', config.text_url || 'http://127.0.0.1:11435');

  const groqHint = el('p', 'При исчерпании квоты Groq приложение автоматически перейдёт на Gemini, если указан Google API key. Если ключа нет, анализ завершится с исходной ошибкой Groq.', 'muted');
  groqHint.style.cssText = 'font-size:12px;color:#a8b2d1;margin:6px 0;';

  textSection.append(fields['groq_api_key'].box, fields['groq_extra_api_keys'].box, fields['groq_model'].box, fields['google_api_key'].box, fields['google_text_model'].box, groqHint, fields['text_model'].box, fields['text_url'].box);
  body.append(textSection);

  const comfySection=el('div',undefined,'comfy-settings');
  comfySection.style.cssText='margin:12px 0;padding:12px;background:#141724;border-radius:10px;border:1px solid #282e44';
  comfySection.append(el('h3','Параметры локального ComfyUI','settings-subtitle'));
  for(const [key,label] of [['comfy_url','ComfyUI URL'],['unet','Модель изображений'],['clip','Текстовый энкодер'],['vae','VAE'],['job_timeout_seconds','Лимит времени, секунд']]){
    fields[key]=field(label,config[key]);
    comfySection.append(fields[key].box);
  }
  body.append(comfySection);

  const flowSection=el('div',undefined,'google-settings');
  flowSection.style.cssText='margin:12px 0;padding:12px;background:#181b2a;border-radius:10px;border:1px solid #3b4261';
  flowSection.append(
    el('h3','Google Flow (Браузерная генерация)','settings-subtitle'),
    el('p','Для генерации через Google Flow используется ваша подписка на flow.google.com. Копируйте готовые промпты кадров и персонажей кнопкой «📋 Промпт» в 1 клик и загружайте готовые изображения обратно в FrameForge.','muted')
  );
  const flowModelBox=el('label');
  flowModelBox.append(el('span','Модель изображений Flow'));
  const flowModelSelect=el('select');
  for(const model of ['🍌 Nano Banana 2 Lite','🍌 Nano Banana 2','🍌 Nano Banana Pro']){
    const option=el('option',model);option.value=model;flowModelSelect.append(option);
  }
  flowModelSelect.value=config.flow_image_model || '🍌 Nano Banana 2';
  flowModelBox.append(flowModelSelect);
  flowSection.append(flowModelBox,el('p','Omni 1.1 Flash также работает через подключённый FrameForge Bridge в Google Flow; отдельный API key не требуется.','muted'));
  body.append(flowSection);

  body.append(button('Сохранить настройки',async()=>{
    const data={
      image_backend: currentSettings?.image_backend || 'comfy',
      text_backend: textBackendSelect.value,
      flow_image_model: flowModelSelect.value
    };
    for(const[key,item]of Object.entries(fields))data[key]=key==='job_timeout_seconds'?Number(item.input.value):item.input.value;
    currentSettings=await api('/settings',data);
    showNotice('Настройки успешно сохранены.');
    dialog.close();
    render();
  },'primary'));
}

// Global Paste Event Listener (Ctrl + V)
window.addEventListener('paste', async (e) => {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.indexOf('image') !== -1) {
      const blob = item.getAsFile();
      if (!blob) continue;
      showNotice('⏳ Загрузка изображения из буфера обмена...');
      try {
        const base64 = await readFileAsBase64(blob);
        if (lastHoveredCard && lastHoveredCard.type === 'scene') {
          await action(`/scenes/${lastHoveredCard.id}/upload`, { image: base64 }, 'Кадр вставлен из буфера!');
          return;
        }
        // Fallback to first scene without image or selected subject
        const targetScene = project?.scenes.find(s => !sceneDisplayGeneration(s)?.asset_id) || project?.scenes[0];
        if (targetScene) {
          await action(`/scenes/${targetScene.id}/upload`, { image: base64 }, `Кадр ${targetScene.position} вставлен из буфера!`);
        } else if (selectedSubject) {
          await action(`/characters/${selectedSubject}/upload`, { image: base64 }, 'Портрет персонажа вставлен из буфера!');
        }
      } catch (err) {
        showNotice('Ошибка вставки: ' + err.message, true);
      }
      break;
    }
  }
});

projectSelect.addEventListener('change',async()=>{notice.textContent='';window.scrollTo({top:0});if(!projectSelect.value){resetDraft();return;}current=projectSelect.value;project=null;view='studio';await refresh(true);});
document.querySelector('#new-button').addEventListener('click',resetDraft);
document.querySelector('#settings-button').addEventListener('click',()=>settings().catch(error=>showNotice(error.message,true)));
document.querySelectorAll('.step').forEach(item=>item.addEventListener('click',()=>{view=item.dataset.view;render();}));
try{
  token=(await api('/session')).token;
  currentSettings=await api('/settings');
  await listProjects();
  render();
}catch(error){showNotice(error.message,true);}
setInterval(()=>{if(currentSettings?.image_backend==='flow')void refreshFlowBridgeStatus();},3500);
setInterval(()=>{if(current&&!dialog.open&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))refresh().catch(error=>showNotice('Не удалось обновить: '+error.message,true));},2500);
