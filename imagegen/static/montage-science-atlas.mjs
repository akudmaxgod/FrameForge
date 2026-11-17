import {drawSlideReveal} from './science-slide-reveal.mjs';
import {ATLAS} from './science-atlas-config.mjs';
let fontReady;
function loadFont(){return fontReady??=(async()=>{const font=new FontFace('ScienceAtlas','url(/static/fonts/science-atlas.ttf)',{weight:'100 900'});await font.load();document.fonts.add(font);})();}
export function visibleNodes(plan,seconds){
 const offset=(plan.nodes||[]).length+(plan.elements||[]).length;
 let visible=[...(plan.nodes||[]),...(plan.elements||[]),...(plan.annotations||[])].map((_,i)=>i).filter(i=>plan.cue_ms?.[i]==null||seconds*1000>=plan.cue_ms[i]);
 if(plan.annotations?.length&&plan.cue_ms?.length){const active=visible.filter(i=>i>=offset);if(active.length)visible=visible.filter(i=>i<offset||i===Math.max(...active));}
 return visible;
}
export function focusState(plan,seconds){
 const rings=new Map((plan.commands||[]).filter(c=>c.type==='ring'&&c.cue_index!=null).map(c=>[c.cue_index,c])),cue=plan.cue_ms||[];
 const active=[...rings.keys()].sort((a,b)=>a-b).filter(i=>cue[i]!=null&&cue[i]<=seconds*1000);
 if(!active.length)return null;const current=active.at(-1),previous=active.length>1?active.at(-2):current,target=rings.get(current),start=rings.get(previous);
 let t=previous===current?1:Math.min(1,Math.max(0,(seconds*1000-cue[current])/350));t=t*t*(3-2*t);
 return {index:current,x:start.x+(target.x-start.x)*t,y:start.y+(target.y-start.y)*t,phase:Math.round(t*1000)/1000};
}
export function playbackCommands(plan){
 const commands=(plan.commands||[]).map(c=>({...c}));if(plan.presentation!=='visual_first')return commands;
 const scheme=plan.explanatory_scheme||{},unit=Math.max(.05,plan.illustration_motion?.duration_seconds||6)/6;
 const cues=(plan.cue_ms||[]).filter(c=>typeof c==='number'&&c>=0),base=Math.min(cues.length&&Object.keys(scheme).length?Math.min(...cues):0,Math.max(0,6000*unit-2500*unit));
 if(scheme.kind==='measurement'){
  const d=commands.find(c=>c.type==='line'&&c.y1===c.y2&&Math.abs(c.x2-c.x1)>100);
  if(d)for(const [end,direction] of [[d.x1,1],[d.x2,-1]])for(const offset of [-7,7])commands.push({type:'line',x1:end+12*direction,y1:d.y1+offset,x2:end,y2:d.y1,color:d.color,width:d.width,at_ms:Math.round(base+1500*unit),draw_ms:Math.round(250*unit)});
 }
 let order=0;
 for(const c of commands){
  if(c.cue_index!=null)continue;
  if(c.type==='text'&&(plan.text_animation||'typewriter')!=='none'&&!c.typing_cps){c.typing_cps=24;if(!c.at_ms)c.at_ms=c.size>=38||plan.layout==='comparison'?0:Math.round(base+(c.size<=23?150:Object.keys(scheme).length?1750:600)*unit);}
  if(['ring','line','circle'].includes(c.type)&&!c.draw_ms){if(c.at_ms==null)c.at_ms=Math.round(base+(scheme.kind==='measurement'&&c.type==='line'?1050+order*120:400+Math.min(order,12)*160)*unit);c.draw_ms=Math.max(50,Math.round((c.type==='ring'?650:280)*unit));order++;}
 }
 for(const c of commands)if(c.typing_cps){const available=Math.max(.08,(6000*unit-(c.at_ms||0)-100*unit)/1000);c.typing_cps=Math.max(c.typing_cps,((c.text||'').length+(c.typing_offset||0)+1)/available);}
 return commands;
}
export function fitTextCommands(commands,ctx,duration=6){
 const result=[];
 for(const original of commands){
  if(original.type!=='text'){result.push(original);continue;}
  const c={...original};let x=Math.max(48,Math.min(c.x,1712)),right=x>=1280?1856:1872;
  for(const other of commands)if(other.type==='text'&&Math.abs(other.y-c.y)<c.size*.6&&other.x>x+30)right=Math.min(right,other.x-18);
  const width=Math.max(32,right-x);let y=Math.max(32+c.size,c.y),height=Math.min(150,1032-y+c.size*.8);
  for(const other of commands)if(other.type==='text'&&other.y>y+8&&Math.abs(other.x-x)<width*.65)height=Math.min(height,other.y-y+c.size*.8-other.size*.8-8);
  height=Math.max(c.size*1.1,height);
  const wrap=size=>{ctx.font=`${c.weight} ${size}px ScienceAtlas`;const rows=[];let line='';for(let word of c.text.split(/\s+/).filter(Boolean)){
   if(line&&ctx.measureText(line+' '+word).width>width){rows.push(line);line='';}
   while(ctx.measureText(word).width>width&&word.length>1){let end=1;while(end<word.length&&ctx.measureText(word.slice(0,end+1)).width<=width)end++;if(line){rows.push(line);line='';}rows.push(word.slice(0,end));word=word.slice(end);}
   if(word)line=(line+' '+word).trim();}if(line)rows.push(line);return rows;};
  let size=Math.round(c.size),rows;for(;;){rows=wrap(size);if(rows.length*size*1.25<=height||size<=16)break;size--;}
  y=Math.max(32+size,Math.min(y,1032-Math.max(0,rows.length-1)*size*1.25));let offset=c.typing_offset||0;
  rows.forEach((text,i)=>{const item={...c,x,y:y+i*size*1.25,size,text,text_max_width:width,clip_box:[48,24,1824,1032]};if(c.typing_cps){item.typing_offset=offset;offset+=text.length+1;}result.push(item);});
 }
 for(const c of result)if(c.typing_cps)c.typing_cps=Math.max(c.typing_cps,((c.text||'').length+(c.typing_offset||0)+1)/Math.max(.08,(duration*1000-(c.at_ms||0)-duration*1000/60)/1000));
 return result;
}
export async function drawScienceAtlas(ctx,still,plan,seconds){
  await loadFont();
  if(['slide_explain','staged_explain','focus_explain','detail_explain'].includes(plan.presentation_motion))return drawSlideReveal(ctx,still,plan,seconds,ATLAS.background);
  const typographyKey=JSON.stringify([plan.commands,plan.text_animation,plan.explanatory_scheme,plan.cue_ms,plan.illustration_motion]);
  if(still.typographyKey!==typographyKey){still.typesetCommands=fitTextCommands(playbackCommands(plan),ctx,plan.illustration_motion?.duration_seconds||6);still.typographyKey=typographyKey;}
  const commands=still.typesetCommands;
  const visible=visibleNodes(plan,seconds),focus=plan.presentation==='visual_first'?focusState(plan,seconds):null,signature=JSON.stringify([commands,visible,focus?.phase,ctx.canvas.width,ctx.canvas.height,commands.filter(c=>c.at_ms).map(c=>seconds*1000>=c.at_ms),(plan.illustration_motion?.strength||commands.some(c=>c.typing_cps||c.draw_ms||c.type==='pulse'))?Math.round(seconds*24):null]);
  if(still.atlasSignature!==signature){
    const canvas=document.createElement('canvas');canvas.width=ctx.canvas.width;canvas.height=ctx.canvas.height;
    const draw=canvas.getContext('2d');draw.fillStyle=ATLAS.background;draw.fillRect(0,0,canvas.width,canvas.height);draw.scale(canvas.width/1920,canvas.height/1080);
    still.atlasImages??=new Map();
    // Images precede overlays in the retained command list, just as in the exporter.
    for(const cmd of commands){
      if(seconds*1000<(cmd.at_ms||0))continue;
      const progress=cmd.draw_ms?Math.max(0,Math.min(1,(seconds*1000-(cmd.at_ms||0))/cmd.draw_ms)):1;if(progress<=0)continue;
      if(cmd.cue_index!=null&&!visible.includes(cmd.cue_index))continue;
      if(cmd.type==='image'){
        if(!still.atlasImages.has(cmd.asset_id)){const image=new Image();image.src='/api/assets/'+cmd.asset_id;await image.decode();still.atlasImages.set(cmd.asset_id,image);}
        const image=still.atlasImages.get(cmd.asset_id),[x,y,w,h]=cmd.rect,k=Math.min(w/image.naturalWidth,h/image.naturalHeight),iw=image.naturalWidth*k,ih=image.naturalHeight*k;
        if(cmd.crop){const [l,t,r,b]=cmd.crop;draw.drawImage(image,l*image.naturalWidth,t*image.naturalHeight,(r-l)*image.naturalWidth,(b-t)*image.naturalHeight,x,y,w,h);}
        else {const motion=plan.illustration_motion,main=plan.illustrations?.[0]?.asset_id===cmd.asset_id;let factor=1;
          if(main&&motion?.strength&&!(plan.annotations||[]).some(a=>a.position_verified)){let t=Math.max(0,Math.min(1,seconds/Math.max(.1,motion.duration_seconds||6)));t=t*t*(3-2*t);const strength=Math.max(0,Math.min(.12,motion.strength));factor=(1+strength*t)/(1+strength);}
          const mw=iw*factor,mh=ih*factor;draw.drawImage(image,x+(w-mw)/2,y+(h-mh)/2,mw,mh);}
      }else if(cmd.type==='text'){draw.fillStyle=cmd.color;draw.font=`${cmd.weight} ${cmd.size}px ScienceAtlas`;draw.textBaseline='alphabetic';draw.save();if(cmd.clip_box){draw.beginPath();draw.rect(...cmd.clip_box);draw.clip();}draw.fillText(cmd.typing_cps?cmd.text.slice(0,Math.max(0,Math.floor(Math.max(0,seconds*1000-(cmd.at_ms||0))/1000*cmd.typing_cps)-(cmd.typing_offset||0))):cmd.text,cmd.x,cmd.y,cmd.text_max_width||1920);draw.restore();}
      else if(cmd.type==='line'){draw.strokeStyle=cmd.color;draw.lineWidth=cmd.width;draw.beginPath();draw.moveTo(cmd.x1,cmd.y1);draw.lineTo(cmd.x1+(cmd.x2-cmd.x1)*progress,cmd.y1+(cmd.y2-cmd.y1)*progress);draw.stroke();}
      else if(cmd.type==='rect'){draw.strokeStyle=cmd.color;draw.lineWidth=cmd.width;if(cmd.fill){draw.fillStyle=cmd.color;draw.fillRect(cmd.x,cmd.y,cmd.w,cmd.h);}else draw.strokeRect(cmd.x,cmd.y,cmd.w,cmd.h);}
      else if(cmd.type==='ring'){draw.strokeStyle=cmd.color;draw.lineWidth=cmd.width;draw.beginPath();draw.arc(focus&&cmd.cue_index===focus.index?focus.x:cmd.x,focus&&cmd.cue_index===focus.index?focus.y:cmd.y,cmd.radius,-Math.PI/2,-Math.PI/2+Math.PI*2*progress);draw.stroke();}
      else if(cmd.type==='pulse'){const t=(seconds*1000/(cmd.period_ms||2200)+(cmd.phase||0))%1;draw.fillStyle=cmd.color;draw.beginPath();draw.arc(cmd.x1+(cmd.x2-cmd.x1)*t,cmd.y1+(cmd.y2-cmd.y1)*t,cmd.radius||6,0,Math.PI*2);draw.fill();}
      else if(cmd.type==='circle'){draw.fillStyle=cmd.color;draw.beginPath();draw.arc(cmd.x,cmd.y,cmd.radius*progress,0,Math.PI*2);draw.fill();}
    }
    still.atlasCanvas=canvas;still.atlasSignature=signature;
    // The two visible clips need only their current illustration sets.
    const used=new Set(commands.filter(c=>c.type==='image').map(c=>c.asset_id));for(const key of still.atlasImages.keys())if(!used.has(key))still.atlasImages.delete(key);
  }
  ctx.drawImage(still.atlasCanvas,0,0);
}
