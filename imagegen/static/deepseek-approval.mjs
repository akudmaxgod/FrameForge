export function watchPaidPassApprovals(api){
 let stopped=false,timer,box,request,busy=false;
 const close=()=>{box?.remove();box=null;request=null;};
 const show=item=>{
  if(request?.id===item.id)return;close();request=item;
  box=document.createElement('section');box.setAttribute('role','alertdialog');box.setAttribute('aria-label','Дополнительный платный проход DeepSeek');
  box.style.cssText='position:fixed;right:22px;bottom:22px;z-index:100100;width:min(470px,calc(100vw - 44px));box-sizing:border-box;padding:20px;border:1px solid #a78bfa;border-radius:12px;background:#171b2e;color:#f3f4f6;box-shadow:0 12px 45px #0009;font:14px/1.5 system-ui';
  const title=document.createElement('strong');title.textContent='Следующий проход использует платный DeepSeek API';
  const label=document.createElement('p');label.textContent=item.label;
  const countdown=document.createElement('p');countdown.dataset.testid='deepseek-countdown';
  const note=document.createElement('p');note.textContent='Без ответа автоматически продолжим локальной моделью. Платный запрос выполняется только после вашего подтверждения. Точная стоимость зависит от объёма и тарифа; ответ ограничен 10 000 токенов.';
  const decide=async decision=>{if(busy)return;busy=true;try{await api('/deepseek/approvals/'+item.id,{decision});close();}catch(error){note.textContent=error.message;}finally{busy=false;}};
  const paid=document.createElement('button');paid.textContent='Запустить DeepSeek · платно';paid.onclick=()=>decide('deepseek');
  const local=document.createElement('button');local.textContent='Использовать локальную модель';local.onclick=()=>decide('local');local.style.marginLeft='8px';
  box.append(title,label,countdown,note,paid,local);document.body.append(box);
  const tick=()=>{if(!box||request?.id!==item.id)return;const seconds=Math.max(0,Math.ceil(item.expires_at-Date.now()/1000));countdown.textContent=seconds?`До автоматического выбора локальной модели: ${seconds} с`:'Переключаемся на локальную модель…';paid.disabled=seconds===0;setTimeout(tick,250);};tick();
 };
 const poll=async()=>{if(stopped)return;try{const result=await api('/deepseek/approvals');const item=result.requests?.[0];if(item)show(item);else close();}catch{}finally{if(!stopped)timer=setTimeout(poll,1500);}};
 timer=setTimeout(poll,1500);return ()=>{stopped=true;clearTimeout(timer);close();};
}
