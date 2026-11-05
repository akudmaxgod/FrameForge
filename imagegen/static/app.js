import {createStyleSelectorCard, openStyleGallery} from './style-gallery.mjs?v=20261031_v1';
import {STYLE_PRESETS, MODES, titleFromStory, clampSceneCount, presetForStyle, MAX_SCENES, SECONDS_PER_FRAME, recommendedSceneCount, storyboardTiming, countStoryWords, readingDurationSeconds} from './ui-config.mjs?v=marker_20261031';
import {mountMontage} from './montage-editor.mjs';
import {smartMotion} from './smart-motion.mjs';
import {beginAnimationRun,animationRunProgress} from './animation-completion.mjs';
import {voiceDraft,voicePlan,startVoiceProject} from './voice-workflow.mjs';
import {sourceReviewPanel} from './source-review-panel.mjs';
import {catalogControls} from './catalog-controls.mjs';
import {editorialControls} from './editorial-controls.mjs';
import {analysisError,analysisReviewState} from './analysis-error.mjs';
import {openPromptImport,editImportedPrompt,formatImportTime} from './prompt-import.mjs';
import {makeProjectActivation} from './project-selection.mjs';
import {openProjectManager} from './project-manager.mjs';
const activateProject=makeProjectActivation(api);
let montageView=null,montageProjectId=null,montageRenderId=0;

const main=document.querySelector('#main'), controls=document.querySelector('#controls');
const notice=document.querySelector('#notice'), dialog=document.querySelector('#editor');
const projectSelect=document.querySelector('#project-select');
let token='',current=null,project=null,view='studio',refreshing=false,currentSettings=null,sessionRefreshPromise=null,deferredProjectRender=false;
let storyAuditBusy=false,storyAuditStop=false,storyAuditProgress='',storyAuditRegenerating=false,storyAuditExpanded=false;
const storyAuditSelected=new Set();
const STORAGE_KEYS = {
  style: 'frameforge-selected-preset',
  mode: 'frameforge-selected-mode',
  cameraMode: 'frameforge-selected-camera-mode',
  cameraCustom: 'frameforge-selected-camera-custom'
};

function getSavedItem(key, fallback) {
  try {
    const val = localStorage.getItem(key);
    return val !== null && val !== undefined ? val : fallback;
  } catch (e) {
    return fallback;
  }
}

function saveItem(key, val) {
  try {
    if (val === undefined || val === null) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, String(val));
    }
  } catch (e) {}
}

function getInitialPreset() {
  const saved = getSavedItem(STORAGE_KEYS.style, 'realism');
  if (saved && STYLE_PRESETS.some(p => p.id === saved)) return saved;
  return 'realism';
}

function getInitialMode() {
  const saved = getSavedItem(STORAGE_KEYS.mode, 'characters');
  if (saved && MODES.some(m => m.id === saved)) return saved;
  return 'characters';
}

function getInitialCameraMode() {
  const saved = getSavedItem(STORAGE_KEYS.cameraMode, 'cinema');
  const validCameraIds = ['cinema', 'portrait', 'panorama', 'dynamic', 'custom'];
  if (saved && validCameraIds.includes(saved)) return saved;
  return 'cinema';
}

function getInitialCameraCustom() {
  return getSavedItem(STORAGE_KEYS.cameraCustom, '');
}

let selectedCameraCustom = getInitialCameraCustom();
let selectedPreset = getInitialPreset();
let selectedMode = getInitialMode();
let selectedCameraMode = getInitialCameraMode();
let selectedCameraPacing = 'steady';
let selectedAspectRatio = '16:9';
let draftStory = '';
let draftTarget = 12;
let draftTargetAuto = true;
let fixedTiming = false;
let selectedSubject = null;
let selectedLocation = null;
let selectedProp = null;
let channelsList=[],selectedChannelId='';
let lastHoveredCard=null;
let flowBridgeStatus=null;
const picked=new Map();
const omniSelections=new Map();
const animationModels=new Map();
const animationModelName=engine=>engine==='veo_lite'?'Veo 3.1 Lite':'Omni';
function selectedAnimationEngine(){
  if(!animationModels.has(current)){
    let saved;try{saved=localStorage.getItem('frameforge-animation-model:'+current);}catch{}
    const engine=saved||project?.animation_settings?.selected_animation_engine;
    animationModels.set(current,engine==='veo_lite'?'veo_lite':'omni_flash');
  }
  return animationModels.get(current);
}
function renderAnimationModelPicker(location){
  const label=el('label',undefined,'flow-model-picker animation-model-picker');label.append(el('span','Модель анимации выбранных кадров'));
  const select=selectOptions([['omni_flash','Omni'],['veo_lite','Veo 3.1 Lite']],selectedAnimationEngine(),'animation-model-'+location,'Модель анимации выбранных кадров');
  select.className='flow-model-select';
  select.disabled=animationRunBusy;
  select.onchange=()=>{const engine=select.value;animationModels.set(current,engine);try{localStorage.setItem('frameforge-animation-model:'+current,engine);}catch{}render();showNotice(`Для отмеченных кадров выбрана ${animationModelName(engine)}. Сохраните выбор или запустите анимацию.`);};
  label.append(select);return label;
}
const animationDisclosures=new Map();
function rememberAnimationDisclosures(){for(const card of main.querySelectorAll('.scene-card')){const d=card.querySelector('.animation-disclosure');if(d)animationDisclosures.set(card.dataset.projectId+':'+card.dataset.sceneId,d.open);}}
function stopScenePreviews(){for(const video of main.querySelectorAll('.scene-video')){video.pause();video.removeAttribute('src');video.load();}}
function olderProjectSnapshot(next,shown){if(!shown||shown.id!==next.id)return false;if(next.version<shown.version)return true;const versions=new Map((next.scenes||[]).map(s=>[s.id,s.current_revision]));return shown.scenes?.some(s=>versions.has(s.id)&&versions.get(s.id)<s.current_revision);}
let animationRunBusy=false;
let localAnimationBatchProject=null,localAnimationStop=false;
const animationRuns=new Map();
function animationRunFor(pid){
  if(!animationRuns.has(pid)){let saved;try{saved=JSON.parse(localStorage.getItem('frameforge-animation-run:'+pid));}catch{}animationRuns.set(pid,Array.isArray(saved?.targets)&&saved.targets.length?{...saved,failures:saved.failures||{}}:null);}
  return animationRuns.get(pid);
}
function saveAnimationRun(pid,run){animationRuns.set(pid,run);try{localStorage.setItem('frameforge-animation-run:'+pid,JSON.stringify(run));}catch{}}
function watchAnimationRun(pid,scenes,options){
  if(!scenes.length)return;
  saveAnimationRun(pid,beginAnimationRun(animationRunFor(pid),scenes,options));
  document.querySelector('#animation-completion')?.remove();
}
function animationRunFailed(pid,scene,error){const run=animationRunFor(pid);if(run){run.failures[scene.id]=String(error||'Не удалось создать анимацию');saveAnimationRun(pid,run);}}
function updateAnimationCompletion(){
  if(!project||!current)return;
  let run=animationRunFor(current);
  const queued=project.scenes.filter(s=>['queued_omni','generating_omni','queued_veo','generating_veo'].includes(s.data.animation?.status));
  if(!run||run.finished&&queued.length){if(queued.length){watchAnimationRun(current,queued);run=animationRunFor(current);}else if(!run)return;}
  const progress=animationRunProgress(run,project.scenes);
  updateInterruptedAnimation(run,progress);
  if(!progress.finished||animationRunBusy)return;
  const title=progress.failed.length?'Анимация завершена с ошибками':'Анимации готовы';
  if(!run.finished){run.finished=true;run.result={completed:progress.completed.length,total:progress.total,failed:progress.failed.map(s=>s.position)};saveAnimationRun(current,run);}
  if(!run.notified){
    run.notified=true;saveAnimationRun(current,run);
    showNotice(`${title}. Готово ${progress.completed.length} из ${progress.total}.`,progress.failed.length>0);
    document.title=(progress.failed.length?'⚠ ':'✓ ')+title+' — FrameForge';
    // Use an existing permission only; opening the app never triggers a prompt.
    if(document.hidden&&typeof Notification!=='undefined'&&Notification.permission==='granted')try{new Notification(title,{body:`Готово ${progress.completed.length} из ${progress.total}.`,tag:'frameforge-animation-'+run.id});}catch{}
  }
  if(run.dismissed||document.querySelector('#animation-completion'))return;
  const box=el('section',undefined,'animation-completion'+(progress.failed.length?' warning':''));box.id='animation-completion';box.dataset.testid='animation-completion';box.setAttribute('role','status');box.setAttribute('aria-live','polite');
  box.append(el('strong',(progress.failed.length?'⚠ ':'✓ ')+title),el('p',progress.failed.length?`Готово ${progress.completed.length} из ${progress.total}. Требуют внимания кадры: ${progress.failed.map(s=>s.position).join(', ')}.`:`Готово ${progress.total} из ${progress.total}. Можно переходить к монтажу.`));
  const actions=el('div');
  actions.append(button(progress.failed.length?'Посмотреть кадры':'Открыть монтаж',()=>{view=progress.failed.length?'studio':'export';render();},'primary','animation-completion-open'),button('Понятно',()=>{run.dismissed=true;saveAnimationRun(current,run);box.remove();document.title='FrameForge';},'secondary','animation-completion-dismiss'));
  box.append(actions);document.body.append(box);
}
function updateInterruptedAnimation(run,progress){
  const existing=document.querySelector('#animation-interrupted'),bridge=flowBridgeStatus;
  const interrupted=!progress.finished&&!animationRunBusy&&bridge?.connected&&bridge.project_id===current&&!bridge.running&&/ошиб|не удалось|недоступен|таймаут|ERR_/i.test(bridge.status||'');
  if(!interrupted){existing?.remove();return;}
  if(existing)return;
  const pid=current,box=el('section',undefined,'animation-completion warning');box.id='animation-interrupted';box.dataset.testid='animation-interrupted';box.setAttribute('role','status');box.setAttribute('aria-live','polite');
  box.append(el('strong','⚠ Анимация приостановлена'),el('p',`Готово ${progress.completed.length} из ${progress.total}. ${bridge.status}. Готовые клипы сохранены.`));
  const actions=el('div');actions.append(button('Продолжить в Flow',async()=>{await api('/bridge/v1/command',{action:'start',project_id:pid,scene_ids:progress.pending.map(s=>s.scene_id)});box.remove();showNotice('Продолжение очереди отправлено в Flow.');},'primary','animation-interrupted-resume'),button('Скрыть',()=>box.remove(),'secondary'));
  box.append(actions);document.body.append(box);
}
let stillApprovalBusy=false,stillApprovalProgress='';
async function approveAllSelectedStills(){
  if(stillApprovalBusy)return;
  const pid=current;
  const scenes=project.scenes.filter(s=>!sceneStillApproved(s)&&s.active_generation_id&&sceneDisplayGeneration(s)?.asset_id&&!s.stale);
  if(!scenes.length){showNotice('Все готовые выбранные изображения уже утверждены.');return;}
  const items=scenes.map(s=>({scene_id:s.id,revision:s.current_revision,generation_id:s.active_generation_id}));
  stillApprovalBusy=true;
  let approved=0,done=0;const failed=[];
  try{
    for(let start=0;start<items.length;start+=8){
      if(current!==pid)break;
      stillApprovalProgress=`Утверждаю ${done} / ${items.length}…`;render();
      const result=await api(`/projects/${pid}/stills/approve-all`,{scenes:items.slice(start,start+8)});
      approved+=result.approved.length+result.already_approved.length;failed.push(...result.failed);done+=Math.min(8,items.length-start);
    }
    if(current===pid){await refresh(true);showNotice(`Утверждено выбранных кадров: ${approved}.`+(failed.length?` Требуют внимания: ${failed.length}. Кадр ${failed[0].position}: ${failed[0].error}`:''),failed.length>0);}
  }finally{stillApprovalBusy=false;stillApprovalProgress='';if(current===pid){await refresh(true);render();}}
}
let animationParameterSave=Promise.resolve();
function saveCameraParameters(path,data,message){
  const pid=current;
  animationParameterSave=animationParameterSave.catch(()=>{}).then(async()=>{await api(path,data);let next;try{next=await api(`/projects/${pid}`);}catch(error){if(error.status===404&&current===pid){resetDraft();history.replaceState(null,'','/');await listProjects();showNotice('Проект удалён или недоступен. Его можно восстановить из корзины.');return;}throw error;}if(current===pid&&!olderProjectSnapshot(next,project)){project=next;render();if(message)showNotice(message);}});
  return animationParameterSave;
}
function animationLocked(scene){return Boolean(scene.video_asset_id||['completed','queued_omni','generating_omni','queued_veo','generating_veo'].includes(scene.data?.animation?.status));}
function omniSelection(){
  if(!omniSelections.has(current)){
    let draft;try{draft=JSON.parse(localStorage.getItem('frameforge-omni-selection:'+current));}catch{}
    const ids=draft||project.scenes.filter(s=>['omni_flash','veo_lite'].includes(s.data?.animation?.selected_engine)&&s.data?.animation?.mode==='manual').map(s=>s.id);
    omniSelections.set(current,new Set(ids));
  }
  return omniSelections.get(current);
}
function storeOmniSelection(){try{localStorage.setItem('frameforge-omni-selection:'+current,JSON.stringify([...omniSelection()]));}catch{}updateAnimationSelectionSummary();}
function updateAnimationSelectionSummary(){
  const scenes=project.scenes.filter(s=>!animationLocked(s)),selected=scenes.filter(s=>omniSelection().has(s.id)).length;
  const label=document.querySelector('[data-testid=animation-selection-summary]');if(label)label.textContent=`${animationModelName(selectedAnimationEngine())}: ${selected} · 2.5D: ${scenes.length-selected} · готовые / запущенные: ${project.scenes.length-scenes.length}`;
  renderAnimationBudget();
}
function renderAnimationBudget(){
  let box=document.querySelector('#animation-budget');
  const selected=project?.scenes.filter(s=>!animationLocked(s)&&omniSelection().has(s.id))||[];
  if(!project||view!=='studio'||!selected.length){box?.remove();return;}
  if(!box){
    box=el('aside',undefined,'animation-budget');box.id='animation-budget';box.dataset.testid='animation-budget';box.setAttribute('aria-label','Выбранные анимации');
    const counts=el('div',undefined,'animation-budget-counts');counts.dataset.testid='animation-budget-counts';counts.setAttribute('aria-live','polite');box.append(counts);
    const model=el('strong');model.dataset.testid='animation-budget-model';box.append(model);
    const total=el('div',undefined,'animation-budget-total');total.dataset.testid='animation-budget-total';box.append(total);
    box.append(button('Запустить генерацию',()=>runSelectedAnimations(),'primary','animation-budget-generate'));
    const hint=el('small',undefined,'muted');hint.dataset.testid='animation-budget-hint';box.append(hint);document.body.append(box);
  }
  const count=selected.length,engine=selectedAnimationEngine(),model=animationModelName(engine);
  box.querySelector('[data-testid=animation-budget-counts]').textContent=`Выбрано кадров: ${count}`;
  box.querySelector('[data-testid=animation-budget-model]').textContent=model;
  box.querySelector('[data-testid=animation-budget-total]').textContent=engine==='veo_lite'?`≈ ${(count*(project.animation_settings?.veo_lite_credit_cost||10)).toLocaleString('ru-RU')} кредитов`:'Стоимость — по тарифу Flow';
  const waiting=selected.filter(s=>!sceneStillApproved(s)||!sceneDisplayGeneration(s)?.asset_id||s.stale).length;
  const bridgeBusy=Boolean(flowBridgeStatus?.running&&flowBridgeStatus?.project_id===current);
  const launch=box.querySelector('[data-testid=animation-budget-generate]');
  launch.disabled=animationRunBusy||bridgeBusy||waiting===count;
  launch.textContent=animationRunBusy?'Отправляю…':bridgeBusy?'Flow занят…':`Запустить в ${model}`;
  launch.title='Запустить только отмеченные кадры.';
  const hint=box.querySelector('[data-testid=animation-budget-hint]');
  hint.textContent=bridgeBusy?'Дождитесь завершения текущей очереди Flow.':waiting?`Не готовы: ${waiting}`:'';hint.hidden=!hint.textContent;
}
async function saveAnimationSelection(){
  const pid=current,selection=[...omniSelection()],engine=selectedAnimationEngine();
  await animationParameterSave;
  const latest=await api(`/projects/${pid}`);
  const result=await api(`/projects/${pid}/animation/selection`,{selected_scene_ids:selection.filter(id=>latest.scenes.some(s=>s.id===id)),animation_engine:engine,expected_revisions:Object.fromEntries(latest.scenes.map(s=>[s.id,s.current_revision]))});
  const updated=await api(`/projects/${pid}`);
  if(current===pid){project=updated;render();showNotice(`Выбор сохранён: ${result.selected_cloud} ${animationModelName(engine)}, ${result.local_2_5d} кадров 2.5D. Генерация ещё не запущена.`);}
  return pid;
}
async function runSelectedAnimations(onlySceneId=null){
  if(onlySceneId)return runSingleAnimation(onlySceneId,omniSelection().has(onlySceneId)?selectedAnimationEngine():'ken_burns');
  if(animationRunBusy)return;
  if(flowBridgeStatus?.running&&flowBridgeStatus?.project_id===current){showNotice('Дождитесь завершения текущей очереди Flow.');return;}
  const pid=current,engine=selectedAnimationEngine(),selection=new Set(omniSelection()),model=animationModelName(engine);
  if(!selection.size){showNotice('Отметьте кадры для анимации.');return;}
  animationRunBusy=true;renderAnimationBudget();
  try{
    await animationParameterSave;
    const latest=await api(`/projects/${pid}`);if(current!==pid)return;
    project=latest;
    const ready=latest.scenes.filter(s=>selection.has(s.id)&&!animationLocked(s)&&sceneStillApproved(s)&&sceneDisplayGeneration(s)?.asset_id&&!s.stale);
    if(!ready.length)throw Error('Среди отмеченных кадров нет готовых к анимации. Проверьте изображения.');
    // Persist only this launch's selected scenes; other frames and running jobs stay untouched.
    for(const scene of ready){
      if(current!==pid)return;
      await api(`/scenes/${scene.id}/animation/select`,{engine});
    }
    if(current!==pid)return;
    watchAnimationRun(pid,ready);
    const result=await api(`/projects/${pid}/animation/generate`,{scene_ids:ready.map(s=>s.id)});
    const queued=[],failed=[];
    for(const scene of ready){
      const status=result.scenes?.find(s=>s.scene_id===scene.id);
      if(['queued_omni','generating_omni','queued_veo','generating_veo'].includes(status?.status))queued.push(scene);
      else{failed.push(scene);animationRunFailed(pid,scene,status?.error);}
    }
    let bridgeError='';
    if(queued.length){try{await api('/bridge/v1/command',{action:'start',project_id:pid,scene_ids:queued.map(s=>s.id)});}catch(error){bridgeError=error.message;}}
    if(current===pid){
      await refresh(true);
      showNotice(`${model}: ${queued.length} кадров ${bridgeError?'в очереди, ожидают подключения':'отправлено в Flow'}.`+(failed.length?` Ошибок: ${failed.length}.`:'')+(bridgeError?` ${bridgeError}`:''),failed.length>0||Boolean(bridgeError));
    }
  }catch(error){if(current===pid)showNotice(error.message||String(error),true);}
  finally{animationRunBusy=false;if(current===pid)render();updateAnimationCompletion();}
}
async function runSingleAnimation(sceneId,engine){
  if(animationRunBusy)return;
  const pid=current;
  animationRunBusy=true;render();
  try{
    await animationParameterSave;
    if(current!==pid)return;
    const latest=await api(`/projects/${pid}`);if(current!==pid)return;
    project=latest;
    const scene=project.scenes.find(s=>s.id===sceneId);
    if(!scene)throw Error('Кадр больше не существует. Обновите проект.');
    if(animationLocked(scene))throw Error('Для этого кадра уже есть видео или запущенная задача.');
    if(!sceneStillApproved(scene))throw Error('Сначала утвердите текущий вариант изображения кнопкой «Утвердить» под ним.');
    if(!sceneDisplayGeneration(scene)?.asset_id||scene.stale)throw Error('Сначала сохраните актуальное изображение этого кадра.');
    const data={engine};
    if(engine==='ken_burns'){
      data.duration_seconds=6;
      if(scene.data.animation?.ken_burns_mode!=='manual')data.ken_burns_preset='auto';
    }
    // A single-frame trial never saves the bulk draft or changes other scenes.
    await api(`/scenes/${sceneId}/animation/select`,data);
    watchAnimationRun(pid,[scene]);
    const selection=omniSelections.get(pid);
    if(selection){engine==='ken_burns'?selection.delete(sceneId):selection.add(sceneId);try{localStorage.setItem('frameforge-omni-selection:'+pid,JSON.stringify([...selection]));}catch{}}
    const name=engine==='ken_burns'?'2.5D':animationModelName(engine);
    showNotice(`Кадр ${scene.position}: запускаю ${name}. Только этот кадр.`);
    const result=await api(`/projects/${pid}/animation/generate`,{scene_ids:[sceneId]});
    const status=result.scenes?.find(s=>s.scene_id===sceneId);
    if(status?.status==='failed'||status?.status==='waiting_source_image')throw Error(status.error||'Кадр пока не готов к анимации.');
    if(engine!=='ken_burns'){
      if(!['queued_omni','generating_omni','queued_veo','generating_veo'].includes(status?.status))throw Error('Flow не подтвердил постановку кадра в очередь.');
      await api('/bridge/v1/command',{action:'start',project_id:pid,scene_ids:[sceneId]});
    }else if(status?.status!=='completed')throw Error('2.5D не завершено. Откройте подробности на карточке.');
    if(current===pid){await refresh(true);showNotice(`Кадр ${scene.position}: ${name}${engine==='ken_burns'?' готово — смотрите видео на карточке.':' отправлен в Flow. Остальные кадры не запускались.'}`);}
  }catch(error){const scene=project?.scenes.find(s=>s.id===sceneId);if(scene)animationRunFailed(pid,scene,error.message);if(current===pid)await refresh(true);throw error;}
  finally{animationRunBusy=false;render();updateAnimationCompletion();}
}
async function resumeSceneAnimation(scene){
  const pid=current;
  const jobs=await api(`/projects/${pid}/animation/jobs`);
  if(current!==pid)return;
  if(!jobs.jobs?.some(job=>job.scene_id===scene.id))throw Error('Кадр пока не готов к отправке в Flow. Проверьте изображение и описание сцены.');
  await api('/bridge/v1/command',{action:'start',project_id:pid,scene_ids:[scene.id]});
  watchAnimationRun(pid,[scene]);
  showNotice(`Кадр ${scene.position}: продолжение анимации отправлено в Flow.`);
  await refresh(true);
}
function unselectedLocalAnimationScenes(scenes,selection){
  return scenes.filter(scene=>!selection.has(scene.id)&&!animationLocked(scene)&&sceneStillApproved(scene)&&sceneDisplayGeneration(scene)?.asset_id&&!scene.stale);
}
async function runUnselectedLocalAnimations(){
  if(animationRunBusy)return;
  const pid=current,selection=new Set(omniSelection());
  animationRunBusy=true;localAnimationBatchProject=pid;localAnimationStop=false;render();
  let completed=0,failed=0,planned=0;
  try{
    await animationParameterSave;
    const latest=await api(`/projects/${pid}`);if(current!==pid)return;
    project=latest;
    const scenes=unselectedLocalAnimationScenes(latest.scenes,selection);planned=scenes.length;
    if(!planned){showNotice('Нет неотмеченных кадров с готовыми изображениями без анимации.');return;}
    let next=0;
    async function worker(){
      while(current===pid&&!localAnimationStop&&next<scenes.length){
        const scene=scenes[next++];
        showNotice(`Создаю 2.5D: готово ${completed} из ${planned} · ускоренная обработка…`);
        try{
          // Already-planned local scenes need no full-project engine selection request.
          if(scene.data.animation?.selected_engine!=='ken_burns'){
            await api(`/scenes/${scene.id}/animation/select`,{engine:'ken_burns'});
          }
          if(current!==pid||localAnimationStop)break;
          watchAnimationRun(pid,[scene]);
          const result=await api(`/projects/${pid}/animation/generate`,{scene_ids:[scene.id]});
          const status=result.scenes?.find(s=>s.scene_id===scene.id);
          if(status?.status!=='completed')throw Error(status?.error||'2.5D не завершено.');
          completed++;
        }catch(error){failed++;animationRunFailed(pid,scene,error.message);}
      }
    }
    await worker();
    if(current===pid){
      await refresh(true);
      showNotice(`${localAnimationStop?'2.5D остановлено. ':''}Готово новых роликов 2.5D: ${completed} из ${planned}.`+(failed?` Ошибок: ${failed}.`:''),failed>0);
    }
  }catch(error){if(current===pid)showNotice(error.message||String(error),true);}
  finally{animationRunBusy=false;localAnimationBatchProject=null;localAnimationStop=false;if(current===pid)render();updateAnimationCompletion();}
}
async function rebuildLocalAnimations(){
  if(animationRunBusy)return;
  const pid=current,scenes=project.scenes.filter(s=>s.data.animation?.selected_engine==='ken_burns'&&s.video_asset_id&&!s.stale&&sceneStillApproved(s));
  watchAnimationRun(pid,scenes,{replaceAssets:true});
  animationRunBusy=true;render();let completed=0,failed=0;
  try{
    for(const scene of scenes){
      if(current!==pid)break;
      showNotice(`Обновляю плавность 2.5D: ${completed+failed+1} из ${scenes.length}…`);
      try{await api(`/projects/${pid}/animation/regenerate-local`,{scene_ids:[scene.id]});completed++;}catch(error){failed++;animationRunFailed(pid,scene,error.message);}
    }
    if(current===pid){await refresh(true);showNotice(`Обновлено 2.5D: ${completed}.`+(failed?` Не удалось обновить: ${failed}.`:''),failed>0);}
  }finally{animationRunBusy=false;render();updateAnimationCompletion();}
}
function renderAnimationSelectionBar(){
  const bar=el('section',undefined,'animation-selection-bar');
  const model=animationModelName(selectedAnimationEngine());
  bar.append(el('strong','Выберите модель и отметьте кадры'),renderAnimationModelPicker('toolbar'),el('p',`Запускаются только отмеченные кадры — ${model} через Google Flow.`,'muted'));
  if(selectedAnimationEngine()==='veo_lite')bar.append(el('small','Veo использует кредиты Google Flow.','muted'));
  const summary=el('div',undefined,'animation-selection-summary');summary.dataset.testid='animation-selection-summary';
  const activeRun=animationRunFor(current);
  if(activeRun&&!activeRun.finished){const progress=animationRunProgress(activeRun,project.scenes),status=el('small',`Анимация: готово ${progress.completed.length} из ${progress.total}. По завершении появится уведомление «Анимации готовы».`,'animation-progress');status.dataset.testid='animation-progress';bar.append(status);}
  const selectable=project.scenes.filter(s=>!animationLocked(s)),count=selectable.filter(s=>omniSelection().has(s.id)).length;
  summary.textContent=`${model}: ${count} · 2.5D: ${selectable.length-count} · готовые / запущенные: ${project.scenes.length-selectable.length}`;
  const actions=el('div',undefined,'animation-selection-actions');
  actions.append(button('Снять все галочки',()=>{for(const s of selectable)omniSelection().delete(s.id);storeOmniSelection();render();},'secondary','animation-select-none'),button(`Все в ${model}`,()=>{for(const s of selectable)omniSelection().add(s.id);storeOmniSelection();render();},'secondary','animation-select-all'),button('Сохранить выбор',saveAnimationSelection,'secondary','btn-animation-plan'),button('Запустить выбранные',()=>runSelectedAnimations(),'primary','btn-animation-generate'));
  const localReady=unselectedLocalAnimationScenes(project.scenes,omniSelection());
  const localLaunch=button(`Запустить остальные в 2.5D (${localReady.length})`,runUnselectedLocalAnimations,'primary','animation-generate-unselected-local');
  localLaunch.disabled=!localReady.length;
  localLaunch.title='Создать 2.5D для неотмеченных кадров с готовыми изображениями. Готовые ролики и задачи Flow пропускаются.';
  actions.append(localLaunch);
  if(project.scenes.some(s=>s.data.animation?.selected_engine==='ken_burns'&&s.video_asset_id))actions.append(button('Обновить плавность 2.5D',rebuildLocalAnimations,'secondary','animation-rebuild-local'));
  if(animationRunBusy){for(const b of actions.children)b.disabled=true;actions.querySelector('[data-testid="btn-animation-generate"]').textContent='Создаю анимации…';}
  if(localAnimationBatchProject===current){
    const stop=button(localAnimationStop?'Останавливаю…':'Остановить 2.5D',()=>{localAnimationStop=true;stop.disabled=true;stop.textContent='Останавливаю…';showNotice('2.5D остановится после текущих кадров.');},'secondary','animation-stop-local');
    stop.disabled=localAnimationStop;actions.append(stop);
  }
  const waiting=selectable.filter(s=>!sceneStillApproved(s)||!sceneDisplayGeneration(s)?.asset_id||s.stale).length;
  if(waiting)bar.append(el('small',`При запуске будут пропущены ${waiting} кадров без актуального изображения.`,'muted'));
  bar.append(summary,actions);return bar;
}
const labels={queued:'В очереди',waiting_review:'Ожидает проверки плана',waiting_prompt:'Ожидает проверки промпта',blocked_prompt:'Промпт требует внимания',preparing:'Подготовка',submitted:'Отправлено',running:'Генерируется',saving:'Сохранение',reconciling:'Проверка',succeeded:'Готово',failed:'Ошибка',cancelled:'Отменено',paused:'Пауза',completed:'Завершено',completed_with_errors:'Есть ошибки',resource_degraded:'Не хватает ресурсов'};
const legacyStoryImportances=new Set(['','story','story_critical','standard','recurring','key','high','primary']);
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
    description: 'Масштаб и мир. Общие планы чередуются с диалогами, реакциями и деталями по действию и соседним кадрам. Ракурсы подчёркивают пространство; ручная композиция имеет приоритет.',
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

