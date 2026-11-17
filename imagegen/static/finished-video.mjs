let job=null, busy=false, refresh=()=>{};

export function openFinishedVideo(body,api){
  const make=(tag,text)=>{
    const node=document.createElement(tag);
    if(text)node.textContent=text;
    return node;
  };
  const description=make('p','Загрузите готовый MP4 из Insaga. FrameForge сохранит новую копию без исходных метаданных.');
  const file=make('input');
  file.type='file';
  file.accept='.mp4,video/mp4';
  file.setAttribute('aria-label','Выбрать MP4');
  const effects=make('fieldset');
  effects.append(make('legend','Эффекты'));
  const options=[['color','Лёгкая автоцветокоррекция'],['grain','Зерно старой плёнки'],['leaks','Редкие тёплые засветки']].map(([key,name])=>{
    const row=make('div'), label=make('label'), input=make('input');
    input.type='checkbox';
    input.checked=true;
    label.append(input,' '+name);
    row.append(label);
    effects.append(row);
    return [key,input];
  });
  const hint=make('p','Снимите все галочки, чтобы очистить метаданные без перекодирования.');
  hint.className='muted';
  const start=make('button','Обработать');
  start.type='button';
  start.className='primary';
  const status=make('p');
  status.setAttribute('role','status');
  const progress=make('progress');
  progress.max=100;
  progress.setAttribute('aria-label','Прогресс обработки');
  const download=make('a','Скачать обработанный MP4');
  download.download='frameforge-processed.mp4';
  body.append(description,file,effects,hint,start,status,progress,download);

  refresh=()=>{
    file.disabled=effects.disabled=busy;
    start.disabled=busy||!file.files.length;
    progress.hidden=!busy;
    if(job?.state==='running')progress.value=job.progress;
    else progress.removeAttribute('value');
    download.hidden=!job?.url;
    if(job?.url)download.href=job.url;
    status.textContent=job?.error||({uploading:'Загружаю MP4…',running:job?.backend?`Обрабатываю видео · ${job.backend}`:'Обрабатываю видео…',ready:'Готово. Файл можно скачать.'}[job?.state]||'');
  };
  file.addEventListener('change',refresh);
  start.addEventListener('click',async()=>{
    const source=file.files[0];
    if(!source||busy)return;
    if(!/\.mp4$/i.test(source.name)){job={error:'Выберите файл MP4.'};refresh();return;}
    const selected=options.filter(([,input])=>input.checked).map(([key])=>key);
    busy=true;
    job={state:'uploading'};
    refresh();
    try{
      job=await api('/finished-video?effects='+selected.join(','),source);
      while(job.state==='running'){
        refresh();
        await new Promise(resolve=>setTimeout(resolve,1250));
        job=await api('/finished-video/'+job.id);
      }
    }catch(error){job={error:error.message};}
    finally{busy=false;refresh();}
  });
  refresh();
}
