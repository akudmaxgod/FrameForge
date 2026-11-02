export const IMPORT_VERSION='user-prompt-txt/1';
export const MAX_PROMPT_LENGTH=1950;
export const MAX_PROMPTS=2000;

export function parsePrompts(text,separator='paragraphs'){
  if(typeof text!=='string'||!text.trim()||text.length>4000000)throw Error('Выберите непустой TXT с промптами до 4 миллионов символов.');
  if(/[\u0000\ufffd]/u.test(text))throw Error('Не удалось прочитать TXT. Сохраните файл в UTF-8.');
  if(/^\s*\[\s*(?:\{|\])/.test(text))return parsePromptBundle(text,separator).map(item=>item.prompt);
  if(!['paragraphs','lines'].includes(separator))throw Error('Выберите разделитель промптов.');
  const normalized=text.replace(/^\ufeff/,'').replace(/\r\n?/g,'\n');
  const parts=(separator==='lines'?normalized.split('\n'):normalized.split(/\n[\t ]*\n(?:[\t ]*\n)*/)).map(s=>s.trim()).filter(Boolean);
  if(!parts.length||parts.length>MAX_PROMPTS)throw Error(`В файле должно быть от 1 до ${MAX_PROMPTS} промптов.`);
  for(let i=0;i<parts.length;i++)if(parts[i].length>MAX_PROMPT_LENGTH)throw Error(`Промпт ${i+1}: ${parts[i].length} символов; максимум ${MAX_PROMPT_LENGTH}. Проверьте разделитель. Текст не обрезан.`);
  return parts;
}

export function parsePromptBundle(text,separator='paragraphs'){
 if(typeof text==='string'&&/^\s*\[\s*(?:\{|\])/.test(text)){
  if(text.length>4000000)throw Error('Файл промптов слишком большой.');
  let items;try{items=JSON.parse(text);}catch{throw Error('JSON должен содержать массив объектов prompt и narration.');}
  if(!Array.isArray(items)||!items.length||items.length>MAX_PROMPTS)throw Error('Недопустимое количество сцен в JSON.');
  return items.map((item,i)=>{
   if(!item||typeof item!=='object'||typeof item.prompt!=='string'||!item.prompt.trim()||item.prompt.trim().length>MAX_PROMPT_LENGTH||
     typeof item.narration!=='string'||!item.narration.trim()||item.narration.trim().length>4000||/[\u0000\ufffd]/u.test(item.prompt+item.narration))throw Error('Сцена '+(i+1)+': нужны корректные prompt и narration.');
   return {prompt:item.prompt.trim(),narration:item.narration.trim()};
  });
 }
 return parsePrompts(text,separator).map(prompt=>({prompt,narration:''}));
}

export function decodeTxt(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length>8000000)throw Error('Выберите TXT не больше 8 МБ.');
  let text;
  if(bytes[0]===255&&bytes[1]===254)text=new TextDecoder('utf-16le',{fatal:true}).decode(bytes);
  else if(bytes[0]===254&&bytes[1]===255)text=new TextDecoder('utf-16be',{fatal:true}).decode(bytes);
  else try{text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}
  catch{text=new TextDecoder('windows-1251',{fatal:true}).decode(bytes);}
  if(/[\u0000\ufffd]/u.test(text))throw Error('Не удалось прочитать TXT. Сохраните файл в UTF-8.');
  return text;
}

export function formatImportTime(seconds){
  const n=Math.round(seconds);return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;
}

function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function field(label,input){const n=node('label');n.style.cssText='display:grid;gap:6px;margin:12px 0';n.append(node('span',label),input);return n;}

export function openPromptImport({dialog,api,onCreated,notice}){
  const body=dialog.querySelector('#editor-body');body.replaceChildren();
  body.append(node('h2','Импорт готовых промптов · TXT / JSON'),node('p','TXT: один промпт — один кадр. JSON: пары prompt и narration помогают точнее связать сцену с озвучкой. Стиль берётся из самих промптов.','muted'));
  const title=node('input');title.type='text';title.maxLength=200;title.setAttribute('aria-label','Название проекта из промптов');
  const script=node('input');script.type='file';script.accept='.txt,text/plain';script.setAttribute('aria-label','Файл сценария TXT');
  const prompts=node('input');prompts.type='file';prompts.accept='.txt,.json,text/plain,application/json';prompts.setAttribute('aria-label','Файл готовых промптов TXT или JSON');
  const separator=node('select');separator.setAttribute('aria-label','Разделитель промптов');
  for(const [value,label] of [['paragraphs','Пустая строка между промптами'],['lines','Каждая непустая строка — отдельный промпт']]){const o=node('option',label);o.value=value;separator.append(o);}
  const seconds=node('input');seconds.type='number';seconds.min='1';seconds.max='60';seconds.step='0.5';seconds.value='5';seconds.setAttribute('aria-label','Секунд на кадр');
  const summary=node('p','Выберите оба файла.','muted');summary.setAttribute('role','status');summary.dataset.testid='prompt-import-summary';
  const preview=node('div');preview.dataset.testid='prompt-import-preview';
  const error=node('p');error.setAttribute('role','alert');error.style.color='#ff8686';
  const submit=node('button','Создать сцены из промптов','primary');submit.type='button';submit.disabled=true;
  let storyText='',promptsText='',loading=0,submitting=false;
  function update(){
    error.textContent='';preview.replaceChildren();submit.disabled=true;
    try{
      if(!storyText.trim()||!promptsText.trim()){summary.textContent='Выберите оба файла.';return;}
      if(storyText.length>160000)throw Error('Сценарий должен быть не больше 160 000 символов.');
      const list=parsePrompts(promptsText,separator.value),time=Number(seconds.value);
      if(!Number.isFinite(time)||time<1||time>60)throw Error('Длительность кадра — от 1 до 60 секунд.');
      summary.textContent=`${list.length} кадров × ${time} сек. = ${formatImportTime(list.length*time)}. Привязка к озвучке выполняется при монтаже.`;
      const hints=parsePromptBundle(promptsText,separator.value).filter(item=>item.narration).length;if(hints)summary.textContent+=` Фрагментов озвучки: ${hints}.`;
      for(const index of [...new Set([0,1,list.length-1])].filter(i=>i<list.length)){
        const detail=node('details');detail.append(node('summary',`Кадр ${index+1} · ${formatImportTime(index*time)}–${formatImportTime((index+1)*time)}`),node('p',list[index]));preview.append(detail);
      }
      submit.disabled=loading>0||submitting;
    }catch(e){error.textContent=e.message;summary.textContent='Проверьте файл и разделитель.';}
  }
  async function read(input,isScript){
    const file=input.files?.[0];if(!file){if(isScript)storyText='';else promptsText='';update();return;}
    if(isScript)storyText='';else promptsText='';loading++;update();
    try{
      const text=decodeTxt(await file.arrayBuffer());
      if(input.files?.[0]!==file)return;
      if(isScript){storyText=text;if(!title.value.trim())title.value=file.name.replace(/\.txt$/i,'').replace(/[_-]+/g,' ').slice(0,200);}
      else promptsText=text;
    }catch(e){error.textContent=e.message;}finally{loading--;const message=error.textContent;update();if(message)error.textContent=message;}
  }
  script.onchange=()=>read(script,true);prompts.onchange=()=>read(prompts,false);
  separator.onchange=seconds.oninput=update;
  submit.onclick=async()=>{
    if(submitting||submit.disabled)return;
    submitting=true;update();
    try{
      parsePrompts(promptsText,separator.value);
      const created=await api('/projects/from-prompts',{title:title.value.trim()||'Проект из промптов',
        story:storyText,prompts_text:promptsText,separator:separator.value,seconds_per_scene:Number(seconds.value),
        story_filename:script.files[0].name,prompts_filename:prompts.files[0].name});
      dialog.close();await onCreated(created);
      notice(`Импортировано ${created.scenes.length} кадров. Можно запускать генерацию изображений.`);
    }catch(e){error.textContent=e.message;}finally{submitting=false;if(dialog.open){const message=error.textContent;update();error.textContent=message;}}
  };
  body.append(field('Название проекта',title),field('Сценарий · TXT',script),field('Промпты сцен · TXT / JSON',prompts),
    field('Как разделены сцены',separator),field('Длительность одного кадра, сек.',seconds),summary,preview,error,
    node('p',`Формат: один промпт на абзац, между ними пустая строка. До ${MAX_PROMPTS} кадров; до ${MAX_PROMPT_LENGTH} символов в каждом. Обычные пробелы остаются частью промпта.`,'muted'),submit);
  const example=node('a','Скачать образец JSON с фрагментами озвучки');example.href='/prompt-narration-example.json';example.download='prompts-with-narration.json';const task=node('a','Задание для чата, который пишет промпты');task.href='/prompt-narration-chat.txt';task.download='prompt-narration-chat.txt';body.append(example,node('br'),task);
  dialog.showModal();
}

export function editImportedPrompt({scene,dialog,save,notice}){
  const body=dialog.querySelector('#editor-body');body.replaceChildren();body.append(node('h2',`Промпт кадра ${scene.position}`));
  const input=node('textarea');input.value=scene.data.manual_prompt;input.rows=12;input.style.width='100%';input.setAttribute('aria-label','Промпт импортированного кадра');input.maxLength=MAX_PROMPT_LENGTH;
  const count=node('p',`${input.value.length} / ${MAX_PROMPT_LENGTH}`,'muted');input.oninput=()=>count.textContent=`${input.value.length} / ${MAX_PROMPT_LENGTH}`;
  const button=node('button','Сохранить промпт','primary');button.type='button';
  button.onclick=async()=>{button.disabled=true;try{if(!input.value.trim())throw Error('Промпт не должен быть пустым.');await save({manual_prompt:input.value});dialog.close();}catch(e){notice(e.message,true);}finally{button.disabled=false;}};
  body.append(input,count,button);dialog.showModal();
}