CAMERA_MODES.push({id:'custom',name:'Своя',icon:'✎',description:'Задайте собственные планы и ракурсы камеры.',bg:'linear-gradient(#292824,#161513)'});

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
function isActiveGeneration(generation){return generation&&!['succeeded','failed','cancelled','resource_degraded'].includes(generation.state)&&!analysisReviewState(generation);}
function activeBatches(){return (project?.batches||[]).filter(batch=>['running','paused','waiting_review'].includes(batch.state));}
function batchControls(compact=false){const batches=activeBatches();if(!batches.length)return null;const actions=el('div',undefined,'inline-actions'+(compact?' compact-actions':''));for(const batch of batches){if(batch.state==='running'||batch.state==='waiting_review')actions.append(button('Пауза после текущего кадра',()=>action(`/batches/${batch.id}/pause`,{},'Очередь приостановлена после текущих кадров.'),'small-button',`pause-batch-${batch.id}`));else actions.append(button('Продолжить очередь',()=>action(`/batches/${batch.id}/resume`,{},'Очередь продолжена.'),'small-button',`resume-batch-${batch.id}`));actions.append(button('Отменить очередь',()=>action(`/batches/${batch.id}/cancel`,{},'Очередь отменена.'),'small-button',`cancel-batch-${batch.id}`));}return actions;}
function formatDuration(seconds){if(!Number.isFinite(seconds)||seconds<0)return'';const value=Math.round(seconds),minutes=Math.floor(value/60),rest=value%60;return minutes?`${minutes}:${String(rest).padStart(2,'0')}`:`${rest} сек`;}
function progressInfo(generation){if(currentSettings?.image_backend==='flow'&&generation?.purpose==='scene'&&generation?.state==='queued')return{percent:0,stage:flowBridgeStatus?.running?'Генерация в Google Flow':'Ожидает запуска в Google Flow',indeterminate:true};const fallback={queued:5,preparing:10,submitted:28,running:38,saving:96,reconciling:22,succeeded:100,failed:0,cancelled:0,resource_degraded:0};const progress=generation?.progress||{};const completed=Number(progress.analysis_completed_jobs),total=Number(progress.analysis_total_jobs);let timing='';const startedAt=progress.analysis_started_at;const elapsed=typeof startedAt==='number'&&Number.isFinite(startedAt)&&startedAt>0?Math.max(0,Date.now()/1000-startedAt):Number(progress.elapsed_seconds)||0;if(Number.isFinite(total)&&total>0){timing=`Обработано ${Math.min(completed,total)} из ${total} · прошло ${formatDuration(elapsed)}`;}else if(generation?.purpose==='analysis'&&elapsed>0){timing=`Прошло ${formatDuration(elapsed)}`;}const remaining=progress.estimated_remaining_seconds;if(typeof remaining==='number'&&Number.isFinite(remaining)&&remaining>=0)timing+=`${timing?' · ':''}осталось примерно ${formatDuration(remaining)}`;else if(generation?.purpose==='analysis'&&!['succeeded','failed','cancelled'].includes(generation?.state))timing+=`${timing?' · ':''}время до завершения уточняется`;return{percent:Math.max(0,Math.min(100,Number(progress.percent??fallback[generation?.state]??0))),stage:progress.stage||labels[generation?.state]||'Подготовка',timing,indeterminate:Boolean(progress.indeterminate)};}
function progressMeter(generation,compact=false){const info=progressInfo(generation),wrap=el('div',undefined,'progress-meter'+(compact?' compact':'')+(info.indeterminate?' indeterminate':''));wrap.style.margin=compact?'9px 0 3px':'10px 0';const copy=el('div',info.indeterminate?info.stage:`${info.stage} · ${info.percent}%`,'progress-copy');copy.style.cssText='color:#cbd1e0;font-size:12px;margin-bottom:4px';const track=el('div',undefined,'progress-track'),value=el('span');track.style.cssText='height:8px;overflow:hidden;border-radius:999px;background:#252b40;border:1px solid #343b52';value.style.cssText=`display:block;height:100%;min-width:5px;border-radius:inherit;background:linear-gradient(90deg,#6d3dd4,#af7bff);transition:width .5s ease;width:${info.indeterminate?45:info.percent}%`;if(info.indeterminate)value.style.opacity='.82';track.append(value);wrap.append(copy,track);if(info.timing){const timing=el('div',info.timing,'muted');timing.style.cssText='font-size:11px;margin-top:5px';wrap.append(timing);}return wrap;}
function renderOperationProgress(){const active=project?.generations.filter(isActiveGeneration)||[];if(!active.length)return;const latest=active[active.length-1],box=el('section',undefined,'operation-progress');box.dataset.testid='operation-progress';box.style.cssText='margin:0 0 16px;padding:15px 17px;border:1px solid #5b438d;border-radius:14px;background:linear-gradient(120deg,#17142b,#101522);box-shadow:0 8px 28px #0004';const info=progressInfo(latest),hint=el('small',info.indeterminate?'Ожидается ответ внешней модели.':'Процент меняется после фактически завершённого этапа или пакета.');hint.style.cssText='display:block;margin-top:7px;color:#929bb3;font-size:12px';box.append(el('strong',active.length>1?`В работе задач: ${active.length}`:'Выполняется действие'),progressMeter(latest),hint);main.append(box);}
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
  const binary=data instanceof Blob;
  const response=await fetch('/api'+path,{
    method:data===undefined?'GET':'POST',
    headers:data===undefined?{}:{'Content-Type':binary?'application/octet-stream':'application/json','X-Session-Token':token,...(binary?{'X-File-Name':encodeURIComponent(data.name||'footage.mp4')}:{})},
    body:data===undefined?undefined:binary?data:JSON.stringify(data)
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
    const error=Error(message);error.status=response.status;throw error;
  }
  return result;
}
async function refreshFlowBridgeStatus(){
  try{flowBridgeStatus=await api('/bridge/v1/status');}
  catch(error){flowBridgeStatus={connected:false,status:error.message};}
  const label=document.querySelector('#flow-bridge-status');
  if(label){
    const sameProject=flowBridgeStatus.connected&&flowBridgeStatus.project_id===current;
    const status=String(flowBridgeStatus.status||'').replace(/ФФ: Veo генерация кадра ([\w-]+) \(generating_veo\)\.\.\./g,(_,id)=>{const scene=project?.scenes.find(s=>s.id===id);return scene?`Генерируется ${animationModelName(scene.data.animation?.selected_engine)}: кадр ${scene.position}…`:'Генерируется видео…';});
    label.textContent=sameProject
      ? `● Мост подключён${flowBridgeStatus.running?' · генерирует':''}${status?' · '+status:''}`
      : flowBridgeStatus.connected?'● Мост открыт для другого проекта ФФ':'○ Мост не подключён. Откройте инструмент Flow.';
  }
  updateAnimationCompletion();renderAnimationBudget();
}
async function refresh(force=false){
  if(!current||refreshing)return;
  refreshing=true;const pid=current;
  try{
    const next=await api('/projects/'+pid);
    if(current!==pid||olderProjectSnapshot(next,project))return;
    const editing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||Boolean(document.querySelector('.editorial-controls[open], .source-passport[open], .queue-recovery[open]'));
    const liveAnalysis=next.generations?.some(g=>g.purpose==='analysis'&&isActiveGeneration(g));
    const changed=force||liveAnalysis||JSON.stringify(next)!==JSON.stringify(project);
    if(changed){
      project=next;
      if(!selectedSubject||!project.characters.some(c=>c.id===selectedSubject))selectedSubject=referenceSubjects()[0]?.id||null;
      if(!selectedLocation||!project.locations?.some(l=>l.id===selectedLocation))selectedLocation=project.locations?.[0]?.id||null;
      if(!selectedProp||!project.props?.some(p=>p.id===selectedProp))selectedProp=project.props?.[0]?.id||null;
      deferredProjectRender=true;
    }
    if(deferredProjectRender&&(force||!editing)){deferredProjectRender=false;render();}
  }finally{refreshing=false;updateAnimationCompletion();updateVoiceCompletion();}
}
function updateVoiceCompletion(){
  const job=project?.voice_workflow;if(job?.state!=='completed')return;
  const key='frameforge-voice-complete:'+job.result.video_asset_id;
  try{if(localStorage.getItem(key))return;localStorage.setItem(key,'1');}catch{}
  showNotice('Всё готово: ролик Full HD собран с озвучкой. Можно скачать видео.');
  if(document.hidden&&typeof Notification!=='undefined'&&Notification.permission==='granted')try{new Notification('Видео готово',{body:'Ролик собран с озвучкой. Откройте FrameForge, чтобы скачать.',tag:key});}catch{}
}
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

async function refreshChannels(includeArchived=false){try{channelsList=await api('/channels'+(includeArchived?'?include_archived=true':''));}catch(e){channelsList=[];}return channelsList;}
async function listProjects(){const list=await api('/projects');projectSelect.replaceChildren();const empty=el('option','Открыть проект');empty.value='';projectSelect.append(empty);for(const item of list){const option=el('option',item.title);option.value=item.id;option.selected=item.id===current;projectSelect.append(option);}}
function resetDraft(){current=null;project=null;view='studio';selectedSubject=null;selectedLocation=null;selectedProp=null;selectedChannelId='';draftStory='';draftTarget=12;draftTargetAuto=true;selectedPreset=getInitialPreset();selectedMode=getInitialMode();selectedCameraMode=getInitialCameraMode();selectedCameraCustom=getInitialCameraCustom();selectedCameraPacing='steady';selectedAspectRatio='16:9';projectSelect.value='';render();}

function render(){rememberAnimationDisclosures();updateAnimationCompletion();renderAnimationBudget();document.querySelectorAll('.step').forEach(item=>item.classList.toggle('active',item.dataset.view===view));if(view==='export'&&project&&montageProjectId===project.id){montageView?.updateProject(project);return;}montageRenderId++;montageView?.dispose();montageView=null;montageProjectId=null;renderControls();stopScenePreviews();main.replaceChildren();if(!project){renderWelcome();return;}if(view==='queue'||view==='export')renderOperationProgress();if(view==='queue')renderQueue();else if(view==='export')renderExport();else renderStudio();}

