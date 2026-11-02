function node(tag,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}
export function compositionControls({scene,api,button,refresh}){
 const box=node('details');box.dataset.testid=`frame-composition-${scene.position}`;box.append(node('summary','Композиция: план и ракурс'));let loaded=false;
 box.addEventListener('toggle',async()=>{
  if(!box.open||loaded)return;loaded=true;const holder=node('div');box.append(holder);holder.append(node('p','Читаю содержание кадра…'));
  try{
   const current=await api(`/scenes/${scene.id}/composition`);if(!box.isConnected)return;holder.replaceChildren();
   const facts=current.facts.facts;holder.append(node('p','Композиция меняет только камеру. Предмет, действие, участники и факты сохраняются.'),node('blockquote',current.facts.source.quote),node('p',`Предмет: ${facts.subject}. Действие: ${facts.action}.`),node('p',`Участники: ${facts.participants.map(p=>p.name).join(', ')||'не выбраны'}. Время: ${facts.period}.`));
   const controls={},labels={shot_size:'План',camera_angle:'Ракурс',placement:'Расположение'};
   for(const [key,options] of Object.entries(current.choices)){
    const label=node('label',labels[key]),select=node('select');select.setAttribute('aria-label',labels[key]);
    for(const [value,text] of Object.entries(options)){const option=node('option',text);option.value=value;select.append(option);}
    select.value=current.composition[key];controls[key]=select;label.append(select);holder.append(label);
   }
   holder.append(button('Сохранить композицию',async()=>{
    const values=Object.fromEntries(Object.entries(controls).map(([k,v])=>[k,v.value]));
    await api(`/scenes/${scene.id}/composition`,{revision:current.revision,facts_hash:current.facts.content_hash,...values});
    await refresh(true);
   },'secondary'),node('small','Сохранение не запускает генерацию. Готовые медиа сохраняются; новый ракурс применяется при следующей генерации этого кадра.'));
  }catch(e){holder.replaceChildren(node('p',e.message));loaded=false;}
 });return box;
}
