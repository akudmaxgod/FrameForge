export function drawEffects(canvas, settings={}, index=0) {
  const film=(settings.old_film||0)/100, flare=(settings.light_leaks||0)/100;
  canvas.style.visibility=film||flare?'visible':'hidden';
  if(!film&&!flare)return;
  const {width,height}=canvas,ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,width,height);
  if(flare){
    const level=flare*(.45+.55*(.5+.5*Math.sin(index*.055))**3);
    ctx.globalCompositeOperation='lighten';
    for(const [x,y,radius,opacity] of [[.05,.17,.61,.48],[.68,.64,.09,.11]]){
      const glow=ctx.createRadialGradient(x*width,y*height,0,x*width,y*height,radius*width);
      glow.addColorStop(0,`rgba(255,171,98,${level*opacity})`);
      glow.addColorStop(1,'rgba(255,171,98,0)');
      ctx.fillStyle=glow;
      ctx.fillRect(0,0,width,height);
    }
    ctx.globalCompositeOperation='source-over';
  }
  if(film){
    let seed=index+1;
    const sample=()=>{seed=(Math.imul(1664525,seed)+1013904223)>>>0;return seed/4294967296;};
    for(let point=0;point<180;point++){
      const x=sample()*width,y=sample()*height,dust=point<3;
      const radius=(dust?.0012+sample()*.0018:.0006)*width;
      const tone=dust||point%2===0?18:240;
      ctx.fillStyle=`rgba(${tone},${tone},${tone},${(dust?125:29)*film/255})`;
      ctx.beginPath();
      ctx.ellipse(x,y,radius,radius*.55,0,0,Math.PI*2);
      ctx.fill();
    }
  }
}
