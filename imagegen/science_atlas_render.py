"""One retained drawing contract for scientific cards, preview and CUDA export."""
import copy,hashlib,io,json,math,threading
from functools import lru_cache
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from .db import uid,now,dump
from .science_atlas import VERSION,STYLE,asset_name
CONFIG=json.loads(Path(__file__).with_name('static').joinpath('science-atlas-config.json').read_text(encoding='utf-8'))
FONT=Path(__file__).with_name('static')/'fonts/science-atlas.ttf'
_LOCK=threading.RLock()

@lru_cache(maxsize=32)
def font(size,weight=400):
    result=ImageFont.truetype(str(FONT),size)
    if hasattr(result,'get_variation_axes'):
        axes=result.get_variation_axes();result.set_variation_by_axes([weight if b'Weight' in a['name'] else a['default'] for a in axes])
    return result

def lines(text,width,size,weight=400):
    f=font(size,weight);out=[];line=''
    for word in str(text).split():
        if line and f.getlength(line+' '+word)>width:out.append(line);line=word
        else:line=(line+' '+word).strip()
    if line:out.append(line)
    return out

def normalize_material(data):
    from .gpu_pixels import CudaImage
    from .domain import DomainError
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load();image=source.convert('RGB')
        k=min(1,1536/max(image.size))
        if k<1:image=CudaImage(image).resize(tuple(max(1,round(v*k)) for v in image.size))
        buffer=io.BytesIO();image.save(buffer,format='PNG');return buffer.getvalue(),image.size
    except Exception as error:raise DomainError('Не удалось прочитать иллюстрацию научпопа.') from error

def hydrate(service,scene):
    plan=copy.deepcopy(scene['data']['science_atlas']);props=service.db.all('SELECT name,references_json FROM props WHERE project_id=? AND locked=1',(scene['project_id'],))
    available={p['name']:json.loads(p['references_json']).get('prop') for p in props}
    plan['illustrations']=[{'key':k,'asset_id':available.get(asset_name(k))} for k in plan.get('asset_keys',[])]
    plan['details']=[{**d,'asset_id':available.get(asset_name(d['key']))} for d in plan.get('detail_assets',[])]
    for item in plan['illustrations']+plan['details']:
        if item.get('asset_id'):
            asset=service.asset(item['asset_id']);item['width']=asset['width'];item['height']=asset['height']
    if plan.get('presentation')=='visual_first':plan.setdefault('composition_mode','explanatory')
    return compile_commands(plan)

def render_fingerprint(plan):
    """Absent and zero reveal times describe the same rendered scene."""
    value=copy.deepcopy(plan)
    value.pop('illustration_motion',None)
    if value.get('text_animation')=='none':value.pop('text_animation',None)
    for command in value.get('commands',[]):
        if command.get('at_ms') in (None,0):command.pop('at_ms',None)
    for key in ('support_asset_key','support_cue_quote','support_reveal_ms'):
        if not value.get(key):value.pop(key,None)
    return hashlib.sha256(dump(value).encode()).hexdigest()

def snapshot_matches_plan(snapshot,plan):
    if snapshot.get('science_source_hash')==hashlib.sha256(dump(plan).encode()).hexdigest():return True
    previous=snapshot.get('science_atlas')
    return bool(previous) and render_fingerprint(previous)==render_fingerprint(plan)

def readable_color(color,background=None):
    if not color:return CONFIG['text']
    def luminance(value):
        rgb=[int(value[i:i+2],16)/255 for i in (1,3,5)]
        linear=[c/12.92 if c<=.04045 else ((c+.055)/1.055)**2.4 for c in rgb]
        return sum(c*k for c,k in zip(linear,(.2126,.7152,.0722)))
    a,b=sorted((luminance(color),luminance(background or CONFIG['background'])))
    return color if (b+.05)/(a+.05)>=3 else ('#09161A' if luminance(background or CONFIG['background'])>.4 else CONFIG['text'])

