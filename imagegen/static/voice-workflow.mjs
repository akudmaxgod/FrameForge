import {voicePlanVersions} from './voice-plan-versions.mjs';
import {analysisError} from './analysis-error.mjs';
import {sourceReviewPanel} from './source-review-panel.mjs';
export const voiceDraft={file:null,autoRender:false,contentFocus:true};
const busyStates=new Set(['queued','recognizing','analyzing','aligning','images','animating','assembling','exporting','waiting_reference']);
export function voiceStatus(job){
  if(job.state==='failed'&&job.result?.plan_review_wait)return {
    text:'Заключительная проверка плана остановлена. Разбор сохранён.',
    help:'Причина и доступные действия показаны в блоке проверки плана.',
    details:job.error||job.stage,retry:null
  };
  const error=job.state==='failed'&&analysisError(job.error||job.stage);
  if(error)return {...error,retry:'Повторить проверку'};
  if(job.state==='failed'&&job.result?.alignment_unsupported_passages?.length){
    const passage=job.result.alignment_unsupported_passages[0].quote;
    return {
      text:'В озвучке не удалось подтвердить фразу: «'+passage+'».',
      help:'Проверьте, звучит ли эта фраза в аудио. Если звучит — повторите проверку. Если озвучивание пропустило текст внутри ссылки, добавьте озвучку с полным текстом. Адреса источников не считаются словами диктора.',
      details:job.error||job.stage,retry:'Повторить проверку'
    };
  }
  if(job.state==='failed'&&job.result?.alignment_metrics)return {
    text:'Подготовка остановлена: время части текста не подтверждено по озвучке.',
    help:'Если текст и озвучка одинаковые, нажмите «Повторить проверку». Менять сценарий не нужно. Если ошибка повторится, откройте подробности.',
    details:job.error||job.stage,retry:'Повторить проверку'
  };
  return {text:job.stage,retry:job.state==='failed'?'Повторить попытку':'Продолжить'};
}
function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
function time(seconds){const value=Math.round(seconds);return Math.floor(value/60)+':'+String(value%60).padStart(2,'0');}
async function base64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('Не удалось прочитать озвучку.'));reader.readAsDataURL(file);});}
export async function startVoiceProject(api,pid){
  if(!voiceDraft.file)return false;
  const audio=await api(`/projects/${pid}/edit/audio`,{filename:voiceDraft.file.name,data:await base64(voiceDraft.file)});
  await api(`/projects/${pid}/voice`,{audio_asset_id:audio.asset_id,auto_render:voiceDraft.contentFocus?false:voiceDraft.autoRender,frame_content_focus:voiceDraft.contentFocus,frames_only:true,preserve_existing:true,max_shot_seconds:7});
  voiceDraft.file=null;return true;
}
export function voiceControls({project,api,button,notice,refresh,rerender,beforeProduce,audioSelected}){
  const box=node('section');box.className='control-section voice-controls';box.dataset.testid='voice-controls';
  if(project)box.append(sourceReviewPanel({project,api,button,notice,refresh}));
  box.append(node('strong','Подготовить кадры по сценарию и озвучке'));
  if(!project){
    const input=node('input');input.type='file';input.accept='.mp3,.wav,.m4a,.aac,.ogg,.flac';input.dataset.testid='voice-upload';input.setAttribute('aria-label','Загрузить готовую озвучку');
    const label=node('p',voiceDraft.file?voiceDraft.file.name:'Добавьте озвучку, чтобы кадры получили время по речи.');label.className='muted';
    input.onchange=()=>{const file=input.files?.[0];if(!file)return;if(file.size>100000000){notice('Озвучка должна быть не больше 100 МБ.',true);return;}voiceDraft.file=file;audioSelected?.();rerender();};
    const select=node('select');select.setAttribute('aria-label','Режим работы с озвучкой');
    for(const [value,text] of [['plan','Подготовить план'],['auto','Подготовить план и изображения']]){const option=node('option',text);option.value=value;select.append(option);}
    select.value=voiceDraft.autoRender?'auto':'plan';select.onchange=()=>{voiceDraft.autoRender=select.value==='auto';rerender();};
    const focusLabel=node('label',' Согласовать содержание каждого кадра с озвучкой'),focus=node('input');focus.type='checkbox';focus.checked=voiceDraft.contentFocus;focus.dataset.testid='voice-frame-content-focus';focus.onchange=()=>{voiceDraft.contentFocus=focus.checked;};focusLabel.prepend(focus);
    box.append(focusLabel,node('small','Готовит отдельный план для проверки. Изображения запускаются после применения плана.'));
    box.append(label,input,select,node('p','Озвучка обрабатывается на ПК. Изображения создаёт выбранный генератор; для Flow нужна открытая вкладка с подключённым мостом.'));
    if(voiceDraft.file)box.append(button('Убрать озвучку',()=>{voiceDraft.file=null;rerender();},'secondary'));
    return box;
  }
  const job=project.voice_workflow;
  if(/\]\(\s*<?https?:\/\//i.test(project.story||'')){
    box.append(button('Скачать текст для озвучивания',async()=>{
      const source=await api(`/projects/${project.id}/voice/narration-text`);
      const url=URL.createObjectURL(new Blob([source.text],{type:'text/plain;charset=utf-8'}));
      const link=node('a');link.href=url;link.download='Текст для озвучивания.txt';
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    },'secondary','voice-narration-text'));
  }
  if(!job){
    if(project.scenes?.length){
      const input=node('input');input.type='file';input.accept='.mp3,.wav,.m4a,.aac,.ogg,.flac';input.setAttribute('aria-label','Озвучка для дополнения готовых сцен');
      box.append(node('p','Добавьте озвучку: программа дополнит длинные фрагменты новыми кадрами и сохранит готовые изображения и клипы.'),input,
        button('Дополнить сцены по озвучке',async()=>{
          const file=input.files?.[0];if(!file)throw Error('Выберите файл озвучки.');if(file.size>100000000)throw Error('Озвучка должна быть не больше 100 МБ.');
          const audio=await api(`/projects/${project.id}/edit/audio`,{filename:file.name,data:await base64(file)});
          await api(`/projects/${project.id}/voice`,{audio_asset_id:audio.asset_id,preserve_existing:true,frames_only:true,auto_render:false,max_shot_seconds:7});await refresh(true);
        },'primary','voice-expand-existing'));
      return box;
    }
    const imported=project.generations?.find(g=>g.purpose==='edit_audio'&&g.asset_id);
    if(imported&&!project.generations.some(g=>!['analysis','edit_audio'].includes(g.purpose))){
      box.append(node('p','В проекте уже сохранена озвучка. Можно подготовить кадры по её времени.'),button('Подготовить план по озвучке',async()=>{await api(`/projects/${project.id}/voice`,{audio_asset_id:imported.asset_id,auto_render:false,frames_only:true,frame_content_focus:true});await refresh(true);},'primary','voice-start-existing'));
    }else box.append(node('p','Для нового видео добавьте готовую озвучку при создании проекта.'));
    return box;
  }
  const versions=voicePlanVersions({project,api,button,refresh});if(versions)box.append(versions);
  const status=voiceStatus(job);
  const state=node('p',status.text);state.setAttribute('role','status');state.setAttribute('aria-live','polite');box.append(state);
  if(status.help)box.append(node('p',status.help));
  if(status.details){const details=node('details');details.append(node('summary','Подробности проверки'),node('p',status.details));box.append(details);}
  if(busyStates.has(job.state)){
    const progress=node('progress');progress.max=100;progress.value=job.progress;box.append(progress,button('Пауза',async()=>{await api(`/projects/${project.id}/voice/pause`,{});await refresh(true);},'secondary','voice-pause'));
  }
  if(job.result?.planned){
    box.append(node('p',`${job.result.frame_count} кадров · ${time(job.result.duration)} · Full HD`));
    const metrics=job.result.metrics;box.append(node('small',`Время по речи: ${(100*metrics.acoustic_coverage).toFixed(1)}%. Приблизительные границы отмечены в плане.`));
    if(job.state==='ready'&&!job.options?.preserve_existing)box.append(button(job.options?.frames_only?'Подготовить изображения':'Собрать ролик автоматически',async()=>{if(!job.options?.frames_only)await beforeProduce?.();await api(`/projects/${project.id}/voice/produce`,{});await refresh(true);},'primary','voice-produce'));
    if(job.state==='frames_ready')box.append(node('p','Изображения подготовлены. Выберите анимации отдельно, затем соберите видео в монтаже.'));
  }
  if(status.retry&&['failed','paused','reference_review_ready','reference_review_blocked'].includes(job.state))box.append(button(status.retry,async()=>{await api(`/projects/${project.id}/voice/resume`,{});await refresh(true);},'primary','voice-resume'));
  if(job.state==='completed'&&job.result.video_asset_id){const link=node('a','Скачать готовое видео');link.href='/api/assets/'+job.result.video_asset_id;link.download='frameforge-fullhd.mp4';link.className='btn primary';box.append(link);}
  return box;
}
export function voicePlan(project,{button}){
  if(!project?.voice_workflow?.result?.planned)return null;
  const box=node('section');box.className='voice-plan';box.dataset.testid='voice-plan';
  const heading=node('summary',`План по озвучке · ${project.scenes.length} кадров`),details=node('details');
  details.append(heading);
  const audit=project.analysis_metadata?.voice_editorial;
  if(audit)details.append(node('p',`Проверено по сценарию: ${audit.checked_frames} кадров · ${audit.required_location_names.length} эталона локаций · ${audit.artifact_names.length} эталонов предметов.`));
  const audio=node('audio');audio.controls=true;audio.preload='metadata';audio.src='/api/assets/'+project.voice_workflow.audio_asset_id;details.append(audio);
  let playingRow=null;
  for(const scene of project.scenes){
    const scheduled=project.analysis_metadata?.narration_timeline?.frames?.find(f=>f.scene_id===scene.id);
    const timing=scheduled?.timing||scene.data.voice_timing;if(!timing)continue;
    const row=node('article');row.className='voice-plan-row';row.dataset.start=timing.start;row.dataset.end=timing.end;
    row.append(node('strong',`Кадр ${scene.position} · ${time(timing.start)} — ${time(timing.end)}`),node('p',scheduled?.narration_source?.quote||scene.data.source_quote));
    row.append(node('p',scene.data.composition||scene.data.beat));
    if(timing.timing_needs_review)row.append(node('small','Есть приблизительное время — проверьте границы на слух.'));
    row.append(button('Послушать',async()=>{audio.currentTime=timing.start;await audio.play();},'secondary'));
    details.append(row);
  }
  audio.ontimeupdate=()=>{const row=[...details.querySelectorAll('article')].find(r=>+r.dataset.start<=audio.currentTime&&audio.currentTime<+r.dataset.end);if(playingRow!==row){playingRow?.classList.remove('active');row?.classList.add('active');playingRow=row;}};
  box.append(details);return box;
}
