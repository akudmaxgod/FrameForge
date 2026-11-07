function element(tag,text){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;return e;}

export function coverageControls(projectId,{onChange=()=>{}}={}){
 const key='frameforge-insert-coverage:'+projectId;
 let saved={mode:'limited',percent:30,distribution:'spread',photo_motion:'auto'};
 try{const value=JSON.parse(localStorage.getItem(key));if(value&&['limited','matched'].includes(value.mode)&&Number.isFinite(value.percent)&&value.percent>=0&&value.percent<=100&&['spread','best'].includes(value.distribution))saved=value;}catch{}
 const root=element('section');root.className='media-coverage-controls';root.dataset.testid='insert-coverage';
 root.append(element('strong','Сколько таймлайна занять вставками'));
 const row=element('div');row.className='media-coverage-row';
 const mode=element('select');mode.setAttribute('aria-label','Режим покрытия вставками');
 for(const [value,text] of [['limited','До выбранного процента'],['matched','Все подходящие совпадения']]){const o=element('option',text);o.value=value;mode.append(o);}mode.value=saved.mode;
 const amount=element('span');amount.className='media-coverage-amount';
 const presets=[];
 const percent=element('input');percent.type='number';percent.min='0';percent.max='100';percent.step='1';percent.value=saved.percent;percent.setAttribute('aria-label','Максимальная доля вставок, %');
 const distribution=element('select');distribution.setAttribute('aria-label','Распределение вставок');
 for(const [value,text] of [['spread','Распределить по ролику'],['best','Сначала лучшие совпадения']]){const o=element('option',text);o.value=value;distribution.append(o);}distribution.value=saved.distribution;
 const photoMotion=element('select');photoMotion.setAttribute('aria-label','Движение фото-вставок');
 for(const [value,text] of [['auto','Фото: чередовать движения'],['off','Фото: без движения'],['gentle_push_in','Фото: плавный наезд'],['gentle_pull_back','Фото: отдаление'],['gentle_pan_right','Фото: движение вправо'],['gentle_pan_left','Фото: движение влево']]){const o=element('option',text);o.value=value;photoMotion.append(o);}photoMotion.value=saved.photo_motion||'auto';
 function getValue(){const n=Number(percent.value);if(percent.value.trim()===''||!Number.isFinite(n)||n<0||n>100)throw Error('Укажите долю таймлайна от 0 до 100%.');return {mode:mode.value,percent:n,distribution:distribution.value,photo_motion:photoMotion.value};}
 function refresh(){amount.hidden=mode.value!=='limited';for(const b of presets)b.setAttribute('aria-pressed',String(Number(b.dataset.percent)===Number(percent.value)));}
 function changed(){refresh();try{localStorage.setItem(key,JSON.stringify(getValue()));}catch{}onChange();}
 for(const n of [20,30,40,50]){const b=element('button',n+'%');b.type='button';b.dataset.percent=n;b.onclick=()=>{percent.value=n;changed();};presets.push(b);amount.append(b);}
 amount.append(percent,element('span','%'));row.append(mode,amount,distribution,photoMotion);
 const help=element('p','Процент считается от длительности всего ролика, включая уже добавленные вставки. Только подходящие места и целые фразы; готовая анимация и движение камеры защищены.');help.className='media-coverage-help';
 root.append(row,help);refresh();mode.onchange=changed;percent.oninput=changed;distribution.onchange=changed;photoMotion.onchange=changed;
 return {element:root,getValue,setDisabled(value){for(const e of root.querySelectorAll('input,select,button'))e.disabled=value;},
  setValue(value){if(!value)return;mode.value=value.mode;percent.value=value.percent;distribution.value=value.distribution;photoMotion.value=value.photo_motion||'auto';refresh();}};
}

export function coverageText(plan){
 if(!plan?.coverage)return '';
 const c=plan.coverage,seconds=n=>(n/1000).toFixed(1),limit=c.target_percent===null?'без процентного лимита':`лимит ${c.target_percent}%`;
 let text=`Выбрано вставок: ${c.selected_count}. Покрытие: ${c.actual_percent}% (${seconds(c.planned_covered_ms)} из ${seconds(c.duration_ms)} сек.), ${limit}.`;
 if(c.existing_covered_ms)text+=` Уже на таймлайне: ${c.existing_percent}%.`;
 if(c.animated_clips_protected)text+=` Защищено анимированных кадров: ${c.animated_clips_protected}.`;
 if(c.already_above_target)text+=' Имеющиеся вставки уже превышают лимит; новые не добавляются.';
 else if(c.below_target)text+=' Остаток остаётся исходными кадрами: подходящих целых сегментов для заполнения нет.';
 return text;
}
