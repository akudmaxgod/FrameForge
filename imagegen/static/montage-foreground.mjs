const clamp = value => Math.max(0, Math.min(1, value));
const url = id => '/api/assets/' + encodeURIComponent(id);

async function imageFor(id) {
  const image = new Image();
  image.src = url(id);
  await image.decode();
  return image;
}

export async function drawForeground(ctx, still, clip, image, seconds, video) {
  const effect = clip.foreground_effect;
  const {width, height} = ctx.canvas;
  const scale = (clip.framing === 'fill' ? Math.max : Math.min)(
    width / image.naturalWidth, height / image.naturalHeight);
  const w = Math.round(image.naturalWidth * scale), h = Math.round(image.naturalHeight * scale);
  const x = Math.floor((width - w) / 2), y = Math.floor((height - h) / 2);
  const elapsed = (seconds * 1000 - clip.trim_in_ms) / (clip.playback_rate ?? 1);
  const t = clamp(elapsed / Math.max(1000 / 24, clip.base_duration_ms - 1000 / 24));
  if (effect.mode === 'local_25d') {
    ctx.drawImage(video, x, y, w, h);
    return;
  }
  if (effect.mode === 'parallax') {
    const key = effect.mode + ':' + effect.background + ':' + effect.foreground;
    if (still.foregroundKey !== key) {
      const [background, foreground] = await Promise.all([imageFor(effect.background), imageFor(effect.foreground)]);
      still.foregroundLayers = [background, effect.layout === 'parallax-v2' ? foreground : padForeground(foreground)];
      still.foregroundKey = key;
    }
    const paint = effect.layout === 'parallax-v2' ? paintRegistered : paintParallax;
    paint(ctx, ...still.foregroundLayers, x, y, w, h, effect, t);
    return;
  }
  if (effect.flow_pair) {
    if (!effect.motion) {
      ctx.drawImage(image, x, y, w, h);
      return;
    }
    if (still.motionBackgroundId !== effect.background) {
      still.motionBackground = await imageFor(effect.background);
      still.motionBackgroundId = effect.background;
    }
    ctx.drawImage(still.motionBackground, x, y, w, h);
    const surface = still.keySurface ??= document.createElement('canvas');
    const frameH = video.videoHeight;
    const frameW = effect.motion_center_crop ? frameH : video.videoWidth;
    const left = Math.floor((video.videoWidth - frameW) / 2);
    if (surface.width !== frameW || surface.height !== frameH) {
      surface.width = frameW;
      surface.height = frameH;
    }
    const key = surface.getContext('2d', {willReadFrequently: true});
    key.drawImage(video, left, 0, frameW, frameH, 0, 0, frameW, frameH);
    const pixels = key.getImageData(0, 0, frameW, frameH);
    const rgba = pixels.data;
    for (let i = 0; i < rgba.length; i += 4) {
      const neutral = Math.max(rgba[i], rgba[i + 2]);
      const green = rgba[i + 1] - neutral;
      rgba[i + 3] = 255 * clamp((78 - green) / 52);
      if (green > 22) rgba[i + 1] = Math.min(rgba[i + 1], neutral + 14);
    }
    key.putImageData(pixels, 0, 0);
    ctx.drawImage(surface, x, y, w, h);
    return;
  }
  if (effect.mode === 'drift' || effect.mode === 'paired') {
    const shift = w * .014 * Math.sin(Math.PI * t) ** 2 * effect.strength;
    if (effect.mode === 'drift' && shift < .5) {
      ctx.drawImage(image, x, y, w, h);
      return;
    }
    const key = effect.mode + ':' + effect.background + ':' + effect.foreground;
    if (still.foregroundKey !== key) {
      const layers = await Promise.all([imageFor(effect.background), imageFor(effect.foreground)]);
      still.foregroundLayers = layers;
      still.foregroundKey = key;
    }
    const [background, foreground] = still.foregroundLayers;
    if (effect.mode === 'paired') {
      paintPair(ctx, background, foreground, x, y, w, h, effect, t);
      return;
    }
    const slope = shift / Math.max(1, h * (effect.bottom - effect.top));
    const pivot = y + h * effect.bottom;
    ctx.drawImage(background, x, y, w, h);
    ctx.save();
    ctx.transform(1, 0, -slope, 1, slope * pivot, 0);
    ctx.drawImage(foreground, x, y, w, h);
    ctx.restore();
    return;
  }
  ctx.drawImage(image, x, y, w, h);
  const strength = effect.strength * Math.exp(-.5 * ((t - effect.peak) / .075) ** 2);
  if (strength < .003) return;
  const cx = Math.round(x + w * effect.x), cy = Math.round(y + h * effect.y);
  const radius = Math.max(2, Math.round(.026 * w));
  const gradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
  for (const [stop, opacity] of [[0, .94], [.27, .27], [1, 0]]) {
    gradient.addColorStop(stop, `rgba(255,229,166,${opacity * strength})`);
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();
  ctx.fillStyle = gradient;
  ctx.fillRect(cx - radius, cy - radius, radius * 2 + 1, radius * 2 + 1);
  ctx.restore();
}

function paintPair(ctx, background, foreground, x, y, width, height, effect, time) {
  const t = clamp((time - .30) / .40);
  const angle = 3.2 * effect.strength * effect.direction * Math.sin(Math.PI * t) ** 2 * Math.PI / 180;
  const px = x + width * effect.pivot_x;
  const py = y + height * effect.pivot_y;
  ctx.drawImage(background, x, y, width, height);
  ctx.save();
  ctx.translate(px, py);
  ctx.rotate(angle);
  ctx.drawImage(foreground, x - px, y - py, width, height);
  ctx.restore();
}

async function loadLocalImage(file) {
  if (!file || file.size > 12_000_000) throw Error('Выберите изображение до 12 МБ.');
  const source = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = source;
    await image.decode();
    return image;
  } finally {
    URL.revokeObjectURL(source);
  }
}