function renderControls(){controls.replaceChildren();const locked=Boolean(project);const textBackendLabel='DeepSeek Flash';
 const backendBox=el('section',undefined,'control-section');backendBox.append(sectionLabel('Движок генерации'));
 const switcher=el('div',undefined,'mode-grid');
 const backend=currentSettings?.image_backend||'comfy';
 const btnComfy=button('',async()=>{currentSettings=await api('/settings',{image_backend:'comfy'});showNotice('Выбран локальный генератор (ComfyUI)');render();},'mode-card compact'+(backend==='comfy'?' selected':''),'backend-comfy');
 btnComfy.append(el('span','🖥️'),el('strong','Локальный'));
 const btnFlow=button('',async()=>{currentSettings=await api('/settings',{image_backend:'flow'});showNotice('Выбран Google Flow');render();},'mode-card compact'+(backend==='flow'?' selected':''),'backend-flow');
 btnFlow.append(el('span','🌐'),el('strong','Google Flow'));
 if(currentSettings?.local_image_generation_enabled!==false)switcher.append(btnComfy);
 switcher.append(btnFlow);
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
      const configured=String(currentSettings?.flow_project_url||'').trim();
      let url;try{url=new URL(configured);}catch{throw Error('Укажите ссылку на доступный вам FrameForge Bridge в настройках Google Flow.');}
      if(url.protocol!=='https:'||url.hostname!=='flow.google.com')throw Error('Ссылка инструмента должна вести на https://flow.google.com.');
      window.open(url.href,'_blank','noopener');
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
  if(project){const animationBox=el('section',undefined,'control-section');animationBox.append(renderAnimationModelPicker('sidebar'),el('small','Отметьте кадры и запустите анимацию в появившейся панели.','muted'));controls.append(animationBox);}

  const styleBox = el('section', undefined, 'control-section style-control-section');
  styleBox.append(sectionLabel('Выбрать стиль'));
  const activePresetId = locked ? presetForStyle(project.style).id : selectedPreset;
  const activePreset = STYLE_PRESETS.find(p => p.id === activePresetId) || STYLE_PRESETS[0];

  const styleSelect = el('select', undefined, 'style-dropdown-hidden');
  styleSelect.id = 'style-select';
  styleSelect.dataset.testid = 'style-select';
  styleSelect.style.display = 'none';
  styleSelect.disabled = locked;
  for (const preset of STYLE_PRESETS) {
    const opt = el('option', `${preset.name} (${preset.subtitle})`);
    opt.value = preset.id;
    opt.dataset.testid = `style-${preset.id}`;
    opt.selected = preset.id === activePresetId;
    styleSelect.append(opt);
  }
  styleSelect.addEventListener('change', () => {
    if (!locked) {
      selectedPreset = styleSelect.value;
      try {
        saveItem(STORAGE_KEYS.style, selectedPreset);
      } catch (e) {}
      render();
    }
  });

  const styleCard = createStyleSelectorCard({
    activePreset,
    locked,
    onOpen: (cardEl) => {
      openStyleGallery({
        activePresetId,
        presets: STYLE_PRESETS,
        triggerElement: cardEl,
        onSelect: (presetId) => {
          if (!locked) {
            selectedPreset = presetId;
            try {
              saveItem(STORAGE_KEYS.style, selectedPreset);
            } catch (e) {}
            styleSelect.value = presetId;
            render();
          }
        }
      });
    }
  });

  styleBox.append(styleCard, styleSelect);
  controls.append(styleBox);
  const modeBox=el('section',undefined,'control-section');modeBox.append(sectionLabel('Режим'));const modes=el('div',undefined,'mode-grid insaga-mode-tabs');const activeMode=locked?(project.mode||'characters'):selectedMode;
  for(const mode of MODES){const card=button('',()=>{if(!locked){selectedMode=mode.id;saveItem(STORAGE_KEYS.mode,selectedMode);render();}},'mode-card'+(mode.id===activeMode?' selected':''),`mode-${mode.id}`);card.disabled=locked;card.append(el('span',mode.icon),el('strong',mode.name),el('small',mode.short));modes.append(card);}modeBox.append(modes,el('p',MODES.find(item=>item.id===activeMode).description,'mode-description'));controls.append(modeBox);
  const cameraBox=el('section',undefined,'control-section camera-settings');cameraBox.append(sectionLabel('Камера'));
  const cameraId=locked?(project.camera_mode||'cinema'):selectedCameraMode;
  const camera=CAMERA_MODES.find(x=>x.id===cameraId)||CAMERA_MODES[0];
  const preview=el('div',undefined,'camera-preview');preview.style.backgroundImage=`${camera.bg}, linear-gradient(135deg,#353943 0%,#222731 50%,#131820 100%)`;
  preview.append(el('strong',`${camera.icon} ${camera.name}`),el('p',camera.description));cameraBox.append(preview);
  const cameraTabs=el('div',undefined,'camera-tabs');cameraTabs.setAttribute('role','radiogroup');cameraTabs.setAttribute('aria-label','Камера');
  for(const item of CAMERA_MODES){const choice=button('',()=>{if(!locked){selectedCameraMode=item.id;saveItem(STORAGE_KEYS.cameraMode,selectedCameraMode);render();}},'camera-tab'+(cameraId===item.id?' selected':''),`camera-${item.id}`);choice.disabled=locked;choice.setAttribute('role','radio');choice.setAttribute('aria-checked',String(cameraId===item.id));choice.append(el('span',item.icon),el('small',item.name));cameraTabs.append(choice);}
  cameraBox.append(cameraTabs);if(cameraId==='custom'){const field=el('textarea');field.maxLength=1000;field.placeholder='Опишите планы и ракурсы камеры…';field.value=locked?(project.camera_custom||''):selectedCameraCustom;field.readOnly=locked;field.setAttribute('aria-label','Своя камера');field.addEventListener('input',()=>{selectedCameraCustom=field.value;if(!locked){saveItem(STORAGE_KEYS.cameraCustom,selectedCameraCustom);}});cameraBox.append(field);}controls.append(cameraBox);

  if(project){
    const reviewBox=el('section',undefined,'control-section');
    reviewBox.append(sourceReviewPanel({project,api,button,notice:showNotice,refresh}));
    controls.append(reviewBox);
  }
  const catalogue=catalogControls({project,api,button,refresh,enabled:currentSettings?.source_contract_enabled});if(catalogue)controls.append(catalogue);
  const storyBox=el('section',undefined,'control-section');storyBox.append(sectionLabel('Сценарий'));const textarea=el('textarea',undefined,'story-input');textarea.placeholder='Вставьте историю или сценарий…';textarea.maxLength=160000;textarea.value=locked?project.story:draftStory;textarea.readOnly=locked;textarea.dataset.testid='story-input';const counter=el('span',`${textarea.value.length.toLocaleString('ru-RU')} / 160 000`,'counter');if(!locked){const scriptFile=el('input');scriptFile.type='file';scriptFile.accept='.txt';scriptFile.setAttribute('aria-label','Загрузить сценарий TXT');scriptFile.onchange=async()=>{const file=scriptFile.files?.[0];if(!file)return;const text=await file.text();if(text.length>160000){showNotice('Сценарий должен быть не больше 160 000 символов.',true);return;}textarea.value=text;textarea.dispatchEvent(new Event('input'));};storyBox.append(scriptFile);}storyBox.append(textarea,counter);controls.append(storyBox);
  const shownTarget=locked&&project.scenes.length?project.scenes.length:draftTarget;const countBox=el('section',undefined,'control-section');countBox.append(sectionLabel('Количество кадров'));const rangeWrap=el('div',undefined,'range-wrap'),head=el('div',undefined,'range-head'),numInput=el('input');numInput.type='number';numInput.min='1';numInput.max=String(MAX_SCENES);numInput.value=String(shownTarget);numInput.disabled=locked;numInput.style.cssText='width:64px;background:#181c2b;border:1px solid #3d4560;border-radius:6px;color:#fff;text-align:center;font-weight:700;padding:2px 4px;font-size:14px;';numInput.dataset.testid='scene-count-number';head.append(el('span','Кадры (до '+MAX_SCENES+')'),numInput);const range=el('input');range.type='range';range.min='1';range.max=String(MAX_SCENES);range.value=String(shownTarget);range.disabled=locked;range.dataset.testid='scene-count';const auto=button('Авто',()=>{draftTargetAuto=true;draftTarget=recommendedSceneCount(draftStory);range.value=String(draftTarget);numInput.value=String(draftTarget);updateFrameInfo();},'small-button','auto-scene-count');auto.disabled=locked;const recommendation=el('small',undefined,'muted'),metrics=el('div',undefined,'timing-copy');metrics.style.cssText='margin-top:7px;font-size:12px;color:#aeb6cb';const updateFrameInfo=()=>{const story=locked?project.story:textarea.value;const timing=storyboardTiming(story,locked?shownTarget:draftTarget);counter.textContent=`${textarea.value.length.toLocaleString('ru-RU')} / 160 000`;recommendation.textContent=timing.sentences?`Предварительная оценка по тексту: ${recommendedSceneCount(story)} кадров. Количество можно изменить перед разбором.`:'Раскадровка появится после добавления сценария.';metrics.textContent=`Длительность чтения: ${timing.duration.toFixed(1)} сек · Предложений/кадр ~${(timing.sentences/(locked?shownTarget:draftTarget)).toFixed(1)} · Секунд/кадр ~${(timing.duration/(locked?shownTarget:draftTarget)).toFixed(2)}. В тексте: ${timing.sentences} предложений · ${timing.words.toLocaleString('ru-RU')} слов.`;if(voiceDraft.file)metrics.textContent='Число кадров и их длительность будут рассчитаны по готовой озвучке.';if(locked&&project.scenes.length&&!voiceDraft.file){recommendation.textContent=`Разбор по тексту: ${project.scenes.length} кадров.`;metrics.textContent='Основа каждого кадра — его фрагмент сценария и подтверждённый контекст.';}if(locked&&project.analysis_metadata?.prompt_import){const imported=project.analysis_metadata.prompt_import;recommendation.textContent=`Кадры из TXT: ${project.scenes.length}.`;metrics.textContent=`План: ${imported.seconds_per_scene} сек/кадр · ${formatImportTime(imported.duration_seconds)}. Синхронизация с озвучкой — на монтаже.`;}if(project?.voice_workflow?.result?.planned){const voicePlan=project.voice_workflow.result,frameCount=Number(voicePlan.frame_count)||shownTarget;recommendation.textContent=`По готовой озвучке: ${frameCount} кадров; длительность каждого привязана к речи.`;metrics.textContent=`Озвучка ${formatDuration(voicePlan.duration)} · ${frameCount} кадров · в среднем ${(Number(voicePlan.duration)/frameCount).toFixed(1)} сек/кадр · вся запись включена.`;const review=voicePlan.narrative_timing?.review_frames?.length||0;if(review)metrics.textContent+=` · ${review} кадров с приблизительным временем отдельных слов.`;}numInput.value=String(locked?shownTarget:draftTarget);};textarea.addEventListener('input',()=>{draftStory=textarea.value;if(draftTargetAuto){draftTarget=recommendedSceneCount(draftStory);range.value=String(draftTarget);numInput.value=String(draftTarget);}updateFrameInfo();});range.addEventListener('input',()=>{draftTargetAuto=false;draftTarget=clampSceneCount(range.value);numInput.value=String(draftTarget);updateFrameInfo();});numInput.addEventListener('input',()=>{draftTargetAuto=false;const val=clampSceneCount(numInput.value);draftTarget=val;range.value=String(val);updateFrameInfo();});numInput.addEventListener('change',()=>{numInput.value=String(draftTarget);});rangeWrap.append(head,range,auto,recommendation,metrics);countBox.append(rangeWrap);controls.append(countBox);updateFrameInfo();
  if(!locked){
    controls.append(button(voiceDraft.file?(voiceDraft.autoRender?'Создать видео по озвучке':'Подготовить план по озвучке'):`Сгенерировать (${textBackendLabel})`,handleGenerateClick,'primary','generate-project'));
  }
  else if(!project.scenes.length&&!analysisActive()&&!project.generations.some(g=>g.purpose==='analysis'&&g.state==='waiting_review')){
    controls.append(button(`Повторить разбор (${textBackendLabel})`,()=>action(`/projects/${current}/analyse`,{target:draftTarget},'Разбор поставлен в очередь'),'primary','analyse-project'));
  }
  else if(analysisActive()){const generation=[...project.generations].reverse().find(g=>g.purpose==='analysis'&&isActiveGeneration(g));controls.append(progressMeter(generation));}
}
function imagePath(src,alt){const img=el('img');img.src=src;img.alt=alt;return img;}
function analysisActive(){return project?.generations.some(g=>g.purpose==='analysis'&&isActiveGeneration(g));}
async function handleGenerateClick(){await createAndAnalyse();}
async function createAndAnalyse(){if(!draftStory.trim())throw Error('Добавьте текст сценария.');if(voiceDraft.file){const capability=await api('/voice/capability');if(!capability.ready)throw Error(capability.message);}const preset=STYLE_PRESETS.find(item=>item.id===selectedPreset);const created=await api('/projects',{title:titleFromStory(draftStory),idea:'',story:draftStory,style:preset.prompt,mode:selectedMode,camera_mode:selectedCameraMode,camera_pacing:selectedCameraPacing,camera_custom:selectedCameraCustom,aspect_ratio:selectedAspectRatio,channel_profile_id:selectedChannelId||undefined});current=created.id;project=created;if(voiceDraft.file){showNotice('Загружаю озвучку…');await startVoiceProject(api,current);}else await api(`/projects/${current}/analyse`,{target:clampSceneCount(draftTarget)});await listProjects();await refresh(true);showNotice('Проект создан. Обработка выполняется в FrameForge.');}

function renderWelcome(){const empty=el('section',undefined,'empty-state'),inner=el('div');inner.append(el('div','✦','orb'),el('h1','Создайте раскадровку'),el('p','Разберите сценарий автоматически или загрузите готовые промпты для сцен.','muted'),button('Импортировать промпты TXT / JSON',()=>openPromptImport({dialog,api,notice:showNotice,onCreated:async created=>{current=created.id;project=created;view='studio';history.replaceState(null,'',`?project=${created.id}`);await listProjects();render();}}),'primary','import-prompts'));empty.append(inner);main.append(empty);}
function renderHead(subtitle){
  const head=el('div',undefined,'main-head'),copy=el('div'),titleWrap=el('div');
  titleWrap.style.cssText='display:flex;align-items:center;gap:10px;flex-wrap:wrap;';
  titleWrap.append(el('h1',project.title));
  if(project.channel_profile){
    const ch=project.channel_profile;
    const badge=el('span',`Канал: ${ch.name} (v${ch.active_version_number})`,'channel-badge'+(ch.profile_type==='stickman'?' stickman':''));
    badge.dataset.testid='project-channel-badge';
    titleWrap.append(badge);
    if(ch.has_update){
      const updateBtn=button(`⚡ Обновить профиль (доступна v${ch.latest_version_number})`,()=>openChannelUpdateModal(project.id),'channel-update-badge','project-channel-update-btn');
      titleWrap.append(updateBtn);
    }
  }
  copy.append(titleWrap,el('div',subtitle,'muted'));
  head.append(copy);
  head.append(button('Удалить проект',()=>manageProjects('projects',project.id),'project-delete-link'));
  main.append(head);
}
function renderStudio(){
  renderHead(`${project.scenes.length||'—'} кадров · ${MODES.find(item=>item.id===(project.mode||'characters'))?.name||'Персонажи'} · ${presetForStyle(project.style).name}`);
  if(project.scenes.length&&project.generations.some(g=>g.purpose==='analysis'&&isActiveGeneration(g)))renderAnalysisState();
  renderAnalysisCertificate();
  const voiceReview=voicePlan(project,{button});if(voiceReview)main.append(voiceReview);
  renderForgottenCitiesWorkspace();
  if(!project.scenes.length){renderAnalysisState();return;}
  renderCastDiff();
  renderSubjects();
  renderLocations();
  renderProps();
  renderBatchActionBar();
  renderScenes();
}
function referenceJobs(kind,target){
  const jobs=(project.generations||[]).filter(g=>g.purpose===kind&&g[`${kind}_id`]===target.id&&g[`${kind}_revision`]===target.revision);
  return target.casting?jobs.filter(g=>target.casting.generation_ids.includes(g.id)):jobs.slice(-2);
}
async function pickReference(kind,target,assetId){
  if(target.casting)await api(`/${kind}s/${target.id}/select`,{asset_id:assetId,revision:target.revision});
  const key=`${kind}:${target.id}`,choices=picked.get(key)||{};
  choices[kind==='prop'?'item':'environment']=assetId;picked.set(key,choices);
  if(target.casting)target.casting.selected_asset_id=assetId;
  render();
}
async function startReferenceCasting(kind,target){
  if(referenceJobs(kind,target).some(isActiveGeneration))return;
  if(!isForgottenCitiesProject())return action(`/${kind}s/${target.id}/cast`,{count:2},'Два варианта поставлены в очередь');
  if(currentSettings?.image_backend==='flow'){
    const status=await api('/bridge/v1/status');
    if(!status.connected||!status.reference_casting)throw Error('Подключите обновлённый Flow Bridge с поддержкой кастинга эталонов.');
  }
  const result=await api(`/${kind}s/${target.id}/cast`,{count:2});
  if(currentSettings?.image_backend==='flow')await api('/bridge/v1/command',{action:'start',project_id:current,generation_ids:result.generation_ids});
  await refresh(true);showNotice(`«${target.name}»: создаю только два варианта эталона.`);
}
function referencePairReady(kind,target){
  const jobs=referenceJobs(kind,target);
  return !target.casting || (jobs.length===2&&jobs.every(g=>g.state==='succeeded'&&g.asset_id));
}
function referenceCastingButton(kind,target){
  const busy=referenceJobs(kind,target).some(isActiveGeneration);
  const create=button(busy?'Создаются 2 варианта…':'Создать 2 варианта',()=>startReferenceCasting(kind,target),'secondary',`regenerate-${kind}-${target.id}`);
  create.disabled=busy;
  return create;
}
function forgottenCitiesStatus(errors,referencesPending){
  const blockingErrors=[...new Map(errors.filter(item=>item.code!=='missing_required_reference')
    .map(item=>[item.message||item.code,item])).values()];
  const blocked=blockingErrors.length>0;
  const planBlocked=blockingErrors.some(item=>['invalid_narrative_sequence','unresolved_source_scene'].includes(item.code));
  return {blockingErrors,blocked,
    title:blocked?(planBlocked?'НУЖНО ИСПРАВИТЬ СМЫСЛОВОЙ ПЛАН':'ТРЕБУЕТСЯ ИСПРАВЛЕНИЕ'):
      referencesPending?'СЛЕДУЮЩИЙ ШАГ · закрепите базовые эталоны':'PASS · профиль готов к следующему этапу',
    tone:blocked?'#9f3f4b':referencesPending?'#9b7a35':'#287c59',
    background:blocked?'#421a2244':referencesPending?'#3e321a44':'#123d2d55'};
}
function renderForgottenCitiesWorkspace(){
  const meta=project?.analysis_metadata?.forgotten_cities;
  if(!meta)return;
  const box=workspace('Забытые города · профильный анализ','🏛');
  box.dataset.testid='forgotten-cities-workspace';
  const preflight=project?.preflight||meta.preflight||{};
  const preflightErrors=preflight.errors||[];
  const anchors=meta.anchors||[];
  const targetFor=(anchor)=>anchor.anchor_type==='artifact'
    ? (project.props||[]).find(item=>item.name===anchor.canonical_name)
    : (project.locations||[]).find(item=>item.name===anchor.canonical_name);
  const requiredAnchors=anchors.filter(anchor=>anchor.reference_required&&!targetFor(anchor)?.locked);
  const referencesPending=requiredAnchors.length>0;
  const {blockingErrors,blocked,title,tone:statusTone,background:statusBackground}=forgottenCitiesStatus(preflightErrors,referencesPending);
  // The analysis is implementation detail once setup is complete. Keep this
  // compact panel only while it offers a real action or reports a real fault.
  if(!anchors.length&&!blockingErrors.length)return;
  const status=el('div',undefined,blocked?'fc-status blocked':'fc-status pass');
  status.style.cssText=`padding:11px 13px;border-radius:10px;margin-bottom:14px;border:1px solid ${statusTone};background:${statusBackground};`;
  status.append(el('strong',title));
  if(blockingErrors.length){
    const list=el('ul');
    for(const item of blockingErrors.slice(0,8))list.append(el('li',item.message||item.code));
    status.append(list);
  }
  if(blocked&&referencesPending)status.append(el('div','Закрепление эталонов не исправит эти ошибки плана. Сначала исправьте описания сцен.','muted'));
  if(preflight.warnings?.length)status.append(el('div',`Предупреждений: ${preflight.warnings.length}`,'muted'));
  if(!referencesPending)status.append(button('Пересобрать анализ',async()=>{
    await action(`/projects/${current}/analyse`,{target:project.scenes.length||12,rebuild:true},'Пересборка запущена. Старый результат останется до успешного завершения нового разбора.');
  },'small-button'));
  box.append(status);

  if(!referencesPending&&!blocked)return;
  if(referencesPending)box.append(el('p','Создайте два варианта и закрепите выбранный эталон в блоках «Локации» и «Реквизит».','muted'));
  main.append(box);
}
function renderAnalysisCertificate(){
  const meta=project?.analysis_metadata||{};
  if(meta.analyzer_version==='deepseek'||meta.deepseek_usage||meta.diagnostics?.usage){
    const usage = meta.deepseek_usage || meta.diagnostics?.usage || {};
    const diag = meta.diagnostics || {};
    const scenesCount = project.scenes.length;
    const tokensIn = usage.tokens_in || 0;
    const tokensOut = usage.tokens_out || 0;
    const cost = usage.cost_usd != null ? `$${usage.cost_usd.toFixed(4)}` : '';
    const cacheHit = diag.result_cache_hit ? 'Кэш попадание' : (usage.prompt_cache_hit_tokens ? `Кэш промпта: ${usage.prompt_cache_hit_tokens} ток.` : '');
    const warnings = diag.warnings || [];

    const box=el('div',undefined,'analysis-certificate');
    box.style.cssText='margin:0 0 14px;padding:12px 16px;border-radius:10px;border:1px solid #2f855a;background:rgba(22,101,52,.14);display:flex;gap:14px;align-items:center;flex-wrap:wrap;';
    box.dataset.testid='analysis-certificate';
    box.append(
      el('strong','✓ Разбор DeepSeek Flash'),
      el('span',`${scenesCount} сцен · ${tokensIn} входных токенов · ${tokensOut} выходных${cost ? ' · ' + cost : ''}`,'muted')
    );
    if(cacheHit) box.append(el('span',`⚡ ${cacheHit}`,'muted'));
    if(warnings.length) box.append(el('span',`⚠️ Заметок: ${warnings.length}`,'warning-text'));
    main.append(box);
    return;
  }
  if(meta.prompt_import){const p=meta.prompt_import,box=el('div',undefined,'analysis-certificate');box.dataset.testid='imported-prompts-info';box.append(el('strong','Готовые промпты из TXT'),el('p',`${project.scenes.length} кадров · ${p.seconds_per_scene} сек/кадр · план ${formatImportTime(p.duration_seconds)}. Промпты сохранены из файла. Синхронизация с озвучкой — на монтаже.`,'muted'));main.append(box);return;}
  const status=meta.two_layer_status;
  if(!status||status==='not_run')return;
  const certified=status==='certified'||status==='certified_degraded'||status==='adaptive_checked'||status==='adaptive_degraded';
  const box=el('div',undefined,'analysis-certificate');
  box.style.cssText=`margin:0 0 14px;padding:12px 16px;border-radius:10px;border:1px solid ${certified?'#2f855a':'#b45309'};background:${certified?'rgba(22,101,52,.14)':'rgba(180,83,9,.14)'};display:flex;gap:14px;align-items:center;flex-wrap:wrap;`;
  box.dataset.testid='analysis-certificate';
  box.append(
    el('strong',certified?'✓ Сюжет проверен автоматически':'Требуется повторная проверка'),
    el('span',`${meta.audited_events||0} событий · ${meta.audited_pronouns||0} местоимений · ${meta.qwen_audit_calls||0} вызовов Qwen`,'muted')
  );
  if(status==='certified_degraded')box.append(el('span','Groq был недоступен; итог полностью перепроверен локальной Qwen','muted'));
  if(status==='adaptive_degraded')box.append(el('span','Groq не прошёл сертификацию; Qwen выборочно проверила рискованные события, а весь сценарий прошёл структурные проверки','muted'));
  if(status==='adaptive_checked'){
    const recoveryCount=(meta.cloud_recovery_events||[]).length;
    box.append(el('span',recoveryCount
      ?`Groq разобрал сценарий по частям; Qwen дополнительно проверила ${recoveryCount} незавершённых событий`
      :'Groq проверил весь сюжет; Qwen выборочно проверила рискованные события','muted'));
  }
  main.append(box);
}
function renderAnalysisState(){
  const box=workspace('Подготовка проекта','◌');
  const generation=[...project.generations].reverse().find(g=>g.purpose==='analysis');
  if(!generation){
    box.append(el('p','Запустите разбор сценария слева.','muted'));
  } else {
    const stopped=analysisReviewState(generation);
    if(stopped){
      const explanation=el('p',stopped.text,'error');
      box.append(el('strong',stopped.title),explanation,el('p',stopped.help,'muted'));
      const projectId=project.id,jobId=stopped.jobId;
      main.append(box);
      api(`/projects/${projectId}/google-queue/recovery`).then(recovery=>{
        if(!box.isConnected||current!==projectId)return;
        const job=recovery.jobs.find(item=>item.id===jobId);
        if(!job){box.append(el('p','Состояние проверки изменилось. Обновите страницу.','muted'));return;}
        const state=analysisReviewState(generation,job);
        explanation.textContent=state.text;
        if(state.canRetry&&!recovery.paused)box.append(button('Повторить проверку',async()=>{
          if(current!==projectId)throw Error('Открыт другой проект.');
          await api(`/projects/${projectId}/google-queue/recovery`,{job_id:job.id,expected_state:job.state,action:'retry_check',note:'Повтор заключительной проверки после подтверждённого сбоя сервиса'});
          const fresh=await api(`/projects/${projectId}`);
          if(['failed','paused'].includes(fresh.voice_workflow?.state))await api(`/projects/${projectId}/voice/resume`,{});
          await refresh(true);
          showNotice('Продолжена только проверка сохранённого плана.');
        },'primary','plan-review-retry'));
        else box.append(el('p',recovery.paused?'Проверки проекта приостановлены.':job.retry_blocked_reason||'Повтор сейчас недоступен.','muted'));
      }).catch(()=>{if(box.isConnected)box.append(el('p','Не удалось прочитать причину остановки. Обновите страницу.','muted'));});
      return;
    }
    const line=el('div',undefined,'status-line');
    if(isActiveGeneration(generation))line.append(el('span',undefined,'pulse'));
    const backendUsed=generation.snapshot?.profile?.text_backend||'deepseek';
    const backendLabel=backendUsed==='deepseek'?'DeepSeek V4.1 Flash':(backendUsed==='nvidia'?'NVIDIA Nemotron':backendUsed==='hybrid'?'Гибрид':'Локальная модель Qwen');
    line.append(el('strong',`${labels[generation.state]||generation.state} (${backendLabel})`));
    box.append(line);
    if(isActiveGeneration(generation))box.append(progressMeter(generation));
    if(generation.error){
      const failure=analysisError(generation.error);
      if(failure){
        box.append(el('p',failure.text,'error'),el('p',failure.help,'muted'));
        const details=el('details');details.append(el('summary','Подробности ошибки'),el('p',failure.details));box.append(details);
      }else box.append(el('p',generation.error,'error'));
    }
  }
  main.append(box);
}

