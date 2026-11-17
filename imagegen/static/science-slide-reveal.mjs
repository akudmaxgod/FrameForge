const INITIAL=[72,72,1776,936],FINAL=[72,240,1070,600];
export function slideTimeline(plan){const duration=Math.max(1,(plan.illustration_motion?.duration_seconds??6)*1000),unit=Math.min(1,duration/4000),hold=(plan.presentation_motion||'slide_explain')==='slide_explain'?700*unit:0,move=(plan.presentation_motion||'slide_explain')==='slide_explain'?780*unit:1;return {duration,hold,move,end:hold+move,settle:Math.min(450*unit,duration*.15)};}
export function slideState(plan,seconds){
 const timing=slideTimeline(plan),t=Math.max(0,seconds*1000);let p=Math.max(0,Math.min(1,(t-timing.hold)/timing.move));p=p*p*p*(p*(p*6-15)+10);
 const final=[...(plan.slide_layout?.final_rect||FINAL)];if(!plan.slide_layout&&(plan.slide_direction||'left')==='right')final[0]=1920-final[0]-final[2];
 const k=Math.min(INITIAL[2]/final[2],INITIAL[3]/final[3]),start=[INITIAL[0]+(INITIAL[2]-final[2]*k)/2,INITIAL[1]+(INITIAL[3]-final[3]*k)/2,final[2]*k,final[3]*k];
 const mode=plan.presentation_motion||'slide_explain';let rect=start.map((a,i)=>a+(final[i]-a)*p);if(mode!=='slide_explain'){p=1;rect=[...final];}if(mode==='focus_explain'){let q=Math.max(0,Math.min(1,t/timing.duration));q=q*q*(3-2*q);const factor=.985+.015*q;rect=[final[0]+final[2]*(1-factor)/2,final[1]+final[3]*(1-factor)/2,final[2]*factor,final[3]*factor];}
 return {rect,final_rect:final,progress:p,opacity:Math.max(0,Math.min(1,t/Math.max(1,200*Math.min(1,timing.duration/4000))))};
}
export function slideCommandState(plan,c,seconds){
 const timing=slideTimeline(plan),unit=Math.min(1,timing.duration/4000),cue=Number.isInteger(c.cue_index)?plan.cue_ms?.[c.cue_index]:null;
 if(timing.duration<(c.min_duration_ms||0))return {start:timing.duration,progress:0,opacity:0};
 let start=typeof cue==='number'&&Number.isFinite(cue)?cue:0;
 if(c.motion_group!=='illustration'){const mode=plan.presentation_motion||'slide_explain',base=mode==='slide_explain'?timing.end:mode==='focus_explain'?80*unit:200*unit,gap=mode==='focus_explain'?0:220*unit;start=Math.max(start,base+Math.min(c.reveal_group||0,6)*gap);if(mode==='detail_explain'&&c.support_role==='detail')start=Math.max(120*unit,typeof cue==='number'?cue:0);if(plan.support_reveal_ms)start=Math.max(start,plan.support_reveal_ms);}
 const span=(plan.presentation_motion==='focus_explain'?180:c.type==='text'?350:420)*unit;start=Math.min(Math.max(0,start),Math.max(0,timing.duration-timing.settle-span));
 const progress=Math.max(0,Math.min(1,(seconds*1000-start)/Math.max(1,span)));
 return {start,progress,opacity:c.type==='text'?Math.min(1,progress*3):1};
}
export async function drawSlideReveal(ctx,still,plan,seconds,background){
 still.atlasImages??=new Map();
 for(const c of plan.commands||[])if(c.type==='image'&&!still.atlasImages.has(c.asset_id)){const im=new Image();im.src='/api/assets/'+c.asset_id;await im.decode();still.atlasImages.set(c.asset_id,im);}
 ctx.save();ctx.fillStyle=background;ctx.fillRect(0,0,ctx.canvas.width,ctx.canvas.height);ctx.scale(ctx.canvas.width/1920,ctx.canvas.height/1080);
 const group=slideState(plan,seconds),rect=group.rect,final=group.final_rect,k=rect[2]/final[2];
 for(const c of plan.commands||[]){
  const cs=slideCommandState(plan,c,seconds),p=cs.progress;if(p<=0)continue;
  ctx.save();ctx.globalAlpha=cs.opacity;
  if(c.motion_group==='illustration'){ctx.translate(rect[0],rect[1]);ctx.scale(k,k);ctx.translate(-final[0],-final[1]);ctx.globalAlpha*=group.opacity;}
  ctx.fillStyle=c.color;ctx.strokeStyle=c.color;ctx.lineWidth=c.width||1;
  if(c.type==='image'){
   const im=still.atlasImages.get(c.asset_id),[x,y,w,h]=c.rect,f=Math.min(w/im.naturalWidth,h/im.naturalHeight),iw=im.naturalWidth*f,ih=im.naturalHeight*f;
   ctx.globalAlpha*=Math.min(1,p*3);ctx.drawImage(im,x+(w-iw)/2,y+(h-ih)/2,iw,ih);
  }else if(c.type==='text'){
   ctx.font=`${c.weight||400} ${Math.round(c.size)}px ScienceAtlas`;ctx.textBaseline='alphabetic';const m=ctx.measureText(c.text),left=m.actualBoundingBoxLeft||0,right=m.actualBoundingBoxRight||m.width,top=m.actualBoundingBoxAscent||c.size,bottom=m.actualBoundingBoxDescent||0;
   ctx.beginPath();ctx.rect(c.x-left-4,c.y-top-4,(left+right+8)*p,top+bottom+8);ctx.clip();ctx.fillText(c.text,c.x,c.y);
  }else if(c.type==='line'){ctx.beginPath();ctx.moveTo(c.x1,c.y1);ctx.lineTo(c.x1+(c.x2-c.x1)*p,c.y1+(c.y2-c.y1)*p);ctx.stroke();}
  else if(c.type==='ring'){ctx.beginPath();ctx.arc(c.x,c.y,c.radius,-Math.PI/2,-Math.PI/2+2*Math.PI*p);ctx.stroke();}
  else if(c.type==='circle'){ctx.beginPath();ctx.arc(c.x,c.y,c.radius*p,0,2*Math.PI);ctx.fill();}
  else if(c.type==='rect'){ctx.beginPath();ctx.rect(c.x-8,c.y-8,(c.w+16)*p,c.h+16);ctx.clip();if(c.fill)ctx.fillRect(c.x,c.y,c.w,c.h);else ctx.strokeRect(c.x,c.y,c.w,c.h);}
  else if(c.type==='pulse'){const t=(seconds*1000/(c.period_ms||2200)+(c.phase||0))%1;ctx.beginPath();ctx.arc(c.x1+(c.x2-c.x1)*t,c.y1+(c.y2-c.y1)*t,c.radius||6,0,Math.PI*2);ctx.fill();}
  ctx.restore();
 }
 ctx.restore();
 const used=new Set((plan.commands||[]).filter(c=>c.type==='image').map(c=>c.asset_id));for(const key of still.atlasImages.keys())if(!used.has(key))still.atlasImages.delete(key);
}