def compile_commands(plan):
    from .science_density import normalize_density
    plan=normalize_density(plan)
    if plan.get('presentation')=='visual_first':
        from .science_visual_render import compile_visual
        from .science_slide_reveal import enabled
        if enabled(plan):
            from .science_slide_reveal import compile_slide
            return compile_slide(plan)
        return compile_visual(plan)
    from .domain import DomainError
    from .science_atlas import LAYOUTS
    if (plan.get('version')!=VERSION or plan.get('layout') not in LAYOUTS
            or not isinstance(plan.get('headline'),str) or not 0<len(plan['headline'])<=180
            or len(plan.get('caption',''))>350 or len(plan.get('nodes',[]))>6
            or len(plan.get('illustrations',[]))>2):raise DomainError('Некорректный макет научпопа.')
    if any(not isinstance(n.get('label'),str) or not 0<len(n['label'])<=90 for n in plan.get('nodes',[])):raise DomainError('Некорректная подпись схемы.')
    plan=copy.deepcopy(plan);commands=[]
    def text(value,x,y,width,size=32,weight=400,color=None,cue=None):
        for i,line in enumerate(lines(value,width,size,weight)):
            commands.append({'type':'text','text':line,'x':x,'y':y+size+i*size*1.28,'size':size,'weight':weight,'color':color or CONFIG['text'],'cue_index':cue})
    layout=CONFIG['layouts'][plan['layout']]
    rect=lambda values:[round(values[0]*1920),round(values[1]*1080),round(values[2]*1920),round(values[3]*1080)]
    illustrations=plan.get('illustrations',[])
    if plan['layout']=='comparison':
        for i,key in enumerate(('left','right')):
            x,y,w,h=rect(layout[key]);asset=illustrations[i].get('asset_id') if i<len(illustrations) else None
            if asset:commands.append({'type':'image','asset_id':asset,'rect':[x,y,w,h]})
            text(plan.get('labels',['',''])[i],x,y+h+22,w,30,600)
        tx,ty,tw,th=rect(layout['text'])
    elif plan['layout'] in ('process','factors','text_card','local_board') or (plan['layout']=='instruction' and not illustrations):
        tx,ty,tw,th=(135,75,1650,180)
    else:
        x,y,w,h=rect(layout['image'])
        if illustrations and illustrations[0].get('asset_id'):commands.append({'type':'image','asset_id':illustrations[0]['asset_id'],'rect':[x,y,w,h]})
        tx,ty,tw,th=rect(layout.get('text',layout.get('steps')))
    title=plan['headline'].replace('→',' — ').replace('↔',' / ');title_size=56
    while title_size>28 and len(lines(title,tw,title_size,600))*title_size*1.28>min(th,180):title_size-=2
    commands.append({'type':'line','x1':tx,'y1':ty-20,'x2':tx+64,'y2':ty-20,'color':CONFIG['accent'],'width':3})
    text(title,tx,ty,tw,title_size,600)
    caption_y=ty+len(lines(title,tw,title_size,600))*title_size*1.28+20
    caption_size=32
    caption_limit=min(1030,ty+th) if plan['layout'] in ('hero_caption','image_left','image_right') else 1030
    if plan['layout']=='comparison':caption_limit=rect(layout['left'])[1]-18
    while caption_size>20 and len(lines(plan.get('caption',''),tw,caption_size))*caption_size*1.28>caption_limit-caption_y:caption_size-=2
    text(plan.get('caption',''),tx,caption_y,tw,caption_size,color='#B4C2C3')
    if plan.get('representation')=='analogy':text('АНАЛОГИЯ',135,30,800,25,color=CONFIG['accent'])
    nodes=plan.get('nodes',[])
    if nodes and not illustrations:
        step=1540/max(1,len(nodes)-1);y=510
        for i,node in enumerate(nodes):
            x=190+i*step
            linked=any(e.get('source')==i-1 and e.get('target')==i for e in plan.get('edges',[])) if plan.get('planner_version') else bool(i)
            if i and linked:
                commands.append({'type':'line','x1':x-step+34,'y1':y,'x2':x-38,'y2':y,'color':CONFIG['accent'],'width':4,'cue_index':i})
                commands.append({'type':'line','x1':x-52,'y1':y-9,'x2':x-38,'y2':y,'color':CONFIG['accent'],'width':4,'cue_index':i})
                commands.append({'type':'line','x1':x-52,'y1':y+9,'x2':x-38,'y2':y,'color':CONFIG['accent'],'width':4,'cue_index':i})
            commands.append({'type':'circle','x':x,'y':y,'radius':24,'color':CONFIG['accent'],'cue_index':i})
            width=min(230,step-30) if len(nodes)>1 else 600
            text(node['label'],x-width/2,y+45,width,30,600,cue=i)
    elif nodes:
        caption_bottom=caption_y+len(lines(plan.get('caption',''),tw,32))*32*1.28
        cursor=caption_bottom+40;size=30
        while size>20 and sum(len(lines(f'{i+1}. '+node['label'],tw,size))*size*1.28+16 for i,node in enumerate(nodes))>1010-cursor:size-=2
        for i,node in enumerate(nodes):
            label=f'{i+1}. '+node['label'];text(label,tx,cursor,tw,size,cue=i)
            cursor+=len(lines(label,tw,size))*size*1.28+16
    # Exact local test geometry and typography: never delegated to the image model.
    for i,element in enumerate(plan.get('elements',[])):
        x=135+element['x']*1650;y=320+element['y']*650
        w=element.get('w',0)*1650;h=element.get('h',0)*650
        cue=len(nodes)+i if element.get('source_char_start') is not None else None
        color=element.get('color') or CONFIG['text'];kind=element['kind']
        def rule(x1,y1,x2,y2):commands.append({'type':'line','x1':x1,'y1':y1,'x2':x2,'y2':y2,'color':element.get('color') or CONFIG['accent'],'width':4,'cue_index':cue})
        if kind=='text':
            background=CONFIG['background']
            for backing in plan.get('elements',[]):
                if (backing['kind']=='box' and backing.get('fill') and backing['x']<=element['x']<backing['x']+backing.get('w',0)
                        and backing['y']<=element['y']<backing['y']+backing.get('h',0)):
                    background=backing.get('color') or CONFIG['accent']
            text(element['text'].replace('→',' — ').replace('↔',' / '),x,y,max(100,w or 600),32,600,color=readable_color(color,background),cue=cue)
        elif kind=='dot':commands.append({'type':'circle','x':x,'y':y,'radius':max(5,min(24,w/2 or 10)),'color':color,'cue_index':cue})
        elif kind=='cross':
            r=max(8,min(30,w/2 or 15));rule(x-r,y,x+r,y);rule(x,y-r,x,y+r)
        elif kind=='box':commands.append({'type':'rect','x':x,'y':y,'w':w,'h':h,'color':element.get('color') or CONFIG['accent'],'fill':element.get('fill',False),'width':3,'cue_index':cue})
        elif kind in ('line','arrow'):
            rule(x,y,x+w,y+h)
            if kind=='arrow':
                angle=math.atan2(h,w)
                for offset in (-.55,.55):rule(x+w-18*math.cos(angle+offset),y+h-18*math.sin(angle+offset),x+w,y+h)
        if kind!='text' and element.get('text'):text(element['text'],x,y+h+12,max(160,w),28,600,cue=cue)
    plan['commands']=commands;return plan