function referenceSubjects(){const ids=new Set(project.character_reference_ids||[]);return project.characters.filter(c=>ids.has(c.id));}
function renderSubjects(){
 const isIllustration=project.mode==='illustration',isObjects=project.mode==='objects';
 const box=workspace(`${isObjects?'Объекты и архитектура':'Персонажи'} · ${project.characters.length}`,isObjects?'◇':'♙');
 if(isIllustration){box.append(el('p','В этом режиме постоянные референсы не используются — каждая сцена иллюстрируется самостоятельно.','muted'));main.append(box);return;}
 const subjects=referenceSubjects(),ids=new Set(subjects.map(c=>c.id));
 const renderList=(items,parent)=>{const tabs=el('div',undefined,'subject-tabs subject-rows');for(const subject of items){const tab=button('',()=>{selectedSubject=subject.id;render();},'subject-pill'+(subject.id===selectedSubject?' selected':'')+(subject.locked?' locked':''),`subject-${subject.id}`),avatar=el('span',undefined,'subject-avatar');const preview=subject.references.portrait||latestSubjectAsset(subject.id);if(preview)avatar.append(image(preview,subject.name));else avatar.textContent='＋';tab.append(avatar,el('span',subject.name));tabs.append(tab);}parent.append(tabs);};
 if(subjects.length)renderList(subjects,box);else box.append(el('p','В актуальных сценах нет участников для постоянных эталонов.','muted'));
 const other=project.characters.filter(c=>!ids.has(c.id));
 if(other.length){const registry=el('details'),summary=el('summary',`Справочный реестр: ${other.length}`);registry.append(summary,el('p','Эти записи не участвуют в автоматическом отборе эталонов. Здесь доступны сохранённые портреты и ручные операции.','muted'));renderList(other,registry);if(other.some(c=>c.id===selectedSubject))registry.open=true;box.append(registry);}
 const subject=project.characters.find(c=>c.id===selectedSubject)||subjects[0];
 if(subject)box.append(renderSubjectDetail(subject,isObjects));
 if(project.character_selection_issues?.length)box.append(el('p','Состав участников требует проверки: '+project.character_selection_issues.map(e=>e.message).join('; '),'error'));
 main.append(box);
}
function latestSubjectAsset(id){return [...project.generations].reverse().find(g=>g.character_id===id&&g.state==='succeeded'&&g.asset_id&&!g.snapshot.wardrobe_key)?.asset_id;}
function subjectAge(subject){return [...project.generations].reverse().find(g=>g.character_id===subject.id&&g.snapshot?.age_constraint)?.snapshot.age_constraint?.label||null;}
async function regenerateCharacter(subject){
 const flow=currentSettings?.image_backend==='flow';
 if(flow){const status=await api('/bridge/v1/status');if(!status.connected||!status.character_casting)throw Error('Обновите FrameForge Flow Bridge до 0.1.76 и перезагрузите Flow.');if(status.running)throw Error('Мост завершает текущий запрос. Подождите его окончания.');}
 const result=await api(`/characters/${subject.id}/cast`,{count:1});
 if(flow&&result.state==='queued')await api('/bridge/v1/command',{action:'start',project_id:current,generation_ids:result.generation_ids});
 picked.delete(subject.id);await refresh(true);showNotice(result.reused?'Продолжаю одно задание портрета.':'Создаю один новый портрет. Старый вариант сохранён.');
}
function subjectGender(subject){if(subject.visual_gender)return subject.visual_gender.value==='male'?'мужчина':'женщина';return [...project.generations].reverse().find(g=>g.character_id===subject.id&&g.snapshot?.gender_constraint)?.snapshot.gender_constraint?.label||null;}
function renderSubjectDetail(subject,isObjects){const detail=el('div',undefined,'subject-detail'),head=el('div',undefined,'detail-head'),copy=el('div'),age=subjectAge(subject),gender=subjectGender(subject);copy.append(el('h3',subject.name),el('p',subject.description,'muted'));if(age||gender)copy.append(el('p',`Проверить: ${gender||'персонаж'}${age?`, ${age}`:''}`,'age-hint'));if(['name_hint','description_hint'].includes(subject.visual_gender?.source))copy.append(el('p','Пол предположен по имени или описанию — сверьте со сценарием. При необходимости уточните описание.','muted'));head.append(copy);const actions=el('div',undefined,'inline-actions');
 actions.append(button('📋 Промпт',async()=>{
   const found = await api(`/characters/${subject.id}/prompt`);
   if(found?.prompt){await navigator.clipboard.writeText(found.prompt);showNotice(`📋 Промпт для ${subject.name} скопирован в буфер!`);}
 },'small-button'));
 actions.append(makeUploadButton(isObjects?'📁 Загрузить объект':'📁 Загрузить портрет',`/characters/${subject.id}/upload`,'Портрет успешно загружен и зафиксирован!'));
 if(!subject.locked){actions.append(button('Изменить описание',()=>editCharacter(subject),'small-button',`edit-character-${subject.id}`));}
 else{actions.append(button('🔓 Сменить вариант',()=>action(`/characters/${subject.id}/unlock`,{},'Фиксация персонажа снята'),'small-button',`unlock-character-${subject.id}`));}
 const retry=button('Перегенерировать',()=>regenerateCharacter(subject),'secondary',`regenerate-${subject.id}`);actions.append(retry);
 head.append(actions);detail.append(head);
 attachDragAndDrop(detail, `/characters/${subject.id}/upload`, 'Портрет загружен и зафиксирован!');
 const active=project.generations.find(g=>g.character_id===subject.id&&isActiveGeneration(g));if(active)detail.append(progressMeter(active,true));
 if(subject.locked){const grid=el('div',undefined,'candidate-grid');const asset=subject.references.portrait;if(asset){const card=el('article',undefined,'candidate selected');card.append(image(asset,subject.name));const foot=el('footer');foot.append(el('span',isObjects?'Текущий ракурс':'Текущий эталон'),el('span','✓ Зафиксировано','success'));card.append(foot);grid.append(card);}detail.append(grid);}
 const choices=picked.get(subject.id)||{};picked.set(subject.id,choices);const lockedGeneration=project.generations.find(g=>g.asset_id===subject.references.portrait);const portraits=project.generations.filter(g=>g.character_id===subject.id&&g.character_revision===subject.revision&&g.state==='succeeded'&&g.asset_id&&!g.snapshot.wardrobe_key&&g.asset_id!==subject.references.portrait&&(!subject.locked||!lockedGeneration||Date.parse(g.created_at)>Date.parse(lockedGeneration.created_at))).slice(-1);if(portraits.length===1&&!choices.portrait)choices.portrait=portraits[0].asset_id;if(!portraits.length&&!subject.locked)detail.append(el('p','Создайте один портрет или загрузите готовое изображение.','muted'));else{const grid=el('div',undefined,'candidate-grid');for(const [index,generation] of portraits.entries()){const card=button('',()=>{choices.portrait=generation.asset_id;render();},'candidate'+(choices.portrait===generation.asset_id?' selected':''),`candidate-${subject.id}-${index}`);card.append(image(generation.asset_id,`${subject.name}, вариант ${index+1}`));const foot=el('footer');foot.append(el('span',age||gender?`Вариант ${index+1} · ${gender||''}${gender&&age?' ':''}${age||''}`:`Вариант ${index+1}`),el('span',choices.portrait===generation.asset_id?'✓ Выбран':'Выбрать'));card.append(foot);grid.append(card);}detail.append(grid);}
 for(const g of portraits){const r=g.snapshot?.portrait_review;if(r?.status==='unverified'&&r.reason)detail.append(el('p',r.reason,'muted'));}
 if(portraits.some(g=>g.snapshot?.portrait_review?.status==='unverified'))detail.append(el('p','Перед фиксацией проверьте: на портрете один персонаж, пол и внешность соответствуют сценарию.','muted'));
 if(portraits.length||!subject.locked)detail.append(button(isObjects?'Зафиксировать объект':'Подтвердить и зафиксировать персонажа',async()=>{if(!choices.portrait)throw Error('Выберите один из вариантов или загрузите файл.');await lockCharacter(subject,choices,isObjects);},'success-button',`lock-${subject.id}`));return detail;}