function prepareLayer(image, width, height, keyGreen) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const inputRatio = image.naturalWidth / image.naturalHeight;
  if (Math.abs(inputRatio / (width / height) - 1) > .02)
    throw Error('Оба слоя должны иметь те же пропорции, что и исходный кадр.');
  const ctx = canvas.getContext('2d');
  ctx.drawImage(image, 0, 0, width, height);
  if (keyGreen) {
    const frame = ctx.getImageData(0, 0, width, height);
    const pixels = frame.data;
    for (let i = 0; i < pixels.length; i += 4) {
      const neutral = Math.max(pixels[i], pixels[i+2]);
      const green = pixels[i+1] - neutral;
      if (green > 28) {
        pixels[i+3] *= 1 - clamp((green - 28) / 45);
        pixels[i+1] = Math.min(pixels[i+1], neutral + 25);
      }
    }
    ctx.putImageData(frame, 0, 0);
  }
  return canvas;
}

function padForeground(image) {
  const width = image.naturalWidth || image.width, height = image.naturalHeight || image.height;
  const px = Math.ceil(width * .03) + 2, py = Math.ceil(height * .03) + 2;
  const canvas = document.createElement('canvas');
  canvas.width = width + 2*px;
  canvas.height = height + 2*py;
  const ctx = canvas.getContext('2d');
  const columns = [[0,px,0,true],[0,width,px,false],[width-px,px,px+width,true]];
  const rows = [[0,py,0,true],[0,height,py,false],[height-py,py,py+height,true]];
  for (const [sx, sw, dx, flipX] of columns) {
    for (const [sy, sh, dy, flipY] of rows) {
      ctx.save();
      ctx.translate(dx + (flipX ? sw : 0), dy + (flipY ? sh : 0));
      ctx.scale(flipX ? -1 : 1, flipY ? -1 : 1);
      ctx.drawImage(image,sx,sy,sw,sh,0,0,sw,sh);
      ctx.restore();
    }
  }
  return {canvas, width, height, px, py};
}