class ScienceReader:
    def __init__(self,plan,resolve):
        from .science_motion import playback_commands
        from .science_typography import fit_commands
        from .science_density import normalize_density
        plan=normalize_density(plan)
        self.plan=copy.deepcopy(plan);self.slide=plan.get('presentation_motion') in ('slide_explain','staged_explain','focus_explain','detail_explain');self.compositors={}
        if self.slide:
            from .science_slide_reveal import compile_slide
            self.plan=compile_slide(self.plan)
        self.commands=self.plan.get('commands',[]) if self.slide else fit_commands(playback_commands(plan),font)
        self.resolve=resolve;self.images={};self.frames={}
        duration_ms=(plan.get('illustration_motion') or {}).get('duration_seconds',6)*1000
        for c in self.commands:
            if c.get('typing_cps'):c['typing_cps']=max(c['typing_cps'],(len(c['text'])+c.get('typing_offset',0)+1)/max(.08,(duration_ms-c.get('at_ms',0)-duration_ms/60)/1000))
    def compositor(self,size):
        from .science_gpu import SlideCompositor
        if size not in self.compositors:
            for old in self.compositors.values():old.close()
            self.compositors.clear()
            self.compositors[size]=SlideCompositor(self.plan,self.commands,self.resolve,size)
        return self.compositors[size]
    def gpu_image(self,seconds,size):
        if not self.slide:raise ValueError('Retained GPU images require slide_explain')
        return self.compositor(size).image(seconds)
    def image(self,seconds,size,fill=False,preview=False):
        if self.slide:
            from .science_slide_reveal import duration_ms
            return self.compositor(size).pil_image(duration_ms(self.plan)/1000 if preview else seconds)

        cue=self.plan.get('cue_ms',[])
        visible=tuple(i for i in range(len(self.plan.get('nodes',[]))+len(self.plan.get('elements',[]))+len(self.plan.get('annotations',[]))) if i>=len(cue) or cue[i] is None or seconds*1000>=cue[i])
        if self.plan.get('annotations') and cue:
            offset=len(self.plan.get('nodes',[]))+len(self.plan.get('elements',[]))
            active=[i for i in visible if i>=offset]
            if active:visible=tuple(i for i in visible if i<offset or i==max(active))
        focus=None
        if self.plan.get('presentation')=='visual_first':
            from .science_visual_render import focus_state
            focus=focus_state(self.plan,seconds)
        from .science_motion import image_scale,typed_text,pulse_position,draw_progress
        animated=bool(self.plan.get('illustrations') and self.plan.get('illustration_motion',{}).get('strength')) or any(c.get('typing_cps') or c.get('draw_ms') or c['type']=='pulse' for c in self.commands)
        timed=tuple(seconds*1000>=c.get('at_ms',0) for c in self.commands if c.get('at_ms'))
        key=(size,visible,focus['phase'] if focus else None,timed,round(seconds*24) if animated and not preview else None,preview)
        if key in self.frames:return self.frames[key]
        from .gpu_pixels import CudaImage,paste
        image=Image.new('RGB',size,CONFIG['background']);scale=size[0]/1920
        overlay=Image.new('RGBA',size);draw=ImageDraw.Draw(overlay)
        for cmd in self.commands:
            if not preview and seconds*1000<cmd.get('at_ms',0):continue
            progress=draw_progress(cmd,seconds,preview)
            if progress<=0:continue
            index=cmd.get('cue_index')
            if index is not None and index not in visible:continue
            if cmd['type']=='image':
                asset=cmd['asset_id']
                if asset not in self.images:
                    with Image.open(self.resolve(asset)) as src:self.images[asset]=src.convert('RGB')
                x,y,w,h=[round(v*scale) for v in cmd['rect']]
                source=self.images[asset]
                if cmd.get('crop'):
                    box=tuple(v*(source.width if i%2==0 else source.height) for i,v in enumerate(cmd['crop']))
                    paste(image,CudaImage(source).resize((w,h),box),(x,y));continue
                k=min(w/source.width,h/source.height)
                iw,ih=max(1,round(source.width*k)),max(1,round(source.height*k))
                factor=1 if preview else image_scale(self.plan,cmd,seconds)
                iw,ih=max(1,round(iw*factor)),max(1,round(ih*factor))
                paste(image,CudaImage(source).resize((iw,ih)),(x+(w-iw)//2,y+(h-ih)//2))
            elif cmd['type']=='text':
                draw.text((cmd['x']*scale,cmd['y']*scale),typed_text(cmd,seconds,preview),font=font(max(1,round(cmd['size']*scale)),cmd['weight']),fill=cmd['color'],anchor='ls')
            elif cmd['type']=='line':draw.line((cmd['x1']*scale,cmd['y1']*scale,(cmd['x1']+(cmd['x2']-cmd['x1'])*progress)*scale,(cmd['y1']+(cmd['y2']-cmd['y1'])*progress)*scale),fill=cmd['color'],width=max(1,round(cmd['width']*scale)))
            elif cmd['type']=='rect':
                draw.rectangle((cmd['x']*scale,cmd['y']*scale,(cmd['x']+cmd['w'])*scale,(cmd['y']+cmd['h'])*scale),outline=cmd['color'],fill=cmd['color'] if cmd.get('fill') else None,width=max(1,round(cmd['width']*scale)))
            elif cmd['type']=='ring':
                if focus and cmd.get('cue_index')==focus['index']:cmd={**cmd,'x':focus['x'],'y':focus['y']}
                x,y,r=[cmd[k]*scale for k in ('x','y','radius')];draw.arc((x-r,y-r,x+r,y+r),start=-90,end=-90+360*progress,fill=cmd['color'],width=max(1,round(cmd['width']*scale)))
            elif cmd['type']=='pulse':
                px,py=pulse_position(cmd,seconds);r=cmd.get('radius',6)*scale;px*=scale;py*=scale
                draw.ellipse((px-r,py-r,px+r,py+r),fill=cmd['color'])
            elif cmd['type']=='circle':
                x,y,r=[cmd[k]*scale for k in ('x','y','radius')];r*=progress;draw.ellipse((x-r,y-r,x+r,y+r),fill=cmd['color'])
        paste(image,overlay,(0,0),overlay)
        self.frames[key]=image
        while len(self.frames)>8:self.frames.pop(next(iter(self.frames)))
        return image
    def close(self):
        for compositor in self.compositors.values():compositor.close()
        self.compositors.clear()
        for image in self.images.values():image.close()
        self.images.clear();self.frames.clear()

def render_video(plan,resolve,output,duration=6,fps=24,target_size=(1920,1080)):
    import av
    from .gpu_pixels import video_frame
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    plan=copy.deepcopy(plan)
    if plan.get('presentation_motion') in ('slide_explain','staged_explain','focus_explain','detail_explain') or plan.get('frame_density')=='filled':plan.setdefault('illustration_motion',{})['duration_seconds']=duration
    elif plan.get('illustration_motion'):plan['illustration_motion']['duration_seconds']=duration
    reader=ScienceReader(plan,resolve)
    try:
        with av.open(str(output),'w',format='mp4') as container:
            stream=container.add_stream('h264_nvenc',rate=fps);stream.width,stream.height=target_size;stream.pix_fmt='yuv420p'
            stream.options={'preset':'p4','cq':'20','rc':'vbr'}
            total=max(1,round(duration*fps))
            for i in range(total):
                for packet in stream.encode(video_frame(reader.gpu_image(i/fps,target_size) if reader.slide else reader.image(i/fps,target_size))):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        stats=dict(reader.compositor(target_size).stats) if reader.slide else {}
        if reader.slide:stats['yuv_download_bytes']=total*target_size[0]*target_size[1]*3//2
        return {'gpu_stats':stats,'host_transfer':'one yuv420p download per frame to PyAV/NVENC' if reader.slide else 'legacy PIL bridge','path':str(output),'duration':duration,'fps':fps,'frames':total,'renderer_version':(6 if reader.plan.get('presentation_motion')=='slide_explain' else 7) if reader.slide else 4,'encoder':'h264_nvenc','compute_device':'cuda','safe_crop':{},'reason':'Научпоп: макет и подписи, без деформации иллюстрации'}
    finally:reader.close()

def assemble_project(service,pid,scene_ids=None):
    """Publish derived scene cards only after all their illustration assets exist."""
    from .science_atlas import asset_name
    if service.project(pid).get('visual_format')!='science_atlas':return {'assembled':0,'waiting':0}
    result={'assembled':0,'waiting':0}
    with _LOCK:
        for row in service.db.all('SELECT id FROM scenes WHERE project_id=? ORDER BY position',(pid,)):
            if scene_ids is not None and row['id'] not in scene_ids:continue
            scene=service.scene(row['id'])
            if not scene['data'].get('science_atlas'):continue
            plan=hydrate(service,scene)
            if any(not item['asset_id'] for item in plan['illustrations']):result['waiting']+=1;continue
            key=hashlib.sha256(dump(plan).encode()).hexdigest()
            active=service.db.one('SELECT snapshot FROM generations WHERE id=?',(scene.get('active_generation_id'),))
            if active and snapshot_matches_plan(json.loads(active['snapshot']),plan):continue
            reader=ScienceReader(plan,lambda aid:service.root/service.asset(aid)['path'])
            try:
                image=reader.image((plan.get('support_reveal_ms') or 0)/1000,(1920,1080),preview=True);buffer=io.BytesIO();image.save(buffer,format='PNG');data=buffer.getvalue()
            finally:reader.close()
            aid,gid=uid(),uid();relative=Path('assets')/pid/(aid+'.png');path=service.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            snapshot={'scene':scene['data'],'science_source_hash':key,'science_auto':True,'science_atlas':plan,'profile':service.profile(),'project_id':pid}
            with service.db.transaction() as c:
                if c.execute('SELECT current_revision FROM scenes WHERE id=?',(scene['id'],)).fetchone()[0]!=scene['current_revision']:
                    path.unlink(missing_ok=True);continue
                c.execute("INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,scene_id,scene_revision,asset_id,created_at,started_at,finished_at,progress) VALUES(?,?,?, ?,0,'succeeded',?,?,?,?,?,?,?)",(gid,pid,'scene',dump(snapshot),scene['id'],scene['current_revision'],aid,now(),now(),now(),dump({'stage':'Научпоп: сцена собрана локально','percent':100})))
                c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,pid,gid,str(relative),hashlib.sha256(data).hexdigest(),1920,1080,len(data)))
                c.execute('UPDATE scenes SET active_generation_id=?,video_asset_id=NULL WHERE id=?',(gid,scene['id']))
                service.db.event(c,pid,'generation_completed',{'id':gid,'state':'succeeded'})
            result['assembled']+=1
    return result