function lockCharacter(subject,choices,isObjects){
 return action(`/characters/${subject.id}/lock`,{revision:subject.revision,references:choices,review:{one_person_confirmed:true}},'Референс зафиксирован');
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

function renderLocations(){
  if(['dynamic','illustration'].includes(project.mode))return;
 const unused=new Set((project.analysis_metadata?.voice_editorial?.unused_locations||[]).map(item=>item.id));
 const locations=(project.locations||[]).filter(item=>!unused.has(item.id));if(!locations.length)return;
 const box=workspace('Локации','⌖');box.id='location-editor';
 const forgotten=project.analysis_metadata?.forgotten_cities;
 const anchors=new Map((forgotten?.anchors||[]).map(a=>[a.canonical_name,a]));
 const groups=forgotten?[
  ['Эталоны для кадров',locations.filter(l=>anchors.get(l.name)?.reference_required||l.locked&&l.references.environment)],
  ['Описания сооружений из сценария',locations.filter(l=>anchors.get(l.name)?.source_kind&&!anchors.get(l.name)?.reference_required&&!(l.locked&&l.references.environment))],
  ['Географические упоминания',locations.filter(l=>!anchors.get(l.name))],
  ['Другие описания мест',locations.filter(l=>anchors.has(l.name)&&!anchors.get(l.name)?.source_kind&&!anchors.get(l.name)?.reference_required&&!(l.locked&&l.references.environment))]
 ]:[['',locations]];
 if(forgotten)box.append(el('p','Эталоны прикрепляются только к подходящим сценам. Описания коридоров, залов и шахт уточняют кадр и не означают, что для каждого нужна отдельная картинка.','muted'));
 for(const [label,items] of groups){
  if(!items.length)continue;
  if(label)box.append(el('h3',`${label} · ${items.length}`));
  const tabs=el('div',undefined,'subject-tabs');
  for(const location of items){
   const tab=button('',()=>{selectedLocation=location.id;render();},'subject-pill'+(location.id===selectedLocation?' selected':'')+(location.locked?' locked':''),`location-${location.id}`);
   const avatar=el('span',undefined,'subject-avatar'),preview=location.references.environment||latestLocationAsset(location.id);
   if(preview)avatar.append(image(preview,location.name));else avatar.textContent='⌂';
   tab.append(avatar,el('span',location.name));tabs.append(tab);
  }
  box.append(tabs);
 }
 const location=locations.find(item=>item.id===selectedLocation)||locations.find(l=>anchors.get(l.name)?.reference_required)||locations[0];
 box.append(renderLocationDetail(location));main.append(box);
}
function latestLocationAsset(id){return [...project.generations].reverse().find(g=>g.location_id===id&&g.state==='succeeded'&&g.asset_id)?.asset_id;}
function renderLocationDetail(location){const detail=el('div',undefined,'subject-detail'),head=el('div',undefined,'detail-head'),copy=el('div');copy.append(el('h3',location.name),el('p',location.description,'muted'));head.append(copy);const actions=el('div',undefined,'inline-actions');
 actions.append(button('📋 Промпт',async()=>{
   const prompts = await api(`/projects/${current}/prompts`);
   const found = prompts.locations.find(l=>l.id===location.id);
   if(found?.prompt){await navigator.clipboard.writeText(found.prompt);showNotice(`📋 Промпт локации ${location.name} скопирован в буфер!`);}
 },'small-button'));
 actions.append(makeUploadButton('📁 Загрузить референс',`/locations/${location.id}/upload`,'Локация успешно загружена и зафиксирована!'));
 if(!location.locked){actions.append(button('Изменить описание',()=>editLocation(location),'small-button',`edit-location-${location.id}`));actions.append(referenceCastingButton('location',location));}
 else{actions.append(button('🔓 Сменить вариант',()=>action(`/locations/${location.id}/unlock`,{},'Фиксация локации снята'),'small-button',`unlock-location-${location.id}`));}
 head.append(actions);detail.append(head);
 attachDragAndDrop(detail, `/locations/${location.id}/upload`, 'Локация загружена и зафиксирована!');
 const active=project.generations.find(g=>g.location_id===location.id&&isActiveGeneration(g));if(active)detail.append(progressMeter(active,true));
 if(location.locked){const grid=el('div',undefined,'candidate-grid'),asset=location.references.environment;if(asset){const card=el('article',undefined,'candidate selected');card.append(image(asset,location.name));const foot=el('footer');foot.append(el('span','Основной вид'),el('span','✓ Зафиксировано','success'));card.append(foot);grid.append(card);}detail.append(grid);return detail;}
 const choices={...(picked.get('location:'+location.id)||{}),...(location.casting?{environment:location.casting.selected_asset_id}:{})};picked.set('location:'+location.id,choices);const candidates=referenceJobs('location',location).filter(g=>g.state==='succeeded'&&g.asset_id);if(!candidates.length)detail.append(el('p','Создайте варианты локально или загрузите изображение из Google Flow.','muted'));else{const grid=el('div',undefined,'candidate-grid');for(const [index,generation] of candidates.entries()){const card=button('',()=>pickReference('location',location,generation.asset_id),'candidate'+(choices.environment===generation.asset_id?' selected':''),`location-candidate-${location.id}-${index}`);card.disabled=!referencePairReady('location',location);card.append(image(generation.asset_id,`${location.name}, вариант ${index+1}`));const foot=el('footer');foot.append(el('span',`Вариант ${index+1}`),el('span',choices.environment===generation.asset_id?'✓ Выбран':'Выбрать'));card.append(foot);grid.append(card);}detail.append(grid);}
 const lock=button('Закрепить внешний вид локации',async()=>{if(!choices.environment)throw Error('Выберите вариант локации или загрузите файл.');await action(`/locations/${location.id}/lock`,{revision:location.revision,references:choices},'Внешний вид локации зафиксирован');},'success-button',`lock-location-${location.id}`);lock.disabled=!referencePairReady('location',location)||!choices.environment;detail.append(lock);return detail;}
function editLocation(location){const body=modal('Описание локации'),description=field('Постоянные детали: фасад, окна, двери, планировка',location.description,'textarea');body.append(description.box,button('Сохранить',async()=>{await action(`/locations/${location.id}/edit`,{revision:location.revision,description:description.input.value},'Описание локации обновлено. Создайте новые варианты.');dialog.close();},'primary'));}

function renderProps(){
  if(['dynamic','illustration'].includes(project.mode))return;
  // Keep non-story records visible so the user can correct classification in
  // either direction. Only story props are sent to generation and references.
  const props=project.props||[];
  if(!props.length)return;
  const box=workspace('Реквизит и классификация','🏺'),tabs=el('div',undefined,'subject-tabs');
  box.id='prop-editor';
  for(const prop of props){
    const tab=button('',()=>{selectedProp=prop.id;render();},'subject-pill'+(prop.id===selectedProp?' selected':'')+(prop.locked?' locked':''),`prop-${prop.id}`);
    const avatar=el('span',undefined,'subject-avatar');
    const preview=prop.references?.prop||prop.references?.item||latestPropAsset(prop.id);
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
    if(story)actions.append(referenceCastingButton('prop',prop));
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
    const grid=el('div',undefined,'candidate-grid'),asset=prop.references?.prop||prop.references?.item;
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
  const choices={...(picked.get('prop:'+prop.id)||{}),...(prop.casting?{item:prop.casting.selected_asset_id}:{})};picked.set('prop:'+prop.id,choices);
  const candidates=referenceJobs('prop',prop).filter(g=>g.state==='succeeded'&&g.asset_id);
  if(!candidates.length)detail.append(el('p','Создайте варианты локально или загрузите изображение из Google Flow.','muted'));
  else{
    const grid=el('div',undefined,'candidate-grid');
    for(const [index,generation] of candidates.entries()){
      const card=button('',()=>pickReference('prop',prop,generation.asset_id),'candidate'+(choices.item===generation.asset_id?' selected':''),`prop-candidate-${prop.id}-${index}`);
      card.disabled=!referencePairReady('prop',prop);
      card.append(image(generation.asset_id,`${prop.name}, вариант ${index+1}`));
      const foot=el('footer');
      foot.append(el('span',`Вариант ${index+1}`),el('span',choices.item===generation.asset_id?'✓ Выбран':'Выбрать'));
      card.append(foot);
      grid.append(card);
    }
    detail.append(grid);
  }
  const lock=button('Закрепить эталон',async()=>{
    if(!choices.item)throw Error('Выберите вариант предмета или загрузите файл.');
    await action(`/props/${prop.id}/lock`,{revision:prop.revision,references:choices},'Предмет зафиксирован');
  },'success-button',`lock-prop-${prop.id}`);
  lock.disabled=!referencePairReady('prop',prop)||!choices.item;detail.append(lock);
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

function getCandidateReferenceAsset(kind, item){
  if(!item) return null;
  if(kind === 'character'){
    return picked.get(item.id)?.portrait || (item.references?.portrait) || latestSubjectAsset(item.id);
  }
  if(kind === 'location'){
    return picked.get('location:'+item.id)?.environment || (item.references?.environment) || (item.casting ? item.casting.selected_asset_id : null) || referenceJobs('location', item).filter(g=>g.state==='succeeded'&&g.asset_id)[0]?.asset_id;
  }
  if(kind === 'prop'){
    return picked.get('prop:'+item.id)?.item || (item.references?.item || item.references?.prop) || (item.casting ? item.casting.selected_asset_id : null) || referenceJobs('prop', item).filter(g=>g.state==='succeeded'&&g.asset_id)[0]?.asset_id;
  }
  return null;
}

function unconfirmedReadyReferences(){
  if(!project) return [];
  const result = [];
  const subjects = referenceSubjects();
  for(const c of subjects){
    if(!c.locked){
      const asset = getCandidateReferenceAsset('character', c);
      if(asset) result.push({kind: 'character', item: c, asset});
    }
  }
  for(const l of (project.locations||[])){
    if(!l.locked){
      const asset = getCandidateReferenceAsset('location', l);
      if(asset) result.push({kind: 'location', item: l, asset});
    }
  }
  for(const p of (project.props||[])){
    if(!p.locked){
      const asset = getCandidateReferenceAsset('prop', p);
      if(asset) result.push({kind: 'prop', item: p, asset});
    }
  }
  return result;
}

async function autoAcceptReadyReferences(){
  if(!current || !project) return;
  try {
    const res = await api(`/projects/${current}/accept-references`, {});
    if(res?.total_accepted){
      showNotice(`Автопринятие: эталоны зафиксированы (${res.total_accepted})`);
      await refresh(true);
      return res;
    }
  } catch (err) {
    console.warn('Backend accept-references error, falling back:', err);
  }

  const ready = unconfirmedReadyReferences();
  if(!ready.length) return;
  for(const ref of ready){
    try {
      if(ref.kind === 'character'){
        await api(`/characters/${ref.item.id}/lock`, {
          revision: ref.item.revision,
          references: {portrait: ref.asset},
          review: {one_person_confirmed: true}
        });
      } else if(ref.kind === 'location'){
        await api(`/locations/${ref.item.id}/lock`, {
          revision: ref.item.revision,
          references: {environment: ref.asset}
        });
      } else if(ref.kind === 'prop'){
        await api(`/props/${ref.item.id}/lock`, {
          revision: ref.item.revision,
          references: {item: ref.asset, prop: ref.asset}
        });
      }
    } catch (e) {
      console.warn(`Failed to auto-lock ${ref.kind} ${ref.item.name}:`, e);
    }
  }
  showNotice(`Автопринятие: эталоны зафиксированы (${ready.length})`);
  await refresh(true);
}

function sceneBatchBlockReason({allowMissingReferences=false}={}){
  const errors=project.preflight?.errors||[];
  const analysisErrors=[...new Map(errors.filter(item=>item.code!=='missing_required_reference').map(item=>[item.message||item.code,item])).values()];
  if(project.preflight?.ok===false&&analysisErrors.length)
    return 'Генерация заблокирована: '+analysisErrors.slice(0,3).map(item=>item.message||item.code).join('; ');

  if(allowMissingReferences)return null;
  const resources=project.reference_requirements||[];
  const missing=[];

  for(const req of resources){
    if(req.ready) continue;
    let hasCandidate = false;
    let isActive = false;
    if(req.kind === 'character'){
      const c = project.characters?.find(x=>x.id===req.id);
      if(c){
        hasCandidate = Boolean(getCandidateReferenceAsset('character', c));
        isActive = project.generations?.some(g=>g.character_id===c.id && isActiveGeneration(g));
      }
    } else if(req.kind === 'location'){
      const l = project.locations?.find(x=>x.id===req.id);
      if(l){
        hasCandidate = Boolean(getCandidateReferenceAsset('location', l));
        isActive = project.generations?.some(g=>g.location_id===l.id && isActiveGeneration(g));
      }
    } else if(req.kind === 'prop'){
      const p = project.props?.find(x=>x.id===req.id);
      if(p){
        hasCandidate = Boolean(getCandidateReferenceAsset('prop', p));
        isActive = project.generations?.some(g=>g.prop_id===p.id && isActiveGeneration(g));
      }
    }

    if(isActive){
      return `Идёт создание эталона «${req.name}». Дождитесь завершения генерации.`;
    }
    if(!hasCandidate){
      missing.push(req.name);
    }
  }

  for(const err of errors){
    if(err.code === 'missing_required_reference' && err.details?.anchor_name){
      const anchorName = err.details.anchor_name;
      const c = project.characters?.find(x=>x.name===anchorName || x.canonical_name===anchorName);
      const l = project.locations?.find(x=>x.name===anchorName);
      const p = project.props?.find(x=>x.name===anchorName);
      const hasCandidate = (c && getCandidateReferenceAsset('character', c)) ||
                           (l && getCandidateReferenceAsset('location', l)) ||
                           (p && getCandidateReferenceAsset('prop', p));
      if(!hasCandidate && !missing.includes(anchorName)){
        missing.push(anchorName);
      }
    }
  }

  if(missing.length)
    return 'Сначала сгенерируйте или загрузите эталоны для: '+missing.map(name=>`«${name}»`).join(', ');

  if(project.preflight?.ok===false)
    return errors.map(item=>item.message||item.code).join('; ')||'Проект не прошёл проверку перед генерацией.';

  return '';
}

let sceneBatchStarting=false;

function flowPortraitPlan(){
  const missing=[],ready=[];
  for(const subject of referenceSubjects()){
    const saved=subject.references?.portrait;
    if(subject.locked&&saved)continue;
    const variants=(project.generations||[]).filter(g=>
      g.character_id===subject.id&&g.character_revision===subject.revision&&
      g.state==='succeeded'&&g.asset_id&&!g.snapshot?.wardrobe_key);
    const selected=picked.get(subject.id)?.portrait;
    const asset=variants.find(g=>g.asset_id===selected)?.asset_id||variants.at(-1)?.asset_id||saved;
    if(asset)ready.push({subject,asset});
    else missing.push(subject);
  }
  return {missing,ready};
}

async function waitForFlowSceneCommand(pid,commandId){
  let last=null;
  for(let attempt=0;attempt<30;attempt++){
    await new Promise(resolve=>setTimeout(resolve,500));
    if(current!==pid)throw Error('Открытый проект изменился. Проверьте запуск в Flow.');
    last=await api('/bridge/v1/status');
    flowBridgeStatus=last;
    if(last.connected&&last.project_id===pid&&last.last_ack_command_id===commandId)return last;
  }
  throw Error('Flow пока не подтвердил команду. Откройте вкладку FrameForge Bridge и проверьте её состояние.');
}

async function startAllScenes(){
  if(sceneBatchStarting)return;
  const pid=current,isFlow=currentSettings?.image_backend==='flow';
  if(!pid||!project?.scenes?.length)throw Error('Сначала откройте проект с кадрами.');
  const count=project.scenes.length;
  const sameProject=()=>{
    if(current!==pid||project?.id!==pid||(currentSettings?.image_backend==='flow')!==isFlow)
      throw Error('Открытый проект или способ генерации изменился. Запуск остановлен.');
  };
  sceneBatchStarting=true;
  try{
    if(isFlow){
      const bridge=await api('/bridge/v1/status');sameProject();
      if(!bridge.connected)throw Error('Мост Flow не подключён. Откройте FrameForge Bridge и нажмите «Проверить связь», затем повторите запуск.');
      if(!bridge.staged_image_batch)throw Error('Обновите FrameForge Flow Bridge до 0.1.82 и перезагрузите вкладку Flow.');
      if(bridge.running||bridge.pending_command==='start'||bridge.active_image_requests){
        if(bridge.project_id!==pid)throw Error('Flow занят другим проектом. Дождитесь завершения или поставьте его на паузу.');
        showNotice('Генерация этого проекта уже запущена в Flow.');return;
      }
      if(bridge.pending_command)throw Error('Flow ещё обрабатывает предыдущую команду. Дождитесь ответа моста.');
      if(bridge.project_id!==pid){
        showNotice('Подключаю Flow к открытому проекту…');
        const check=await api('/bridge/v1/command',{action:'check',project_id:pid});
        await waitForFlowSceneCommand(pid,check.command_id);sameProject();
      }
      await refresh(true);sameProject();
      const {missing,ready}=flowPortraitPlan();
      const stage=missing.length?'portraits':'scenes';
      if(stage==='scenes'){
        const reason=sceneBatchBlockReason({allowMissingReferences:true});if(reason)throw Error(reason);
        if(ready.length){
          if(!confirm(`Портреты проверены и подходят? Закрепить ${ready.length} и запустить сцены во Flow?`))return;
          for(const {subject,asset} of ready){
            sameProject();
            await api(`/characters/${subject.id}/lock`,{
              revision:subject.revision,references:{portrait:asset},review:{one_person_confirmed:true}
            });
          }
          await refresh(true);sameProject();
        }
      }
      const command=await api('/bridge/v1/command',{action:'start',project_id:pid,images_only:true,batch_stage:stage});
      showNotice(stage==='portraits'?'Flow: запускаю недостающие портреты…':'Flow: запускаю сцены…');
      const status=await waitForFlowSceneCommand(pid,command.command_id);
      if(status.running||status.active_image_requests>0)
        showNotice(stage==='portraits'
          ? `Flow начал подготовку портретов: ${missing.length}. После проверки нажмите эту кнопку снова.`
          : `Flow подтвердил запуск изображений: ${count} кадров в плане.`);
      else if(stage==='portraits'&&status.status?.startsWith('Портреты готовы к проверке'))
        showNotice(status.status);
      else if(status.status)
        throw Error(`Flow принял команду и остановился: ${status.status}`);
      else showNotice('Flow принял команду. Состояние появится в панели моста.');
    }else{
      await autoAcceptReadyReferences();sameProject();
      await refresh(true);sameProject();
      const reason=sceneBatchBlockReason();if(reason)throw Error(reason);
      await action(`/projects/${pid}/batch`,{idempotency_key:crypto.randomUUID()},'Все сцены истории поставлены в очередь');
    }
  }finally{
    sceneBatchStarting=false;
    if(current===pid){
      try{await refresh(true);}catch(error){console.warn('Не удалось обновить состояние генерации:',error);}
      render();
    }
  }
}

function renderBatchActionBar(){
  if(!project.scenes?.length) return;

  const bar = el('section', undefined, 'batch-action-bridge');
  const inner = el('div', undefined, 'batch-action-inner');

  const left = el('div', undefined, 'batch-action-info');
  const count = project.scenes.length;
  const completed = project.scenes.filter(s => sceneDisplayGeneration(s)?.asset_id).length;

  const isFlow = currentSettings?.image_backend === 'flow';
  const portraits = isFlow ? flowPortraitPlan() : null;
  const readyCount = portraits ? portraits.ready.length : unconfirmedReadyReferences().length;

  const title = el('div', undefined, 'batch-action-title');
  const badge = el('span', isFlow ? 'FLOW' : 'КАДРЫ', 'batch-action-badge' + (isFlow ? ' flow-badge' : ''));
  title.append(badge, el('strong', isFlow ? 'Генерация всей истории в Google Flow' : 'Генерация кадров истории'));
  left.append(title);

  const sub = el('div', undefined, 'batch-action-subtitle muted');
  if(portraits?.missing.length){
    sub.textContent = `Шаг 1 из 2 · Нужно создать портреты: ${portraits.missing.length}. Сцены запускаются после проверки.`;
  } else if(portraits?.ready.length){
    sub.textContent = `Шаг 2 из 2 · Проверьте портреты (${readyCount}) в разделе «Персонажи». Затем подтвердите их и запустите сцены.`;
  } else if(!isFlow&&readyCount>0){
    sub.textContent = `Вся история: ${count} сцен (100% сюжета) · Готовые эталоны (${readyCount}) будут приняты автоматически`;
  } else {
    sub.textContent = `Вся история: ${count} сцен (100% сюжета) · Готово ${completed} из ${count}`;
  }
  left.append(sub);

  const right = el('div', undefined, 'batch-action-buttons');

  if(!isFlow&&readyCount>0){
    const acceptBtn = button(`✓ Принять эталоны (${readyCount})`, async () => {
      await autoAcceptReadyReferences();
      showNotice(`Все готовые эталоны (${readyCount}) успешно зафиксированы!`);
      await refresh(true);
    }, 'secondary batch-accept-btn', 'auto-accept-references');
    right.append(acceptBtn);
  }

  let generateBtnText = 'Сгенерировать все кадры';
  if(portraits?.missing.length)generateBtnText='▶ Сгенерировать портреты во Flow';
  else if(portraits?.ready.length)generateBtnText='✓ Подтвердить портреты и запустить сцены';
  else if(isFlow)generateBtnText='▶ Сгенерировать все сцены во Flow';
  const generateBtn = button(generateBtnText, startAllScenes, 'primary batch-generate-btn', 'generate-scenes');
  generateBtn.disabled=sceneBatchStarting;
  right.append(generateBtn);

  const animatedCount = project.scenes.filter(s => s.video_asset_id).length;
  const chooseAnimations = button(`Выбрать анимацию · ${animatedCount} / ${project.scenes.length}`, () => {
    const picker = main.querySelector('[data-testid="animation-model-toolbar"]');
    picker?.scrollIntoView({behavior: 'smooth', block: 'center'});
    picker?.focus({preventScroll: true});
  }, 'secondary batch-accept-btn', 'animation-open-selection');
  right.append(chooseAnimations);

  inner.append(left, right);
  bar.append(inner);
  main.append(bar);
}
async function runStoryAudit(){
  if(storyAuditBusy)return;
  const pid=current,ids=project.scenes.filter(s=>sceneDisplayGeneration(s)?.asset_id).map(s=>s.id);
  if(!ids.length)throw Error('Сначала дождитесь готовых кадров.');
  storyAuditBusy=true;storyAuditStop=false;
  try{
    for(let i=0;i<ids.length&&!storyAuditStop&&current===pid;i+=4){
      storyAuditProgress=`Обработано ${i} / ${ids.length}`;render();
      const result=await api(`/projects/${pid}/story-audit`,{scene_ids:ids.slice(i,i+4)});
      await refresh(true);
      if(result.stop_audit??result.items.some(x=>x.status==='unverified')){
        showNotice('Gemini не завершил проверку пакета. Результат сохранён; аудит можно повторить.');
        break;
      }
      storyAuditProgress=`Обработано ${Math.min(i+4,ids.length)} / ${ids.length}`;
    }
  }finally{storyAuditBusy=false;if(current===pid){await refresh(true);render();}}
}

function renderStoryAudit(box){
  if(!isForgottenCitiesProject())return;
  const panel=el('details'),heading=el('summary','Аудит кадров · Gemini');panel.append(heading);
  panel.open=storyAuditBusy||storyAuditExpanded;
  panel.addEventListener('toggle',()=>{storyAuditExpanded=panel.open;});
  const controls=el('div',undefined,'scene-toolbar');
  const start=button(storyAuditBusy?'Остановить аудит':'Проверить готовые кадры',async()=>{
    if(storyAuditBusy){storyAuditStop=true;return;}await runStoryAudit();
  },'secondary');controls.append(start);
  controls.append(button('Добавить недостающие эталоны',async()=>{
    await action(`/projects/${current}/upgrade-artifact-references`,{},'Эталоны находок обновлены. Выберите варианты в реквизите.');
  },'secondary'));
  if(storyAuditProgress)controls.append(el('span',storyAuditProgress,'muted'));
  panel.append(controls,el('p','Проверка сохраняет замечания и промпты. Замена изображения запускается только для выбранных рекомендаций.','muted'));
  const items=project.story_audit||[],labels={keep:'Соответствует',meaning:'Смысловая ошибка',variety:'Разнообразить монтаж',uncertain:'Нужна оценка',unverified:'Не проверено'};
  panel.append(el('p',`Сохранено проверок: ${items.length}. Соответствуют: ${items.filter(x=>x.status==='keep'&&!x.stale).length}. Смысловые замечания: ${items.filter(x=>x.status==='meaning'&&!x.stale).length}. Для разнообразия: ${items.filter(x=>x.status==='variety'&&!x.stale).length}.`,'muted'));
  const selectableItems=items.filter(x=>!x.stale&&!x.batch_id&&['meaning','variety'].includes(x.status));
  const selectionInputs=new Map(),selectionCount=el('span',undefined,'muted');
  function syncSelection(){
    for(const [id,input] of selectionInputs)input.checked=storyAuditSelected.has(id);
    selectionCount.textContent=`Выбрано: ${selectableItems.filter(x=>storyAuditSelected.has(x.id)).length} / ${selectableItems.length}`;
  }
  const selectionControls=el('div',undefined,'scene-toolbar');
  const selectAll=button('Выбрать все',()=>{for(const item of selectableItems)storyAuditSelected.add(item.id);syncSelection();},'secondary','audit-select-all');
  const clearSelection=button('Снять выбор',()=>{for(const item of items)storyAuditSelected.delete(item.id);syncSelection();},'secondary','audit-clear-selection');
  selectAll.disabled=storyAuditRegenerating||!selectableItems.length;
  clearSelection.disabled=storyAuditRegenerating;
  selectionControls.append(selectAll,clearSelection,selectionCount);panel.append(selectionControls);
  for(const item of items){
    if(item.status==='keep'&&!item.stale)continue;
    const row=el('div',undefined,'reference-row'),line=el('div');
    const selectable=!item.stale&&!item.batch_id&&['meaning','variety'].includes(item.status);
    if(selectable){const check=el('input');check.type='checkbox';check.checked=storyAuditSelected.has(item.id);
      selectionInputs.set(item.id,check);
      check.onchange=()=>{if(check.checked)storyAuditSelected.add(item.id);else storyAuditSelected.delete(item.id);syncSelection();};line.append(check);}
    line.append(el('strong',`Кадр ${item.position}: ${labels[item.status]||item.status}${item.batch_id?' · замена запущена':item.stale?' · кадр изменён':''}`));
    row.append(line,el('p',item.reason),el('p',item.observed,'muted'));
    if(item.corrected_prompt){const prompt=el('details');prompt.append(el('summary','Исправленный промпт'),el('p',item.corrected_prompt));row.append(prompt);}
    panel.append(row);
  }
  syncSelection();
  const regenerate=button('Перегенерировать выбранные',async()=>{
    if(storyAuditRegenerating)return;
    const pid=current,selected=items.filter(x=>storyAuditSelected.has(x.id)&&!x.stale&&!x.batch_id&&['meaning','variety'].includes(x.status));
    if(!selected.length)throw Error('Выберите рекомендации для замены.');
    storyAuditRegenerating=true;regenerate.disabled=true;
    try{for(const item of selected){await api(`/projects/${pid}/story-audit-regenerate`,{audit_id:item.id});storyAuditSelected.delete(item.id);}
      showNotice('Выбранные кадры поставлены в очередь с исправленными промптами.');
    }finally{storyAuditRegenerating=false;await refresh(true);}
  },'secondary');regenerate.disabled=storyAuditRegenerating;
  panel.append(regenerate);box.append(panel);
}

function renderScenes(){
  const box=workspace('Сгенерированные кадры','▣'),toolbar=el('div',undefined,'scene-toolbar'),info=el('div');
  const completed=project.scenes.filter(scene=>sceneDisplayGeneration(scene)?.asset_id).length;
  info.append(el('strong',`${completed} из ${project.scenes.length}`),el('div','Готовые изображения появляются здесь автоматически.','muted'));
  toolbar.append(info);
  const blockReason=sceneBatchBlockReason();
  if(stillApprovalRequired()){
    const pending=project.scenes.filter(s=>!sceneStillApproved(s)&&s.active_generation_id&&sceneDisplayGeneration(s)?.asset_id&&!s.stale).length;
    const approve=button(stillApprovalBusy?stillApprovalProgress:'Утвердить все',approveAllSelectedStills,'success-button','approve-all-stills');
    approve.disabled=stillApprovalBusy||!pending;approve.title=pending?'Утвердить текущий выбранный вариант каждого готового кадра.':'Все актуальные выбранные изображения уже утверждены; единственный вариант утверждается автоматически.';toolbar.append(approve);
  }

  const controls=batchControls();if(controls)toolbar.append(controls);box.append(toolbar);
  renderStoryAudit(box);
  if(blockReason)box.append(el('p',blockReason,'error'));
  
  box.append(renderAnimationSelectionBar());

  const grid=el('div',undefined,'scene-grid');for(const scene of project.scenes)grid.append(renderSceneCard(scene));box.append(grid);main.append(box);
}
async function assembleProjectMp4(projectId, control){
  const original=control.textContent;
  control.textContent='Собираю MP4…';
  showNotice('Сборка MP4 выполняется. Дождитесь ссылки на файл.');
  try{
    const result=await api(`/projects/${projectId}/animation/assemble`,{});
    if(!result?.url)throw Error('Сервер не вернул ссылку на собранный MP4.');
    await refresh(true);
    showNotice('MP4 собран. Нажмите «Скачать MP4» рядом с кнопкой сборки.');
    return result;
  }finally{control.textContent=original;}
}
function sceneGenerations(scene){return project.generations.filter(g=>g.purpose==='scene'&&g.scene_id===scene.id&&(g.scene_revision===scene.current_revision||g.current_scene_compatible));}
function sceneVariants(scene){
  const images=new Map();
  for(const generation of sceneGenerations(scene)){
    if(generation.state!=='succeeded'||!generation.asset_id)continue;
    const previous=images.get(generation.asset_id);
    // Legacy duplicate queues can share one upload. Keep its selected row so
    // selection and approval actions still refer to the persisted generation.
    if(!previous||generation.id===scene.active_generation_id)images.set(generation.asset_id,generation);
  }
  return [...images.values()];
}
function sceneDisplayGeneration(scene){const active=project.generations.find(g=>g.id===scene.active_generation_id);return active||[...sceneGenerations(scene)].reverse().find(g=>g.state==='succeeded'&&g.asset_id);}
function isForgottenCitiesProject(){return project?.channel_profile?.profile_type==='forgotten_cities';}
function stillApprovalRequired(){return project?.still_approval_required===true;}
function sceneStillApproved(scene){return !stillApprovalRequired()||scene?.still_approval?.status==='approved';}

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
  ['gentle_push_in','Мягкий наезд (12%)'],['gentle_pull_back','Мягкое отдаление (12%)'],['gentle_pan_left','Спокойный проход влево'],['gentle_pan_right','Спокойный проход вправо'],
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
    if(anim.selected_engine==='omni_flash')return [`⚡ Видео: Omni · ${(anim.omni||{}).resolution||'720p'} · ${scene.video_metadata?.duration_seconds||anim.actual_duration_seconds||'—'} сек.`,'badge-omni'];
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
  const approvalRequired=stillApprovalRequired();
  const approved=sceneStillApproved(scene);
  if(approvalRequired){
    const review=scene.still_approval||{};
    const text=approved?(review.automatic?'✓ Единственный вариант — можно анимировать':'✓ Изображение утверждено для анимации'):review.status==='rejected'?'✕ Изображение отклонено. Выберите или создайте другой вариант.':'Ожидает утверждения изображения';
    box.append(el('div',text,'still-review-banner '+(approved?'approved':review.status==='rejected'?'rejected':'pending')));
  }
  const row=el('div',undefined,'animation-control-row');
  const [badgeText,badgeClass]=animationBadge(scene,anim);
  const badge=el('span',badgeText,'scene-anim-badge '+badgeClass); badge.dataset.testid=`scene-anim-badge-${scene.position}`;
  const locked=animationLocked(scene),chosen=omniSelection().has(scene.id);
  const effectiveEngine=locked?(anim.selected_engine||'ken_burns'):(chosen?selectedAnimationEngine():'ken_burns');
  const model=animationModelName(locked?anim.selected_engine:selectedAnimationEngine());
  const chooseLabel=el('label',undefined,'omni-choice'),choose=el('input');choose.type='checkbox';choose.checked=locked?['omni_flash','veo_lite'].includes(effectiveEngine):chosen;choose.disabled=locked||animationRunBusy;choose.dataset.testid=`omni-select-${scene.position}`;choose.setAttribute('aria-label',`Анимировать кадр ${scene.position} в ${model}`);
  chooseLabel.append(choose,el('strong',locked&&effectiveEngine==='ken_burns'?'2.5D':model),el('small',locked?(scene.video_asset_id?'Видео готово':['queued_omni','queued_veo'].includes(anim.status)?'Ожидает запуска в Flow':'Генерируется в Flow'):'Отметьте для Google Flow'));
  choose.onchange=()=>{
    choose.checked?omniSelection().add(scene.id):omniSelection().delete(scene.id);
    const disclosure=box.querySelector('.animation-disclosure');
    if(disclosure)animationDisclosures.set(project.id+':'+scene.id,disclosure.open);
    storeOmniSelection();
    // Keep the gallery, previews and scroll containers mounted while choosing frames.
    const replacement=renderCompactAnimationControl(scene,display,anim);
    box.replaceWith(replacement);
    replacement.querySelector('input[type="checkbox"]')?.focus({preventScroll:true});
  };
  if(!locked){const motion=smartMotion(scene.data,scene.position),manual=anim.ken_burns_mode==='manual';badge.textContent=chosen?`Выбран ${model}`:`2.5D · ${manual?(Object.fromEntries(KB_MOTION_OPTIONS)[anim.ken_burns_preset]||'ручная камера'):'Авто: '+(Object.fromEntries(KB_MOTION_OPTIONS)[motion.preset]||'мягкое движение').toLowerCase()}`;badge.className='scene-anim-badge '+(chosen?(effectiveEngine==='veo_lite'?'badge-veo':'badge-omni'):'badge-kb');}
  row.append(chooseLabel,badge);
  if(scene.video_asset_id||anim.status==='completed'){
    const remove=el('button','Удалить видео','btn-delete-scene-video'); remove.type='button';remove.dataset.testid=`delete-video-btn-${scene.position}`;remove.setAttribute('aria-label',`Удалить видео кадра ${scene.position} и вернуть статичное изображение`);
    remove.onclick=async e=>{e.stopPropagation();try{await api(`/scenes/${scene.id}/animation/delete`,{});showNotice(`Видео кадра ${scene.position} удалено.`);await refresh(true);}catch(error){showNotice(`Ошибка удаления: ${error.message||error}`,true);}}; row.append(remove);
  }
  box.append(row);
  const actual=scene.video_metadata?.duration_seconds||anim.actual_duration_seconds;
  const requested=(anim[anim.selected_engine==='omni_flash'?'omni':'veo']||{}).duration_seconds||anim.duration_seconds;
  if(['omni_flash','veo_lite'].includes(anim.selected_engine)&&scene.video_asset_id&&actual&&requested&&Math.abs(actual-requested)>0.15){
    const hint=el('small',`Запрошено ${requested} сек. · Flow вернул ${actual} сек.`,'animation-single-hint');hint.dataset.testid=`animation-duration-hint-${scene.position}`;box.append(hint);
  }
  if(['queued_omni','queued_veo'].includes(anim.status)){
    const resume=button('Продолжить в Flow',()=>resumeSceneAnimation(scene),'success-button',`animation-resume-${scene.position}`);
    const pending=el('div',undefined,'animation-single-actions');pending.append(resume);box.append(pending,el('small','Видео ещё не создано. Откройте вкладку Flow с подключённым мостом и продолжите запуск.','animation-single-hint'));
  }
  if(!locked){
    const trials=el('div',undefined,'animation-single-actions');
    const cloud=button(`Проба в ${animationModelName(selectedAnimationEngine())}`,()=>runSingleAnimation(scene.id,selectedAnimationEngine()),'success-button',`animation-test-cloud-${scene.position}`);
    const local=button('Проба 2.5D',()=>runSingleAnimation(scene.id,'ken_burns'),'secondary',`animation-test-local-${scene.position}`);
    for(const control of [cloud,local]){control.disabled=animationRunBusy||!approved||!display?.asset_id||scene.stale;if(!approved)control.title='Сначала нажмите «Утвердить» под выбранным изображением.';}
    trials.append(cloud,local);box.append(trials,el('small',!approved?'Сначала нажмите «Утвердить» под выбранным изображением.':'Проба запускает только этот кадр. Результат появится на карточке.','animation-single-hint'));
  }
  const disclosure=document.createElement('details'); disclosure.className='animation-disclosure'; disclosure.dataset.testid=`scene-animation-disclosure-${scene.position}`;
  disclosure.open=animationDisclosures.get(project.id+':'+scene.id)||false;
  const summary=el('summary',effectiveEngine==='omni_flash'?'Качество и длительность Omni':'Настроить движение камеры');summary.setAttribute('aria-label',`Настроить анимацию кадра ${scene.position}`); disclosure.append(summary);
  const params=el('div',undefined,'animation-parameters');
  const engine=effectiveEngine;
  if(engine==='ken_burns'){
    const preset=selectOptions([['auto','Авто — по сцене'],...KB_MOTION_OPTIONS],anim.ken_burns_mode==='manual'?anim.ken_burns_preset:'auto',`ken-burns-motion-${scene.position}`,'Движение 2.5D Ken Burns');
    preset.disabled=locked||animationRunBusy;
    preset.onchange=()=>saveCameraParameters(`/scenes/${scene.id}/animation/select`,{engine:'ken_burns',ken_burns_preset:preset.value},`Кадр ${scene.position}: движение 2.5D сохранено.`).catch(e=>showNotice(e.message,true));
    const run=el('button','Анимировать 2.5D','btn-animate-kb');run.type='button';run.dataset.testid=`btn-animate-kb-${scene.position}`;
    run.disabled=!approved||locked||animationRunBusy; if(!approved)run.title='Сначала утвердите текущий вариант изображения.';
    run.onclick=async()=>{run.disabled=true;try{await runSelectedAnimations(scene.id);}catch(error){showNotice(`Ошибка 2.5D: ${error.message||error}`,true);run.disabled=false;}};
    params.append(preset,el('small',anim.ken_burns_mode==='manual'?'Движение выбрано вручную.':smartMotion(scene.data,scene.position).reason,'camera-reason'),run);
  }else if(engine==='omni_flash'){
    const omni=anim.omni||{};const resolution=selectOptions([['360p','360p'],['720p','720p']],omni.resolution||'720p',`omni-resolution-${scene.position}`,'Качество Omni');
    const duration=selectOptions([[4,'4 сек.'],[6,'6 сек.'],[8,'8 сек.'],[10,'10 сек.']].map(([a,b])=>[String(a),b]),String(omni.duration_seconds||6),`omni-duration-${scene.position}`,'Длительность Omni');
    resolution.disabled=duration.disabled=locked||animationRunBusy;
    const persist=()=>saveCameraParameters(`/scenes/${scene.id}/animation/select`,{engine:'omni_flash',omni_resolution:resolution.value,duration_seconds:Number(duration.value)});
    resolution.onchange=async()=>{try{await persist();showNotice('Параметры Omni сохранены.');}catch(error){showNotice(error.message||String(error),true);}}; duration.onchange=resolution.onchange;
    const run=el('button','Анимировать Omni','btn-animate-omni');run.type='button';run.dataset.testid=`btn-animate-omni-${scene.position}`;
    run.disabled=!approved||locked||animationRunBusy; if(!approved)run.title='Сначала утвердите текущий вариант изображения.';
    run.onclick=async()=>{run.disabled=true;try{await persist();await refresh(true);await runSelectedAnimations(scene.id);}catch(error){showNotice(`Omni не запущен: ${error.message||error}`,true);run.disabled=false;}};
    params.append(el('strong','Omni через Google Flow'),resolution,duration,run,el('small','Промпт создаётся автоматически из действия сцены и движения камеры.','muted'),el('small','Использует подключённый FrameForge Bridge. Отдельный ключ не нужен.','muted'));
  }else if(engine==='veo_lite'){
    const camera=(anim.veo||{}).camera_motion||anim.camera_motion||'subtle';
    const motions=VEO_MOTION_OPTIONS.some(([id])=>id===camera)?VEO_MOTION_OPTIONS:[[camera,'Авто — по сцене'],...VEO_MOTION_OPTIONS];
    const motion=selectOptions(motions,camera,`veo-motion-${scene.position}`,'Движение камеры Veo');motion.disabled=locked||animationRunBusy;
    const persist=()=>saveCameraParameters(`/scenes/${scene.id}/animation/select`,{engine:'veo_lite',camera_motion:motion.value},`Кадр ${scene.position}: движение Veo сохранено.`);
    motion.onchange=()=>persist().catch(error=>showNotice(error.message,true));
    const run=el('button','Анимировать Veo','btn-animate-veo');run.type='button';run.dataset.testid=`btn-animate-veo-${scene.position}`;
    run.disabled=!approved||locked||animationRunBusy; if(!approved)run.title='Сначала утвердите текущий вариант изображения.';
    run.onclick=async()=>{run.disabled=true;try{await persist();await runSelectedAnimations(scene.id);}catch(error){showNotice(`Veo не запущен: ${error.message||error}`,true);run.disabled=false;}};
    params.append(el('strong','Veo 3.1 Lite через Google Flow'),motion,run,el('small','Промпт создаётся автоматически из действия сцены и движения камеры.','muted'),el('small','Использует кредиты Google Flow.','muted'));
  }else params.append(el('p','Выберите движок, чтобы настроить анимацию этого кадра.','muted'));
  if(['omni_flash','veo_lite'].includes(engine)){
    const scope=selectOptions([['story_action','Действие сцены'],['camera_only','Только движение камеры']],anim.motion_scope||'story_action',`animation-motion-scope-${scene.position}`,'Что анимировать');
    scope.disabled=locked||animationRunBusy;
    scope.onchange=()=>saveCameraParameters(`/scenes/${scene.id}/animation/select`,{engine,motion_scope:scope.value},`Кадр ${scene.position}: способ движения сохранён.`).catch(error=>showNotice(error.message,true));
    params.append(scope,el('small','В режиме камеры люди и предметы сохраняют позы исходного кадра.','muted'));
  }
  if(anim.error){const err=document.createElement('details');err.className='animation-error-details';err.append(el('summary','Ошибка — подробнее'),el('pre',anim.error,'error'));params.append(err);}
  const more=document.createElement('details');more.className='animation-more';more.append(el('summary','Дополнительно'));
  if(locked)params.append(el('small','Чтобы выбрать новую анимацию, сначала завершите задачу или удалите текущее видео.','muted'));
  more.append(button('Другие способы анимации',()=>animateSceneModal(scene),'small-button'));
  const upload=el('button','Загрузить MP4 вручную','small-button');upload.type='button';upload.onclick=e=>{e.preventDefault();triggerFlowMp4Upload(scene);};more.append(upload);params.append(more);
  upload.disabled=!approved; if(!approved)upload.title='Сначала утвердите текущий вариант изображения.';
  disclosure.append(params);box.append(disclosure);return box;
}

function renderSceneCard(scene){
  const card=el('article',undefined,'scene-card');
  let animationControl;
  card.dataset.sceneId=scene.id;
  card.dataset.projectId=project.id;
  card.addEventListener('mouseenter',()=>{lastHoveredCard={type:'scene',id:scene.id};});
  const visual=el('div',String(scene.position),'scene-visual'),generation=sceneDisplayGeneration(scene),latest=[...sceneGenerations(scene)].reverse()[0],display=generation||latest;
    if(generation?.snapshot?.identity_review?.status==='unverified')card.append(el('p',generation.snapshot.identity_review.reason,'muted'));
  
  if(scene.video_asset_id){
    const video=el('video',undefined,'scene-video');
    video.src=`/api/assets/${scene.video_asset_id}`;
    video.autoplay=false;
    video.preload='none';
    video.controls=true;
    if(display?.asset_id)video.poster=`/api/assets/${display.asset_id}`;
    video.loop=true;
    video.muted=true;
    video.playsInline=true;
    video.addEventListener('loadedmetadata',()=>{
      if(!Number.isFinite(video.duration)||video.duration<=0)return;
      scene.video_metadata={...scene.video_metadata,duration_seconds:Math.round(video.duration*1000)/1000};
      const anim=scene.data.animation||{},badge=card.querySelector(`[data-testid="scene-anim-badge-${scene.position}"]`);
      if(badge)badge.textContent=animationBadge(scene,anim)[0];
      const requested=(anim[anim.selected_engine==='omni_flash'?'omni':'veo']||{}).duration_seconds||anim.duration_seconds;
      const control=card.querySelector('.scene-animation-control');
      if(['omni_flash','veo_lite'].includes(anim.selected_engine)&&control&&requested&&Math.abs(video.duration-requested)>0.15){
        let hint=card.querySelector(`[data-testid="animation-duration-hint-${scene.position}"]`);
        if(!hint){hint=el('small',undefined,'animation-single-hint');hint.dataset.testid=`animation-duration-hint-${scene.position}`;control.append(hint);}
        hint.textContent=`Запрошено ${requested} сек. · Flow вернул ${scene.video_metadata.duration_seconds} сек.`;
      }
    });
    video.addEventListener('play',()=>{for(const other of main.querySelectorAll('.scene-video'))if(other!==video)other.pause();});
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
  } else {
    const noLocBadge=el('div',undefined,'scene-ref-badge scene-ref-missing-loc');
    noLocBadge.title='Локация не привязана к кадру. Нажмите, чтобы выбрать или изменить.';
    noLocBadge.style.cssText='background:#7f1d1d;color:#fca5a5;border:1px dashed #ef4444;border-radius:4px;padding:2px 6px;font-size:10px;font-weight:bold;cursor:pointer;display:inline-flex;align-items:center;gap:3px;';
    noLocBadge.append(el('span','⚠️'),el('span','Без локации'));
    noLocBadge.addEventListener('click',(e)=>{e.stopPropagation();editScene(scene);});
    badges.append(noLocBadge);
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
  if(!sceneStillApproved(scene)){animBtn.disabled=true;animBtn.title='Сначала утвердите текущий вариант изображения.';}
  animBtn.addEventListener('click',(e)=>{e.stopPropagation();const control=card.querySelector('.scene-animation-control'),disclosure=control?.querySelector('.animation-disclosure');if(disclosure){disclosure.open=true;control.scrollIntoView({behavior:'smooth',block:'nearest'});}else animateSceneModal(scene);});
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
  if(!locName){
    const unres=el('div',undefined,'scene-unresolved-location');
    unres.style.cssText='display:inline-flex;align-items:center;gap:4px;margin:4px 0 8px;padding:3px 8px;background:rgba(239,68,68,0.15);border:1px dashed #ef4444;border-radius:6px;font-size:11px;color:#fca5a5;cursor:pointer;';
    unres.title='Локация не привязана (заменена пустой строкой). Нажмите, чтобы настроить локацию сцены.';
    unres.append(el('span','⚠️'),el('strong','Локация не определена'),el('small','(нажмите для настройки)','muted'));
    unres.onclick=(e)=>{e.stopPropagation();editScene(scene);};
    body.append(unres);
  }
  if(scene.data.prompt_import){const t=scene.data.prompt_timing,details=el('details');details.append(el('summary',`Промпт · ${formatImportTime(t.start_seconds)}–${formatImportTime(t.end_seconds)}`),el('p',scene.data.manual_prompt),button('Изменить промпт',()=>editScene(scene),'small-button'));body.append(details);}
  const pinnedHero=scene.data?.participant_identity;
  const originHero=Boolean(display?.asset_id)&&project.scenes.some(s=>s.data?.participant_identity?.asset_id===display.asset_id);
  if(pinnedHero||originHero){
    const hero=el('button',pinnedHero?`Эталон героя: кадр ${pinnedHero.reference_position}`:'Закреплён как эталон героя','muted');
    hero.type='button';hero.onclick=()=>openFullscreenViewer(pinnedHero?.asset_id||display.asset_id,'Эталон героя');body.append(hero);
  }
  if(display?.asset_id&&display?.snapshot?.expected_people===1){
    body.append(button('Закрепить героя',()=>pinParticipantModal(scene,display)));
  }
  if(stillApprovalRequired()&&(display?.snapshot?.source_fidelity_review?.status === 'unverified' || display?.snapshot?.quality_review?.status === 'unverified')) {
    body.append(el('p', 'Просмотрите изображение перед ручным утверждением.', 'scene-stale-warning'));
  }

  const identityReview=display?.snapshot?.identity_review;
  if(identityReview?.status==='needs_review'||identityReview?.status==='unverified'){
    body.append(el('p',identityReview.reason,'scene-stale-warning'));
  }
  const compositionReview=display?.snapshot?.composition_review;
  if(compositionReview?.status==='needs_review'||compositionReview?.status==='unverified'){
    body.append(el('p',compositionReview.reason,'scene-stale-warning'));
  }
  const animData = scene.data?.animation || {};
  const animInfo = renderCompactAnimationControl(scene, display, animData);
  animationControl = animInfo;
  if(scene.stale)animInfo.append(el('p', `⚠️ Устарело: ${scene.stale_reason || 'изменился исходный кадр или сценарий'}`, 'scene-stale-warning'));
  body.append(animInfo);

  // Variants row for current revision
  const variants = sceneVariants(scene);
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

      if(stillApprovalRequired()){
        vFooter.classList.add('still-review-footer');
        const reviewStatus=v.review?.status||'pending_review';
        const reviewLabels={pending_review:'Ожидает проверки',approved:'✓ Утверждён',rejected:'✕ Отклонён'};
        const reviewBadge=el('span',v.review?.automatic?'✓ Единственный вариант':reviewLabels[reviewStatus]||reviewStatus,`still-review-status ${reviewStatus}`);
        if(v.review?.continuity?.visual_status==='unavailable')reviewBadge.title='Структурные проверки пройдены; архитектура, стиль и эпоха подтверждаются человеком.';
        vFooter.append(reviewBadge);
      }

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
      if(stillApprovalRequired()){
        const reviewStatus=v.review?.status||'pending_review';
        const reviewActions=el('div',undefined,'still-review-actions');
        if(reviewStatus!=='approved'){
          const approve=el('button','Утвердить','still-approve-btn');approve.type='button';approve.dataset.testid=`approve-generation-${v.id}`;
          approve.onclick=async e=>{e.stopPropagation();await action(`/generations/${v.id}/review`,{decision:'approved'},`Кадр ${scene.position}: вариант утверждён для анимации`);};
          reviewActions.append(approve);
        }
        if(reviewStatus!=='rejected'){
          const reject=el('button','Отклонить','still-reject-btn');reject.type='button';reject.dataset.testid=`reject-generation-${v.id}`;
          reject.onclick=async e=>{e.stopPropagation();await action(`/generations/${v.id}/review`,{decision:'rejected',reason_code:'manual_visual_rejection'},`Кадр ${scene.position}: вариант отклонён`);};
          reviewActions.append(reject);
        }
        vItem.append(reviewActions);
      }
      variantsBox.append(vItem);
    });
    body.append(variantsBox);
  }

  if(display?.error)body.append(el('p',display.error,'error'));
  const editorial=editorialControls({scene,enabled:isForgottenCitiesProject()&&currentSettings?.source_contract_enabled,api,button,refresh});if(editorial)body.append(editorial);
  if(isActiveGeneration(display))body.append(progressMeter(display,true));
  card.append(visual,body);
  attachDragAndDrop(card, `/scenes/${scene.id}/upload`, `Кадр ${scene.position} успешно сохранён!`);
  return card;
}

