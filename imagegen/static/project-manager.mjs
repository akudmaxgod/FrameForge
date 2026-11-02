const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const size=bytes=>{const units=['Б','КБ','МБ','ГБ'];let i=0;while(bytes>=1024&&i<3){bytes/=1024;i++;}return bytes.toLocaleString('ru-RU',{maximumFractionDigits:1})+' '+units[i];};

export function openProjectManager({api,currentId,prepareDelete,onChange,onOpen,notice,initial='projects',confirmId=null}){
 document.querySelector('.project-manager-dialog')?.close();
 const dialog=node('dialog',undefined,'project-manager-dialog');
 const header=node('header');
 const title=node('div');
 const close=node('button','Закрыть');
 const tabs=node('div',undefined,'project-manager-tabs');
 const tabGroup=node('div',undefined,'project-manager-tab-group');
 tabGroup.style.cssText='display:flex;gap:7px;align-items:center;';
 const deleteAllBtn=node('button','Удалить всё','danger-quiet');
 deleteAllBtn.dataset.testid='delete-all-projects-btn';
 deleteAllBtn.type='button';
 deleteAllBtn.title='Принудительно удалить все проекты и очистить место на диске';
 deleteAllBtn.style.cssText='margin-left:auto;';

 const body=node('div',undefined,'project-manager-body');
 title.append(node('h2','Проекты и кэш'),node('p','Управляйте проектами и освобождайте место на диске.'));
 header.append(title,close);
 close.type='button';
 close.onclick=()=>dialog.close();

 let tab=initial==='trash'?'projects':initial,projects=null,cache=null,cacheConfirm=false,busy=false,disposed=false,message='',error='';
 const selectedCache=new Set(),tabButtons={};

 function button(text,action,cls=''){
  const b=node('button',text,cls);
  b.type='button';
  b.disabled=busy;
  b.onclick=()=>Promise.resolve().then(action).catch(e=>{error=e.message;render();});
  return b;
 }

 for(const [id,label] of [['projects','Проекты'],['cache','Кэш']]){
  const b=button(label,async()=>{tab=id;cacheConfirm=false;error='';await load();body.scrollTop=0;});
  b.setAttribute('role','tab');
  b.setAttribute('aria-controls','project-manager-panel');
  tabButtons[id]=b;
  tabGroup.append(b);
 }

 deleteAllBtn.onclick=()=>mutate(async()=>{
  if(currentId)await prepareDelete(currentId);
  const res=await api('/maintenance/projects/delete-all',{confirm:true});
  await onChange({all_deleted:true,deleted_id:currentId});
  projects=await api('/maintenance/projects');
  const freedInfo=res?.freed_bytes?` (освобождено ${size(res.freed_bytes)})`:'';
  message=`Все проекты (${res?.deleted_count||0}) удалены с диска${freedInfo}.`;
 });

 tabs.setAttribute('role','tablist');
 tabs.setAttribute('aria-label','Управление проектами');
 tabs.append(tabGroup,deleteAllBtn);
 body.id='project-manager-panel';
 body.setAttribute('role','tabpanel');

 async function load(){
  busy=true;render();
  try{
   if(tab==='cache'){
    cache=await api('/maintenance/cache');
    if(!selectedCache.size)for(const group of cache.groups)selectedCache.add(group.id);
   } else {
    projects=await api('/maintenance/projects');
    if(confirmId){
     const pid=confirmId;
     confirmId=null;
     await prepareDelete(pid);
     const res=await api(`/maintenance/projects/${pid}/delete`,{confirm:true});
     await onChange({deleted_id:pid});
     projects=await api('/maintenance/projects');
     const freedInfo=res?.freed_bytes?` (освобождено ${size(res.freed_bytes)})`:'';
     message='Проект удалён с диска'+freedInfo+'.';
    }
   }
  }catch(e){error=e.message;}finally{busy=false;if(!disposed)render();}
 }

 async function mutate(action){
  busy=true;error='';render();
  try{await action();}catch(e){error=e.message;}finally{busy=false;if(!disposed)render();}
 }

 function render(){
  if(disposed)return;
  for(const [id,b] of Object.entries(tabButtons)){b.setAttribute('aria-selected',String(id===tab));b.disabled=busy;}
  deleteAllBtn.disabled=busy||!projects?.projects?.length;
  deleteAllBtn.style.display=tab==='projects'?'inline-block':'none';

  body.replaceChildren();
  if(message){const p=node('p',message,'manager-success');p.setAttribute('role','status');body.append(p);}
  if(error){const p=node('p',error,'manager-error');p.setAttribute('role','alert');body.append(p);}
  if(busy)body.append(node('p','Обновляю…','manager-muted'));
  if(tab==='cache')renderCache();else renderProjects();
 }

 function renderProjects(){
  if(!projects)return;
  const rows=projects.projects;
  if(!rows.length){body.append(node('p','Проектов пока нет. Место на диске свободно.','manager-empty'));return;}
  const list=node('div',undefined,'manager-project-list');
  for(const p of rows){
   const row=node('article',undefined,'manager-project-row'),copy=node('div'),actions=node('div',undefined,'manager-row-actions');
   copy.append(node('strong',p.title),node('small',`${p.scene_count} сцен · ${p.media_count} медиа${p.id===currentId?' · открыт сейчас':''}`));
   actions.append(
     button('Открыть',async()=>{await onOpen(p.id);dialog.close();}),
     button('Удалить',()=>mutate(async()=>{
       await prepareDelete(p.id);
       const res=await api(`/maintenance/projects/${p.id}/delete`,{confirm:true});
       await onChange({deleted_id:p.id});
       projects=await api('/maintenance/projects');
       const freedInfo=res?.freed_bytes?` (освобождено ${size(res.freed_bytes)})`:'';
       message=`Проект «${p.title}» удалён с диска${freedInfo}.`;
     }),'danger-quiet')
   );
   row.append(copy,actions);list.append(row);
  }
  body.append(list);
 }

 function renderCache(){
  if(!cache)return;
  body.append(node('h3','Пересоздаваемые временные файлы'),node('p','Изображения, видео, озвучка, монтаж, сохранённые привязки и установленные модели не удаляются. После очистки соответствующие расчёты выполнятся заново.','manager-muted'));
  if(cache.busy)body.append(node('p',cache.busy,'manager-warning'));
  const groups=node('div',undefined,'manager-cache-groups');
  for(const group of cache.groups){
   const label=node('label',undefined,'manager-cache-group'),check=node('input'),copy=node('span'),amount=node('strong',size(group.bytes));
   check.type='checkbox';check.checked=selectedCache.has(group.id);check.disabled=busy;check.setAttribute('aria-label',group.label);
   check.onchange=()=>{check.checked?selectedCache.add(group.id):selectedCache.delete(group.id);cacheConfirm=false;render();};
   copy.append(node('span',group.label),node('small',group.files+' файлов'+(group.skipped?' · защищённых/пропущенных: '+group.skipped:'')));
   label.append(check,copy,amount);groups.append(label);
  }
  body.append(groups);
  const bytes=cache.groups.filter(g=>selectedCache.has(g.id)).reduce((sum,g)=>sum+g.bytes,0),actions=node('div',undefined,'manager-cache-actions');
  actions.append(node('strong','Выбрано: '+size(bytes)),button('Обновить оценку',load));
  const clear=button('Очистить выбранный кэш',()=>{cacheConfirm=true;render();});
  clear.disabled=busy||Boolean(cache.busy)||!bytes||!selectedCache.size;actions.append(clear);body.append(actions);
  if(cacheConfirm){
   const box=node('section',undefined,'manager-confirm');
   box.append(node('h3','Очистить '+size(bytes)+' кэша?'),node('p','Будут удалены только выбранные временные расчёты. Готовые результаты проектов сохранятся.'),button('Отмена',()=>{cacheConfirm=false;render();}),button('Да, очистить кэш',()=>mutate(async()=>{
    const result=await api('/maintenance/cache/clear',{confirm:true,expected_token:cache.token,groups:[...selectedCache]});
    cache=result.remaining;cacheConfirm=false;
    message=`Освобождено ${size(result.removed_bytes)} · удалено файлов: ${result.removed_files}${result.failed_files?' · не удалось удалить: '+result.failed_files:''}`;
    await onChange({cache_cleared:true});
   }),'danger'));
   body.append(box);
  }
 }

 dialog.append(header,tabs,body);
 document.body.append(dialog);
 dialog.addEventListener('close',()=>{disposed=true;dialog.remove();});
 dialog.showModal();
 load();
 return {dispose(){disposed=true;dialog.remove();}};
}
