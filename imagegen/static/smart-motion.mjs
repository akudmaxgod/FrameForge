export function smartMotion(scene,position=0){
  const shot=String(scene.shot||'').toLowerCase(),text=[scene.beat||'',scene.source_quote||''].join(' ').toLowerCase();
  const has=(value,words)=>words.some(word=>value.includes(word));
  if(has(shot,['close','portrait','face','detail','insert','крупн','портрет','лицо','деталь']))return position%3===2?{preset:'gentle_pull_back',reason:'Мягкое отдаление от детали: спокойная смена движения между соседними кадрами.'}:{preset:'gentle_push_in',reason:'Мягкий наезд: акцент на лице или детали, без сильного кадрирования.'};
  if(has(shot,['wide','panoramic','establishing','landscape','panorama','общий','широк','панорам','пейзаж']))return position%3!==1?{preset:position%3===2?'gentle_pan_left':'gentle_pan_right',reason:'Спокойный проход по общему плану: небольшой запас по краям сохраняет картинку внутри кадра.'}:{preset:'gentle_pull_back',reason:'Мягкое отдаление: раскрыть место и сохранить общий план.'};
  if(has(shot,['atmosphere','атмосфер']))return {preset:position%2?'gentle_pan_left':'gentle_pan_right',reason:'Заметный плавный проход камеры для атмосферного плана, без изменения масштаба.'};
  if(has(text,['reveals the','opens the view','уходит','покидает','открывается вид','затихает']))return {preset:'gentle_pull_back',reason:'Мягкое отдаление: раскрытие пространства или завершение эпизода.'};
  if(has(text,['notices','discovers','reaction','замечает','обнаруживает','осознаёт','осознает','реакция']))return {preset:'gentle_push_in',reason:'Мягкий наезд: внимание к находке или реакции.'};
  if(Number.isInteger(scene.visible_people)&&scene.visible_people>=2)return {preset:'gentle_pull_back',reason:'Мягкое отдаление: сохранить группу в кадре.'};
  return {preset:position%2?'gentle_push_in':'gentle_pull_back',reason:'Спокойное движение: чередование наезда и отдаления для нейтральных планов.'};
}