function pinParticipantModal(scene,display){
  const body=modal(`Эталон героя из кадра ${scene.position}`);
  body.append(el('p','Укажи кадры, в которых появляется этот же человек. Его внешность сохранится, а действие и место будут определяться сценарием.'));
  const existing=scene.data?.participant_identity||project.scenes.find(s=>s.data?.participant_identity?.asset_id===display.asset_id)?.data.participant_identity;
  const name=field('Имя или роль героя',existing?.name||'Герой','input');
  const linked=project.scenes.filter(s=>s.data?.participant_identity?.asset_id===display.asset_id).map(s=>s.position);
  const numbers=field('Номера кадров через запятую',linked.join(', '),'input');
  body.append(name.box,numbers.box,button('Закрепить эталон',async()=>{
    const positions=numbers.input.value.split(/[\s,;]+/).filter(Boolean).map(Number);
    if(!positions.length||positions.some(p=>!Number.isInteger(p)||!project.scenes.some(s=>s.position===p))){showNotice('Укажи существующие номера кадров.',true);return;}
    const targets=project.scenes.filter(s=>positions.includes(s.position));
    try{
      await api(`/projects/${current}/pin-participant`,{generation_id:display.id,name:name.input.value,appearance:existing?.appearance||'',scene_ids:targets.map(s=>s.id),revisions:Object.fromEntries(targets.map(s=>[s.id,s.current_revision]))});
      body.closest('dialog').close();await refresh(true);showNotice('Эталон героя закреплён. Обнови изображения выбранных кадров.');
    }catch(e){showNotice(e.message,true);}
  }));
}