function paintParallax(ctx, background, front, x, y, width, height, effect, t) {
  const sx = width/front.width, sy = height/front.height;
  ctx.drawImage(background, x, y, width, height);
  ctx.save();
  ctx.beginPath();
  ctx.rect(x,y,width,height);
  ctx.clip();
  ctx.drawImage(front.canvas,
    x + width*effect.shift_x*(t-.5) - front.px*sx,
    y + height*effect.shift_y*(t-.5) - front.py*sy,
    front.canvas.width*sx, front.canvas.height*sy);
  ctx.restore();
}

function paintRegistered(ctx, background, foreground, x, y, width, height, effect, t) {
  const zoom = 1 / (1 - Math.max(Math.abs(effect.shift_x), Math.abs(effect.shift_y)) - .006);
  const w = Math.ceil(width * zoom), h = Math.ceil(height * zoom);
  const left = x - (w - width) / 2, top = y - (h - height) / 2;
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, width, height);
  ctx.clip();
  ctx.drawImage(background, left, top, w, h);
  ctx.drawImage(foreground, left + width * effect.shift_x * (t - .5),
    top + height * effect.shift_y * (t - .5), w, h);
  ctx.restore();
}

export function foregroundPairDialog(clip, context, apply) {
  const make = (tag, text) => {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    return element;
  };
  const dialog = make('dialog');
  dialog.className = 'montage-foreground-dialog';
  const prompts = make('details');
  prompts.append(make('summary','Промпты для подготовки изображений'));
  const common = 'Use the approved scene image. Match canvas, framing, camera, lighting, scale and all positions. Separate by depth. Keep all nearest figures, clothing, carried objects and their near table, support, nearby ground and vegetation together as a single near plane. Distant figures and architecture belong to the far plane. ';
  for (const [name, description] of [
    ['Задник', 'Output the far plane only. Reconstruct the area behind the entire removed near plane. Keep all visible far elements in place.'],
    ['Передний план', 'Output the entire near plane at the same full canvas size and position. Everything else must be transparent or uniform pure green (#00FF00). Do not crop or recenter.']
  ]) {
    const area = make('textarea');
    area.readOnly = true;
    area.setAttribute('aria-label', name);
    area.value = common + description + (context.beat ? ' Scene context: ' + context.beat : '');
    const copy = make('button','Копировать');
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(area.value); status.textContent = 'Промпт скопирован.'; }
      catch { area.select(); }
    };
    prompts.append(make('p',name),area,copy);
  }
  const format = make('select');
  format.setAttribute('aria-label','Фон переднего изображения');
  for (const [value, label] of [['alpha','Прозрачный PNG'],['green','Зелёный фон']]) {
    const option = make('option',label);
    option.value = value;
    format.append(option);
  }
  const files = {};
  const rows = [];
  for (const [key, label] of [['background','Неподвижный задник'],['foreground','Вся передняя композиция']]) {
    const row = make('label',label + ' ');
    const input = make('input');
    input.type='file';
    input.accept='image/png,image/jpeg,image/webp';
    row.append(input);
    files[key]=input;
    rows.push(row);
  }
  const canvas = make('canvas');
  canvas.width=800;
  canvas.height=450;
  canvas.className='montage-pair-preview';
  canvas.setAttribute('aria-label','Предпросмотр параллакса');
  const axis = (label, value) => {
    const row=make('label',label+' ');
    const field=make('input');
    field.type='number';
    field.min='-6';
    field.max='6';
    field.step='any';
    field.required=true;
    field.value=String(value);
    row.append(field);
    return [row,field];
  };
  const old=clip.foreground_effect?.mode==='parallax'?clip.foreground_effect:null;
  const [horizontal,dx]=axis('Горизонталь, %',(old?.shift_x??.028)*100);
  const [vertical,dy]=axis('Вертикаль, %',(old?.shift_y??.010)*100);
  const settings=()=>({shift_x:Number(dx.value)/100,shift_y:Number(dy.value)/100});
  const status=make('p','Загрузи два изображения одной сцены в одинаковом кадрировании.');
  status.setAttribute('role','status');
  const play=make('button','Воспроизвести');
  const save=make('button','Применить к клипу');
  const close=make('button','Закрыть');
  const ctx=canvas.getContext('2d');
  let source, layers, animation, saving=false, generation=0;
  const stop=()=>cancelAnimationFrame(animation);
  function paint(t) {
    ctx.clearRect(0,0,canvas.width,canvas.height);
    if (layers) paintRegistered(ctx,layers.background,layers.foreground,0,0,canvas.width,canvas.height,settings(),t);
    else if (source) ctx.drawImage(source,0,0,canvas.width,canvas.height);
  }
  async function prepare() {
    const attempt=++generation;
    stop();
    layers=null;
    save.disabled=play.disabled=true;
    paint(0);
    if (!source || !files.background.files[0] || !files.foreground.files[0]) return;
    try {
      const [bg,fg]=await Promise.all([loadLocalImage(files.background.files[0]),loadLocalImage(files.foreground.files[0])]);
      if (attempt!==generation || !dialog.open) return;
      const width=source.naturalWidth, height=source.naturalHeight;
      if (width*height>25_000_000) throw Error('Выбери кадр размером до 25 мегапикселей.');
      const background=prepareLayer(bg,width,height,false);
      const foreground=prepareLayer(fg,width,height,format.value==='green');
      const pixels=foreground.getContext('2d').getImageData(0,0,width,height).data;
      let clear=0,solid=0;
      for(let i=3;i<pixels.length;i+=4){
        if(pixels[i]<8)clear++;
        if(pixels[i]>=128)solid++;
      }
      if(clear<.08*width*height || solid<.002*width*height)
        throw Error('Переднее изображение должно содержать видимую композицию на прозрачном фоне.');
      layers={background,foreground};
      status.textContent='Слои готовы. Проверь движение в предпросмотре.';
      save.disabled=play.disabled=false;
      paint(.5);
    } catch(error) {
      if(attempt===generation) status.textContent=error.message;
    }
  }
  files.background.onchange=files.foreground.onchange=format.onchange=prepare;
  dx.oninput=dy.oninput=()=>{stop();paint(1);};
  play.onclick=()=>{
    if(!dx.reportValidity() || !dy.reportValidity()) return;
    stop();
    const start=performance.now();
    const span=Math.max(1000/24,(clip.trim_out_ms-clip.trim_in_ms)/(clip.playback_rate??1)-1000/24);
    const tick=now=>{
      const t=clamp((now-start)/span);
      paint(t);
      if(t<1&&dialog.open)animation=requestAnimationFrame(tick);
    };
    animation=requestAnimationFrame(tick);
  };
  save.onclick=async()=>{
    if(saving || !layers || !dx.reportValidity() || !dy.reportValidity())return;
    stop();
    saving=true;
    const controls=[save,close,play,format,dx,dy,files.background,files.foreground];
    controls.forEach(control=>{control.disabled=true;});
    status.textContent='Сохраняю два изображения…';
    try {
      const pair=settings();
      for(const key of ['background','foreground']){
        pair[key]=layers[key].toDataURL('image/png').split(',')[1];
        if(pair[key].length>16_000_000)throw Error('Подготовленный слой превышает 12 МБ.');
      }
      const commit=await apply(pair);
      if(dialog.open){commit();dialog.close();}
    }catch(error){status.textContent=error.message;}
    finally{saving=false;controls.forEach(control=>{control.disabled=false;});}
  };
  close.onclick=()=>dialog.close();
  dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  dialog.addEventListener('close',()=>{stop();dialog.remove();},{once:true});
  save.disabled=play.disabled=true;
  dialog.append(make('h3','Параллакс из двух изображений'),prompts,...rows,format,canvas,
    horizontal,vertical,status,play,save,close);
  document.body.append(dialog);
  dialog.showModal();
  imageFor(clip.source_image_asset_id).then(image=>{
    if(!dialog.open)return;
    source=image;
    canvas.height=Math.max(1,Math.round(canvas.width*image.naturalHeight/image.naturalWidth));
    return prepare();
  }).catch(error=>{status.textContent=error.message;});
}