function editScene(scene){
  if(scene.data.prompt_import){editImportedPrompt({scene,dialog,notice:showNotice,save:data=>action(`/scenes/${scene.id}/edit`,{revision:scene.current_revision,data},'Промпт сохранён.')});return;}
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
          gentle_push_in: {scale_start:1,scale_end:1.12,pan_x_start:0,pan_x_end:0,pan_y_start:0,pan_y_end:0},
          gentle_pull_back: {scale_start:1.12,scale_end:1,pan_x_start:0,pan_x_end:0,pan_y_start:0,pan_y_end:0},
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
    {id:'gentle_push_in',label:'Мягкий наезд (12%)'},
    {id:'gentle_pull_back',label:'Мягкое отдаление (12%)'},
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
function renderQueue(){const queueGenerations=project.generations.filter(g=>!g.purpose.startsWith('edit_'));renderHead('Состояние генерации');const box=workspace('Очередь задач','◌'),controls=batchControls(true),list=el('div',undefined,'queue-list');if(controls){box.append(controls,el('p','Пауза завершит сохранение текущего кадра и остановит следующие задачи.','muted'));}if(!queueGenerations.length)list.append(el('p','Задач пока нет.','muted'));for(const generation of [...queueGenerations].reverse()){const item=el('div',undefined,'queue-item'),scene=project.scenes.find(s=>s.id===generation.scene_id),subject=project.characters.find(c=>c.id===generation.character_id),location=project.locations?.find(item=>item.id===generation.location_id),prop=project.props?.find(item=>item.id===generation.prop_id),title=scene?`Кадр ${scene.position}`:subject?subject.name:location?`Эталон: ${location.name}`:prop?`Эталон: ${prop.name}`:'Разбор сценария',copy=el('div'),failed=Boolean(generation.error);copy.append(el('strong',title),el('div',failed?generation.error:(generation.progress.stage||generation.purpose),failed?'error':'muted'));if(isActiveGeneration(generation))copy.append(progressMeter(generation,true));item.append(copy,el('span',labels[generation.state]||generation.state,generation.state==='failed'?'error':''));list.append(item);}box.append(list);main.append(box);}
function renderExport(){renderHead('Монтаж и сохранение ролика');const holder=el('div');main.append(holder);const epoch=montageRenderId;montageProjectId=project.id;mountMontage(holder,{project,api,downloadZip:downloadExport,notice:showNotice}).then(editor=>{if(epoch!==montageRenderId){editor.dispose();return;}montageView=editor;}).catch(error=>{if(epoch===montageRenderId){montageProjectId=null;holder.textContent=error.message;showNotice(error.message,true);}});}
async function downloadExport(){const response=await fetch('/api/export/'+current);if(!response.ok){const error=await response.json();throw Error(error.error);}const url=URL.createObjectURL(await response.blob()),link=el('a');link.href=url;link.download='frames.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),5000);}



async function settings(){
  const body=modal('Настройки FrameForge'),config=await api('/settings'),fields={};
  
  const textSection=el('div',undefined,'text-settings');
  textSection.style.cssText='margin:12px 0;padding:12px;background:#17162b;border-radius:10px;border:1px solid #493b70';
  textSection.append(el('h3','⚡ Анализ сценария (DeepSeek Flash)','settings-subtitle'));

  fields['deepseek_api_key']=field('DeepSeek API Key', '');
  fields['deepseek_api_key'].input.type='password';
  fields['deepseek_api_key'].input.autocomplete='new-password';
  fields['deepseek_api_key'].input.placeholder=config.deepseek_key_configured?'Ключ сохранён (из .env или настроек); оставьте пустым, чтобы сохранить':'sk-...';

  const dsHint=el('p','Используется модель deepseek-flash: 2 логических прохода (реестр сущностей и сцены), кэширование по SHA-256 текста. Ключ также считывается из .env.','muted');
  dsHint.style.cssText='font-size:12px;color:#a8b2d1;margin:6px 0;';
  textSection.append(fields['deepseek_api_key'].box, dsHint);
  body.append(textSection);

  const reviewSection=el('div',undefined,'text-settings');
  reviewSection.append(el('h3','Проверка сцен и бесплатная очередь','settings-subtitle'));
  const queueEnabled=el('input');queueEnabled.type='checkbox';queueEnabled.checked=config.google_queue_enabled===true;
  const queueLabel=el('label');queueLabel.append(queueEnabled,el('span','Сохранять проверки Gemini в бесплатной очереди'));
  const contractEnabled=el('input');contractEnabled.type='checkbox';contractEnabled.checked=config.source_contract_enabled===true;
  const contractLabel=el('label');contractLabel.append(contractEnabled,el('span','Использовать точные цитаты и единое задание сцены'));
  reviewSection.append(queueLabel,contractLabel,el('p','Очередь использует подтверждённые бесплатные квоты. Точные цитаты и задание сцены работают отдельно. Автоматическая оценка изображений, эталонов и конечных промптов отменена. Для анимации используется выбранное готовое изображение; отдельное утверждение не требуется.','muted'));
  body.append(reviewSection);

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
    el('p','Для генерации изображений через Google Flow используется ваша подписка на flow.google.com.','muted')
  );
  const flowModelBox=el('label');
  flowModelBox.append(el('span','Модель изображений Flow'));
  const flowModelSelect=el('select');
  for(const model of ['🍌 Nano Banana 2 Lite','🍌 Nano Banana 2','🍌 Nano Banana Pro']){
    const option=el('option',model);option.value=model;flowModelSelect.append(option);
  }
  flowModelSelect.value=config.flow_image_model || '🍌 Nano Banana 2';
  flowModelBox.append(flowModelSelect);

  const bridgeUrlBox=el('label');bridgeUrlBox.append(el('span','Ссылка на ваш FrameForge Bridge'));
  const bridgeUrlInput=el('input');bridgeUrlInput.type='url';bridgeUrlInput.placeholder='https://flow.google.com/project/…/tool/…';bridgeUrlInput.value=config.flow_project_url||'';
  fields.flow_project_url={input:bridgeUrlInput,box:bridgeUrlBox};bridgeUrlBox.append(bridgeUrlInput);flowSection.append(bridgeUrlBox);
  flowSection.append(flowModelBox, el('p','Генерация через Flow работает через подключённый FrameForge Bridge; отдельный API key не требуется.','muted'));
  body.append(flowSection);

  const stockSection=el('div',undefined,'stock-settings');
  stockSection.style.cssText='margin:12px 0;padding:12px;background:#131929;border-radius:10px;border:1px solid #2b395c';
  stockSection.append(el('h3','Источники материалов (Pixabay)','settings-subtitle'));

  fields['pixabay_api_key']=field('Pixabay API Key', '');
  fields['pixabay_api_key'].input.type='password';
  fields['pixabay_api_key'].input.autocomplete='new-password';
  fields['pixabay_api_key'].input.placeholder=config.pixabay_key_configured?'Ключ сохранён в защищённом хранилище (DPAPI); введите новый для замены':'Введите ключ из pixabay.com/api/docs/';

  const stockStatusBox=el('div');
  stockStatusBox.style.cssText='margin:8px 0;font-size:12px;line-height:1.4;';

  const stockBtns=el('div');
  stockBtns.style.cssText='display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center;';

  const checkBtn=button('Проверить подключение',async()=>{
    checkBtn.disabled=true;
    stockStatusBox.textContent='Проверка подключения к Pixabay...';
    stockStatusBox.style.color='#94a3b8';
    try{
      const enteredKey=fields['pixabay_api_key'].input.value.trim();
      const res=await api('/stock/pixabay/check',enteredKey?{key:enteredKey}:{});
      if(res.ok){
        stockStatusBox.style.color='#4ade80';
        stockStatusBox.textContent='✓ ' + (res.message || 'Подключение успешно!') + ' (Лимит: ' + (res.remaining !== undefined ? res.remaining : '?') + '/' + (res.limit !== undefined ? res.limit : '?') + ' в минуту)';
      }else{
        stockStatusBox.style.color='#f87171';
        stockStatusBox.textContent='✗ ' + (res.error || 'Ошибка проверки ключа');
      }
    }catch(e){
      stockStatusBox.style.color='#f87171';
      stockStatusBox.textContent='✗ Ошибка: ' + e.message;
    }finally{
      checkBtn.disabled=false;
    }
  });

  const deleteKeyBtn=button('Удалить ключ',async()=>{
    if(!confirm('Удалить сохранённый ключ Pixabay?'))return;
    deleteKeyBtn.disabled=true;
    try{
      await api('/stock/pixabay/key/delete',{});
      config.pixabay_key_configured=false;
      fields['pixabay_api_key'].input.value='';
      fields['pixabay_api_key'].input.placeholder='Введите ключ из pixabay.com/api/docs/';
      stockStatusBox.style.color='#fbbf24';
      stockStatusBox.textContent='Ключ Pixabay удалён.';
      deleteKeyBtn.remove();
    }catch(e){
      stockStatusBox.style.color='#f87171';
      stockStatusBox.textContent='Ошибка удаления: ' + e.message;
      deleteKeyBtn.disabled=false;
    }
  });

  stockBtns.append(checkBtn);
  if(config.pixabay_key_configured){
    stockBtns.append(deleteKeyBtn);
  }

  const stockHint=el('p','Бесплатный ключ Pixabay позволяет искать и импортировать стоковые фото и видео без внешних платных подписок. Лимит: 100 запросов в минуту.','muted');
  stockHint.style.cssText='font-size:12px;color:#a8b2d1;margin:6px 0;';

  stockSection.append(fields['pixabay_api_key'].box, stockStatusBox, stockBtns, stockHint);
  body.append(stockSection);

  body.append(button('Сохранить настройки',async()=>{
    const data={
      image_backend: currentSettings?.image_backend || 'comfy',
      text_backend: 'deepseek',
      google_queue_enabled:queueEnabled.checked,
      source_contract_enabled:contractEnabled.checked,
      flow_image_model: flowModelSelect.value
    };
    for(const[key,item]of Object.entries(fields)){
      if(key==='job_timeout_seconds') data[key]=Number(item.input.value);
      else if(key==='deepseek_api_key'){
        if(item.input.value.trim()) data[key]=item.input.value.trim();
      }
      else if(key==='pixabay_api_key'){
        if(item.input.value.trim()) data[key]=item.input.value.trim();
      }
      else data[key]=item.input.value;
    }
    currentSettings=await api('/settings',data);
    showNotice('Настройки успешно сохранены.');
    dialog.close();
    render();
  },'primary'));
}

async function openChannelsModal(includeArchived=false){
  const body=modal('Каналы и стили');
  const chList=await refreshChannels(includeArchived);
  const headWrap=el('div');
  headWrap.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px;';
  
  const createBtn=button('＋ Создать новый канал',()=>openChannelEditorModal(null),'primary','create-channel-btn');
  createBtn.style.cssText='width:auto;padding:8px 16px;';
  
  const filterLabel=el('label');
  filterLabel.style.cssText='display:inline-flex;align-items:center;gap:8px;font-size:13px;color:#cbd5e1;cursor:pointer;margin:0;';
  const filterCheck=el('input');
  filterCheck.type='checkbox';
  filterCheck.checked=includeArchived;
  filterCheck.addEventListener('change',()=>openChannelsModal(filterCheck.checked));
  filterLabel.append(filterCheck,el('span','Показать архивные каналы'));
  headWrap.append(createBtn,filterLabel);
  body.append(headWrap);

  if(!chList.length){
    body.append(el('p','Каналы пока не созданы. Нажмите «Создать новый канал», чтобы настроить профиль стиля.','muted'));
    return;
  }

  const grid=el('div',undefined,'channel-grid');
  for(const ch of chList){
    const card=el('div',undefined,'channel-card'+(ch.is_archived?' archived':''));
    card.dataset.testid=`channel-card-${ch.slug||ch.id}`;
    const cardHead=el('div',undefined,'channel-card-head');
    const info=el('div');
    info.append(el('h3',ch.name,'channel-card-title'),el('div',ch.slug||ch.id,'channel-card-slug'));
    cardHead.append(info);
    if(ch.is_archived)cardHead.append(el('span','В архиве','channel-card-tag danger'));
    
    const meta=el('div',undefined,'channel-card-meta');
    const typeNames={stickman:'Стикмен',forgotten_cities:'Забытые города',realism:'Реализм',anime:'Аниме',comic:'Комикс','3d_render':'3D',cinematic:'Кино',illustration:'Иллюстрация'};
    meta.append(
      el('span',`Тип: ${typeNames[ch.profile_type]||ch.profile_type}`,'channel-card-tag'),
      el('span',`Версия: v${ch.active_version?.version_number||1}`,'channel-card-tag'),
      el('span',`Персонажей: ${ch.characters_count||ch.characters?.length||0}`,'channel-card-tag'),
      el('span',`Референсов: ${ch.references_count||ch.reference_assets?.length||0}`,'channel-card-tag')
    );
    
    if(ch.description){
      const desc=el('p',ch.description,'muted');
      desc.style.cssText='font-size:12px;margin:4px 0;';
      card.append(cardHead,meta,desc);
    }else{
      card.append(cardHead,meta);
    }

    const actions=el('div',undefined,'inline-actions');
    actions.style.cssText='margin-top:auto;padding-top:8px;border-top:1px solid #232a3d;';
    actions.append(button('Настроить',()=>openChannelEditorModal(ch.id),'small-button',`edit-channel-${ch.id}`));
    actions.append(button('Дублировать',async()=>{
      await action(`/channels/${ch.id}/duplicate`,{name:`${ch.name} (Копия)`},'Канал дублирован');
      await refreshChannels();
      openChannelsModal(includeArchived);
    },'small-button',`dup-channel-${ch.id}`));
    
    if(!ch.is_archived){
      actions.append(button('В архив',async()=>{
        await action(`/channels/${ch.id}/archive`,{},'Канал отправлен в архив');
        await refreshChannels();
        openChannelsModal(includeArchived);
      },'small-button danger',`archive-channel-${ch.id}`));
    }else{
      actions.append(button('Восстановить',async()=>{
        await action(`/channels/${ch.id}/unarchive`,{},'Канал восстановлен');
        await refreshChannels();
        openChannelsModal(includeArchived);
      },'small-button',`unarchive-channel-${ch.id}`));
    }
    
    card.append(actions);
    grid.append(card);
  }
  body.append(grid);
}

async function openChannelEditorModal(channelId){
  if(!channelId){
    const body=modal('Создание нового канала');
    const nameF=field('Название канала','','input');
    const slugF=field('Slug (идентификатор)','','input');
    
    const typeLabel=el('label');
    typeLabel.append(el('span','Тип профиля'));
    const typeSelect=el('select');
    const typeOpts=[
      {id:'stickman',name:'Стикмен (2D Stickman)'},
      {id:'forgotten_cities',name:'Забытые города — архитектура, артефакты и историческая реконструкция'},
      {id:'realism',name:'Фотореализм'},
      {id:'anime',name:'Аниме / Манга'},
      {id:'comic',name:'Комикс'},
      {id:'3d_render',name:'3D Рендер'},
      {id:'cinematic',name:'Кинематографичный'},
      {id:'illustration',name:'Иллюстрация'}
    ];
    for(const t of typeOpts){
      const o=el('option',t.name);o.value=t.id;typeSelect.append(o);
    }
    typeLabel.append(typeSelect);

    const descF=field('Описание канала','','textarea');
    const stylePromptF=field('Стилевой промпт (Style Prompt)','','textarea');
    const lineStyleF=field('Стиль линий контура (Line Style)','','input');
    const bgStyleF=field('Стиль фона (Background Style)','','input');

    const stickmanCheckLabel=el('label');
    stickmanCheckLabel.style.cssText='display:inline-flex;align-items:center;gap:8px;font-size:13px;margin:8px 0;';
    const stickmanCheck=el('input');
    stickmanCheck.type='checkbox';
    stickmanCheck.checked=true;
    stickmanCheckLabel.append(stickmanCheck,el('span','Создать стандартного персонажа Стикмен (канонический образ)'));

    typeSelect.addEventListener('change',()=>{
      if(typeSelect.value==='stickman'){
        stickmanCheck.checked=true;
        if(!lineStyleF.input.value)lineStyleF.input.value='Черные четкие линии контура 3-4px, минималистичная геометрия';
        if(!bgStyleF.input.value)bgStyleF.input.value='Упрощенный векторный градиентный фон';
      }
    });

    body.append(nameF.box,slugF.box,typeLabel,descF.box,stylePromptF.box,lineStyleF.box,bgStyleF.box,stickmanCheckLabel,button('Создать канал',async()=>{
      if(!nameF.input.value.trim())throw Error('Укажите название канала.');
      const res=await api('/channels',{
        name:nameF.input.value.trim(),
        slug:slugF.input.value.trim()||undefined,
        profile_type:typeSelect.value,
        description:descF.input.value,
        style_prompt:stylePromptF.input.value,
        line_style:lineStyleF.input.value,
        background_style:bgStyleF.input.value,
        init_stickman:stickmanCheck.checked
      });
      showNotice(`Канал «${res.name}» создан!`);
      await refreshChannels();
      openChannelEditorModal(res.id);
    },'primary'));
    return;
  }

  const ch=await api(`/channels/${channelId}`);
  const body=modal(`Настройка канала: ${ch.name}`);
  
  const backBtn=button('← Назад к списку каналов',()=>openChannelsModal(),'small-button');
  backBtn.style.cssText='margin-bottom:14px;';
  body.append(backBtn);

  // Section 1: Metadata
  const metaSec=el('div',undefined,'workspace-section');
  metaSec.append(el('h3','1. Основные параметры канала','workspace-title'));
  const nameF=field('Название канала',ch.name,'input');
  const slugF=field('Slug (идентификатор)',ch.slug||'','input');
  const descF=field('Описание',ch.description||'','textarea');
  metaSec.append(nameF.box,slugF.box,descF.box,button('Сохранить параметры канала',async()=>{
    await action(`/channels/${ch.id}/update`,{
      name:nameF.input.value.trim(),
      slug:slugF.input.value.trim()||undefined,
      description:descF.input.value
    },'Параметры канала сохранены');
    await refreshChannels();
    openChannelEditorModal(ch.id);
  },'small-button'));
  body.append(metaSec);

  // Section 2: Style Version
  const activeVer=ch.active_version||{};
  const verSec=el('div',undefined,'workspace-section');
  verSec.append(
    el('h3',`2. Стиль и правила (Текущая версия: v${activeVer.version_number||1})`,'workspace-title'),
    el('p','Сохранение изменений стиля создаёт новую неизменяемую версию (v'+((activeVer.version_number||1)+1)+'). Ранее созданные проекты останутся на своей версии, пока не будут обновлены.','muted')
  );
  
  const stylePromptF=field('Стилевой промпт (Style Prompt)',activeVer.style_prompt||'','textarea');
  const negPromptF=field('Отрицательный промпт (Negative Prompt)',activeVer.negative_prompt||'','textarea');
  const lineStyleF=field('Стиль линий контура (Line Style)',activeVer.line_style||'','textarea');
  const bgStyleF=field('Стиль фона (Background Style)',activeVer.background_style||'','textarea');
  const compRulesF=field('Правила композиции и ракурсов',activeVer.composition_rules||'','textarea');
  const colorPalF=field('Цветовая палитра',activeVer.color_palette||'','input');
  const animRulesF=field('Правила анимации (Veo / Video)',activeVer.animation_rules||'','textarea');
  
  verSec.append(
    stylePromptF.box,negPromptF.box,lineStyleF.box,bgStyleF.box,compRulesF.box,colorPalF.box,animRulesF.box,
    button('💾 Сохранить новую версию стиля',async()=>{
      await action(`/channels/${ch.id}/versions`,{
        style_prompt:stylePromptF.input.value,
        negative_prompt:negPromptF.input.value,
        line_style:lineStyleF.input.value,
        background_style:bgStyleF.input.value,
        composition_rules:compRulesF.input.value,
        color_palette:colorPalF.input.value,
        animation_rules:animRulesF.input.value
      },'Новая версия стиля успешно сохранена!');
      await refreshChannels();
      openChannelEditorModal(ch.id);
    },'primary')
  );
  body.append(verSec);

  // Section 3: Characters
  const charSec=el('div',undefined,'workspace-section');
  const charHead=el('div');
  charHead.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;';
  charHead.append(el('h3','3. Канонические персонажи канала','workspace-title'));
  const addCharBtn=button('＋ Добавить персонажа',()=>openChannelCharacterModal(ch.id,null),'small-button');
  charHead.append(addCharBtn);
  charSec.append(charHead);

  if(!ch.characters||!ch.characters.length){
    charSec.append(el('p','Канонические персонажи пока не добавлены.','muted'));
  }else{
    const charListWrap=el('div');
    charListWrap.style.cssText='display:flex;flex-direction:column;gap:8px;';
    for(const c of ch.characters){
      const cRow=el('div');
      cRow.style.cssText='display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:#141724;border:1px solid #282f45;border-radius:8px;';
      const cInfo=el('div');
      cInfo.append(
        el('strong',c.canonical_name),
        el('span',` · ${c.role==='main'?'Главный':'Второстепенный'} · v${c.active_version?.version_number||1}`, 'muted')
      );
      if(c.description){
        const cd=el('div',c.description,'muted');
        cd.style.cssText='font-size:12px;margin-top:2px;';
        cInfo.append(cd);
      }
      const cActions=el('div',undefined,'inline-actions');
      cActions.append(button('Редактировать',()=>openChannelCharacterModal(ch.id,c),'small-button'));
      cRow.append(cInfo,cActions);
      charListWrap.append(cRow);
    }
    charSec.append(charListWrap);
  }
  body.append(charSec);

  // Section 4: Reference Assets
  const refSec=el('div',undefined,'workspace-section');
  refSec.append(
    el('h3','4. Референсные ассеты канала','workspace-title'),
    el('p','Изображения-образцы для фиксации стиля персонажей и фонов.','muted')
  );

  const uploadWrap=el('div');
  uploadWrap.style.cssText='display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;background:#141827;padding:12px;border-radius:8px;border:1px solid #2e3650;';
  
  const charSelect=el('select');
  const allCharOpt=el('option','Общий стиль канала');
  allCharOpt.value='';
  charSelect.append(allCharOpt);
  for(const c of (ch.characters||[])){
    const opt=el('option',`Персонаж: ${c.canonical_name}`);
    opt.value=c.id;
    charSelect.append(opt);
  }

  const assetTypeSelect=el('select');
  const assetTypes=[
    {id:'character_front',name:'Вид спереди (Front)'},
    {id:'character_side',name:'Вид сбоку (Side)'},
    {id:'expression_sheet',name:'Эмоции / Мимика'},
    {id:'style_reference',name:'Референс стиля'},
    {id:'prop_reference',name:'Референс предмета'},
    {id:'location_reference',name:'Референс фона'}
  ];
  for(const at of assetTypes){
    const opt=el('option',at.name);opt.value=at.id;assetTypeSelect.append(opt);
  }

  const primaryCheckLabel=el('label');
  primaryCheckLabel.style.cssText='display:inline-flex;align-items:center;gap:6px;font-size:12px;margin:0;';
  const primaryCheck=el('input');
  primaryCheck.type='checkbox';
  primaryCheckLabel.append(primaryCheck,el('span','Основной (Primary)'));

  const fileInput=el('input');
  fileInput.type='file';
  fileInput.accept='image/*';
  fileInput.style.display='none';

  const uploadBtn=button('📁 Загрузить референс',()=>fileInput.click(),'small-button');
  fileInput.addEventListener('change',async()=>{
    if(!fileInput.files||!fileInput.files[0])return;
    try{
      showNotice('⏳ Загрузка референса канала...');
      const file=fileInput.files[0];
      const base64=await readFileAsBase64(file);
      await action(`/channels/${ch.id}/references`,{
        character_id:charSelect.value||null,
        asset_type:assetTypeSelect.value,
        image:base64,
        filename:file.name,
        is_primary:primaryCheck.checked
      },'Референс канала успешно загружен!');
      await refreshChannels();
      openChannelEditorModal(ch.id);
    }catch(err){
      showNotice(err.message,true);
    }
  });

  uploadWrap.append(charSelect,assetTypeSelect,primaryCheckLabel,fileInput,uploadBtn);
  refSec.append(uploadWrap);

  const refs=ch.reference_assets||[];
  if(!refs.length){
    refSec.append(el('p','Референсные изображения пока не загружены.','muted'));
  }else{
    const refGrid=el('div',undefined,'channel-ref-grid');
    for(const r of refs){
      const item=el('div',undefined,'channel-ref-item');
      const img=el('img',undefined,'channel-ref-thumb');
      img.src=`/api/channel_references/${r.id}`;
      img.alt=r.asset_type;
      img.loading='lazy';
      item.append(img);
      if(r.is_primary){
        item.append(el('span','★ Основной','channel-ref-primary'));
      }
      const foot=el('div',undefined,'channel-ref-footer');
      const typeShort=r.asset_type.replace('character_','');
      foot.append(el('span',typeShort,'muted'));
      const delBtn=button('✕',async()=>{
        await action(`/channels/${ch.id}/references/${r.id}`,undefined,'Референс удален');
        await refreshChannels();
        openChannelEditorModal(ch.id);
      },'small-button danger');
      delBtn.style.cssText='padding:1px 5px;font-size:10px;';
      foot.append(delBtn);
      item.append(foot);
      refGrid.append(item);
    }
    refSec.append(refGrid);
  }
  body.append(refSec);
}

function openChannelCharacterModal(channelId,char){
  const body=modal(char?`Редактирование: ${char.canonical_name}`:'Новый персонаж канала');
  const nameF=field('Имя персонажа (Canonical Name)',char?.canonical_name||'','input');
  
  const roleLabel=el('label');
  roleLabel.append(el('span','Роль персонажа'));
  const roleSelect=el('select');
  const roles=[
    {id:'main',name:'Главный герой (Main)'},
    {id:'supporting',name:'Второстепенный (Supporting)'},
    {id:'episodic',name:'Эпизодический (Episodic)'},
    {id:'antagonist',name:'Антагонист (Antagonist)'},
    {id:'narrator',name:'Рассказчик (Narrator)'}
  ];
  for(const r of roles){
    const opt=el('option',r.name);opt.value=r.id;opt.selected=char?.role===r.id;roleSelect.append(opt);
  }
  roleLabel.append(roleSelect);

  const descF=field('Описание роли и характера',char?.description||'','textarea');
  const appPromptF=field('Промпт внешности (Appearance Prompt)',char?.active_version?.appearance_prompt||char?.appearance_prompt||'','textarea');
  const negPromptF=field('Отрицательный промпт (Negative Prompt)',char?.negative_prompt||'','textarea');
  const wardrobeF=field('Правила одежды (Wardrobe Rules)',char?.active_version?.wardrobe_rules||'','textarea');
  const propF=field('Правила пропорций (Proportion Rules)',char?.active_version?.proportion_rules||'','textarea');
  const faceF=field('Правила лица и мимики (Face Rules)',char?.active_version?.face_rules||'','textarea');
  const colorF=field('Правила цветов (Color Rules)',char?.active_version?.color_rules||'','input');

  const backBtn=button('← Назад к каналу',()=>openChannelEditorModal(channelId),'small-button');
  backBtn.style.cssText='margin-bottom:12px;';

  body.append(
    backBtn,nameF.box,roleLabel,descF.box,appPromptF.box,negPromptF.box,wardrobeF.box,propF.box,faceF.box,colorF.box,
    button('Сохранить персонажа',async()=>{
      if(!nameF.input.value.trim())throw Error('Укажите имя персонажа.');
      await action(`/channels/${channelId}/characters`,{
        id:char?.id||undefined,
        canonical_name:nameF.input.value.trim(),
        role:roleSelect.value,
        description:descF.input.value,
        appearance_prompt:appPromptF.input.value,
        negative_prompt:negPromptF.input.value,
        wardrobe_rules:wardrobeF.input.value,
        proportion_rules:propF.input.value,
        face_rules:faceF.input.value,
        color_rules:colorF.input.value
      },'Персонаж канала сохранен');
      await refreshChannels();
      openChannelEditorModal(channelId);
    },'primary')
  );
}

async function openChannelUpdateModal(projectId){
  const diff=await api(`/projects/${projectId}/channel-update-preview`);
  const body=modal(`Обновление профиля канала: ${diff.channel_name}`);
  
  const info=el('p',`В канале «${diff.channel_name}» доступна новая версия стиля (v${diff.style_diff.new_version_number}). Вы можете обновить снимок профиля для этого проекта.`,'muted');
  
  const table=el('table',undefined,'diff-table');
  table.innerHTML=`
    <thead>
      <tr>
        <th>Параметр</th>
        <th>Текущая версия проекта (v${diff.style_diff.old_version_number||'—'})</th>
        <th>Новая версия канала (v${diff.style_diff.new_version_number})</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td><strong>Стилевой промпт</strong></td>
        <td class="diff-old">${diff.style_diff.old_style_prompt||'—'}</td>
        <td class="diff-new">${diff.style_diff.new_style_prompt||'—'}</td>
      </tr>
      <tr>
        <td><strong>Стиль линий</strong></td>
        <td class="diff-old">${diff.style_diff.old_line_style||'—'}</td>
        <td class="diff-new">${diff.style_diff.new_line_style||'—'}</td>
      </tr>
      <tr>
        <td><strong>Стиль фона</strong></td>
        <td class="diff-old">${diff.style_diff.old_background_style||'—'}</td>
        <td class="diff-new">${diff.style_diff.new_background_style||'—'}</td>
      </tr>
    </tbody>
  `;

  const affectedInfo=el('div');
  affectedInfo.style.cssText='margin:14px 0;padding:10px;background:#161c2b;border-radius:8px;border:1px solid #303d5c;font-size:13px;';
  affectedInfo.append(el('strong',`Затронуто кадров в проекте: ${diff.affected_scenes_count}`));
  if(diff.affected_scenes_count>0){
    const sList=el('div',`Кадры: ${diff.affected_scenes.map(s=>`#${s.position}`).join(', ')}`,'muted');
    sList.style.cssText='margin-top:4px;font-size:12px;';
    affectedInfo.append(sList);
  }

  body.append(info,table,affectedInfo);

  if(diff.can_apply){
    body.append(button(`⚡ Применить новую версию (v${diff.style_diff.new_version_number})`,async()=>{
      await action(`/projects/${projectId}/channel-update-apply`,{target_version_id:diff.target_version_id},'Профиль канала успешно обновлен!');
      dialog.close();
      await refresh(true);
    },'primary','apply-channel-update-btn'));
  }else{
    body.append(el('p','Проект уже использует актуальную версию профиля канала.','success'));
  }
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

projectSelect.addEventListener('change',async()=>{notice.textContent='';document.querySelector('#animation-completion')?.remove();document.querySelector('#animation-interrupted')?.remove();document.title='FrameForge';window.scrollTo({top:0});if(!projectSelect.value){resetDraft();return;}const pid=projectSelect.value;current=pid;project=null;view='studio';try{await activateProject(pid);if(current===pid)await refresh(true);}catch(error){showNotice(error.message,true);}});
document.querySelector('#new-button').addEventListener('click',resetDraft);
document.querySelector('#import-prompts-button').addEventListener('click',()=>openPromptImport({dialog,api,notice:showNotice,onCreated:async created=>{current=created.id;project=created;view='studio';history.replaceState(null,'',`?project=${created.id}`);await listProjects();render();}}));
function manageProjects(initial='projects',confirmId=null){return openProjectManager({api,currentId:current,initial,confirmId,notice:showNotice,
 prepareDelete:async pid=>{if(montageView&&montageProjectId===pid)await montageView.prepareMaintenance();},
 onChange:async change=>{if(change.deleted_id===current){resetDraft();history.replaceState(null,'','/');}await listProjects();},
 onOpen:async pid=>{current=pid;project=null;view='studio';await activateProject(pid);await listProjects();history.replaceState(null,'',`?project=${pid}`);await refresh(true);}
});}
document.querySelector('#projects-button')?.addEventListener('click',()=>manageProjects());
const channelsBtn=document.querySelector('#channels-button');
if(channelsBtn)channelsBtn.addEventListener('click',()=>openChannelsModal().catch(error=>showNotice(error.message,true)));
document.querySelector('#settings-button').addEventListener('click',()=>settings().catch(error=>showNotice(error.message,true)));
document.querySelectorAll('.step').forEach(item=>item.addEventListener('click',()=>{view=item.dataset.view;render();}));
try{
  token=(await api('/session')).token;
  currentSettings=await api('/settings');
  await refreshChannels();
  await listProjects();
  const opening=new URLSearchParams(location.search),openingProject=opening.get('project');
  if(openingProject&&[...projectSelect.options].some(option=>option.value===openingProject)){
    current=openingProject;projectSelect.value=current;view=opening.get('view')==='export'?'export':'studio';await activateProject(current);await refresh(true);
  }
  render();
}catch(error){showNotice(error.message,true);}
setInterval(()=>{if(currentSettings?.image_backend==='flow')void refreshFlowBridgeStatus();},3500);
setInterval(()=>{const watching=current&&animationRunFor(current)&&!animationRunFor(current).finished;if(current&&!dialog.open&&(watching||!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)))refresh().catch(error=>showNotice('Не удалось обновить: '+error.message,true));},2500);

