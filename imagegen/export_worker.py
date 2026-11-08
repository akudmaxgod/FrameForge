"""Bounded-memory H.264/AAC exports using the application's FFmpeg libraries."""
import hashlib
import json
import math
import threading
import time
from fractions import Fraction
from pathlib import Path
try:
    import av
    import numpy as np
except ImportError:
    av = None
from PIL import Image, ImageOps, ImageDraw, ImageFont
from .db import uid, now, dump
from .domain import DomainError
from .editing import normalize, number

RATE = 48000

def illustrative_label(image):
    """Identify generic footage without implying it depicts the actual site."""
    scale=image.width/1280
    try:
        font=ImageFont.truetype('arial.ttf',max(10,round(24*scale)))
    except OSError:
        font=ImageFont.truetype('DejaVuSans.ttf',max(10,round(24*scale)))
    draw=ImageDraw.Draw(image)
    draw.rectangle(tuple(round(v*scale) for v in (18,668,336,704)),fill='#16202b')
    draw.text((round(27*scale),round(672*scale)),'Иллюстративный план',font=font,fill='#edf4ff')
    return image


def opacity(c, time_ms):
    local = time_ms - c['start_ms']
    if c.get('in_kind') == 'dissolve' and c['in_ms'] and local < c['in_ms']:
        return max(0, local / c['in_ms'])
    if c.get('in_kind') == 'black' and c['in_ms'] and local < c['in_ms']/2:
        return max(0, local / (c['in_ms']/2))
    if c['out_kind'] == 'black' and c['out_ms'] and c['duration_ms']-local < c['out_ms']/2:
        return max(0, (c['duration_ms']-local)/(c['out_ms']/2))
    return 1


def white_fade(c,time_ms):
    local=time_ms-c['start_ms']
    if c.get('in_kind')=='white' and c['in_ms'] and local<c['in_ms']/2:
        return max(0,min(1,1-local/(c['in_ms']/2)))
    if c.get('out_kind')=='white' and c['out_ms'] and c['duration_ms']-local<c['out_ms']/2:
        return max(0,min(1,1-(c['duration_ms']-local)/(c['out_ms']/2)))
    return 0


class VideoReader:
    def __init__(self, path, seconds):
        self.container = av.open(str(path))
        stream = self.container.streams.video[0]
        self.origin = float((stream.start_time or 0) * stream.time_base)
        self.container.seek(int((seconds+self.origin)/stream.time_base), stream=stream, backward=True)
        self.frames = iter(self.container.decode(stream))
        self.previous = None
        self.next = next(self.frames, None)

    def image(self, seconds, size, fill=False):
        while self.next is not None and float(self.next.time or 0)-self.origin <= seconds+1e-6:
            self.previous, self.next = self.next, next(self.frames, None)
        frame = self.previous or self.next
        if frame is None:
            raise ValueError('Не удалось декодировать видеоклип.')
        source = frame.to_image()
        if source.size == size:
            return source
        if fill:
            return ImageOps.fit(source, size, Image.Resampling.BICUBIC)
        image = ImageOps.contain(source, size, Image.Resampling.BICUBIC)
        canvas = Image.new('RGB',size,'black')
        canvas.paste(image,((size[0]-image.width)//2,(size[1]-image.height)//2))
        return canvas

    def close(self):
        self.container.close()


class AudioReader:
    def __init__(self, path, seconds):
        self.container = av.open(str(path))
        streams = self.container.streams.audio
        self.segment = None
        self.frames = iter(())
        if streams:
            stream = streams[0]
            origin = float((stream.start_time or 0)*stream.time_base)
            self.container.seek(int((seconds+origin)/stream.time_base),stream=stream,backward=True)
            self.frames = self.segments(stream, origin)
            self.segment = next(self.frames, None)

    def segments(self, stream, origin):
        resampler = av.AudioResampler(format='fltp', layout='stereo', rate=RATE)
        cursor = 0
        for frame in self.container.decode(stream):
            for converted in resampler.resample(frame):
                start = round((float(converted.time)-origin)*RATE) if converted.time is not None else cursor
                array = converted.to_ndarray()
                cursor = start + array.shape[1]
                yield start, array
        for converted in resampler.resample(None):
            yield cursor, converted.to_ndarray()
            cursor += converted.samples

    def read(self, seconds, count):
        start = round(seconds*RATE)
        result = np.zeros((2,count),dtype=np.float32)
        while self.segment is not None:
            pos, array = self.segment
            end = pos+array.shape[1]
            if pos >= start+count:
                break
            lo, hi = max(pos,start), min(end,start+count)
            if hi > lo:
                result[:,lo-start:hi-start] = array[:,lo-pos:hi-pos]
            if end <= start+count:
                self.segment = next(self.frames,None)
            else:
                break
        return result

    def close(self):
        self.container.close()


def render(service, document, output, progress, cancelled, size=(1280,720)):
    timeline = normalize(document)
    duration = timeline['duration_ms']
    frames = math.ceil(duration*24/1000)
    videos, audios = {}, {}
    use_audio = document.get('source_audio') or any(a.get('enabled',True) for a in document['audio_tracks'])
    paths = {}
    def path(aid):
        if aid not in paths:
            paths[aid] = service.root/service.asset(aid)['path']
        return paths[aid]
    try:
        with av.open(str(output),'w',format='mp4',options={'movflags':'+faststart'}) as out:
            video = out.add_stream('libx264',rate=24)
            video.width,video.height=size
            video.pix_fmt='yuv420p'
            video.options={'crf':'20','preset':'veryfast'}
            audio = out.add_stream('aac',rate=RATE) if use_audio else None
            if audio:
                audio.layout='stereo'
                audio.bit_rate=192000
            black = Image.new('RGB', size, 'black')
            white = Image.new('RGB', size, 'white')
            clips = timeline['clips']
            active = []
            next_clip = 0
            for index in range(frames):
                if cancelled():
                    raise InterruptedError('Экспорт отменён.')
                time_ms=index*1000/24
                active = [c for c in active if time_ms < c['end_ms']]
                while next_clip < len(clips) and clips[next_clip]['start_ms'] <= time_ms:
                    clip = clips[next_clip]
                    if time_ms < clip['end_ms']:
                        active.append(clip)
                    next_clip += 1
                active_ids={c['edit_clip_id'] for c in active}
                for cid in list(videos):
                    if cid not in active_ids:
                        videos.pop(cid).close()
                canvas = black
                for c in active:
                    cid=c['edit_clip_id']
                    seconds=(c['trim_in_ms']+(time_ms-c['start_ms'])*c.get('playback_rate',1))/1000
                    if cid not in videos:
                        if c.get('media_type')=='image':
                            if c.get('foreground_effect'):
                                from .montage_foreground import ForegroundReader
                                videos[cid] = ForegroundReader(path(c['source_image_asset_id']), c, path)
                            else:
                                from .montage_media import StillReader
                                videos[cid] = StillReader(path(c['source_image_asset_id']), c)
                        else:videos[cid]=VideoReader(path(c['source_video_asset_id']),seconds)
                    if c.get('media_type')!='image':seconds=min(seconds,max(c['trim_in_ms']/1000,c['trim_out_ms']/1000-.001))
                    image=videos[cid].image(seconds,size,fill=c.get('framing')=='fill')
                    fade=white_fade(c,time_ms)
                    if fade:image=Image.blend(image,white,fade)
                    if c.get('illustrative'):
                        image=illustrative_label(image.copy())
                    alpha = opacity(c, time_ms)
                    if alpha >= 1:
                        canvas = image
                    elif alpha > 0:
                        canvas = Image.blend(canvas, image, alpha)
                frame=av.VideoFrame.from_image(canvas)
                frame.pts=index
                frame.time_base=Fraction(1,24)
                for packet in video.encode(frame):
                    out.mux(packet)
                if audio:
                    sample_start=index*2000
                    count=2000
                    samples=np.zeros((2,count),dtype=np.float32)
                    clock=(sample_start+np.arange(count))/RATE*1000
                    sources=[]
                    for a in timeline['audio_tracks']:
                        if a.get('enabled',True):
                            sources.append((a['role'],a['asset_id'],a['offset_ms'],a['trim_in_ms'],a['trim_out_ms']-a['trim_in_ms'],a.get('volume',1),a.get('fade_in_ms',0),a.get('fade_out_ms',0),None))
                    if document.get('source_audio'):
                        for c in timeline['clips']:
                            if c.get('media_type')!='image' and not c.get('muted',True) and c['start_ms']<clock[-1]+1 and c['end_ms']>clock[0]:
                                sources.append(('clip:'+c['edit_clip_id'],c['source_video_asset_id'],c['start_ms'],c['trim_in_ms'],min(c['duration_ms'],(c['trim_out_ms']-c['trim_in_ms'])/c.get('playback_rate',1)),c.get('volume',1),0,0,c))
                    audio_ids=set()
                    for key,aid,start,trim,length,volume,fade_in,fade_out,clip in sources:
                        mask=(clock>=start)&(clock<start+length)
                        positions=np.flatnonzero(mask)
                        if not len(positions):
                            continue
                        audio_ids.add(key)
                        lo,hi=int(positions[0]),int(positions[-1])+1
                        seconds=(trim+clock[lo]-start)/1000
                        if key not in audios:
                            audios[key]=AudioReader(path(aid),seconds)
                        local=clock[lo:hi]-start
                        gain=np.full(hi-lo,volume,dtype=np.float32)
                        if fade_in:
                            gain*=np.minimum(1,local/fade_in)
                        if fade_out:
                            gain*=np.minimum(1,(length-local)/fade_out)
                        if clip:
                            gain*=np.array([opacity(clip,t) for t in clock[lo:hi]],dtype=np.float32)
                            if clip['out_kind']=='dissolve' and clip['out_ms']:
                                gain*=np.minimum(1,(clip['end_ms']-clock[lo:hi])/clip['out_ms'])
                        samples[:,lo:hi]+=audios[key].read(seconds,hi-lo)*gain
                    for key in list(audios):
                        if key not in audio_ids:
                            audios.pop(key).close()
                    np.clip(samples,-1,1,out=samples)
                    af=av.AudioFrame.from_ndarray(samples,format='fltp',layout='stereo')
                    af.sample_rate=RATE
                    af.pts=sample_start
                    af.time_base=Fraction(1,RATE)
                    for packet in audio.encode(af):
                        out.mux(packet)
                if index%12==0:
                    progress(index/frames*95,'Сборка видео и звука')
            for stream in (video,audio):
                if stream:
                    for packet in stream.encode():
                        out.mux(packet)
        progress(98,'Проверка файла')
        with av.open(str(output)) as check:
            if not check.streams.video or (use_audio and not check.streams.audio):
                raise ValueError('В итоговом файле отсутствует дорожка.')
            if check.streams.video[0].codec_context.name!='h264' or (use_audio and check.streams.audio[0].codec_context.name!='aac'):
                raise ValueError('Некорректный кодек результата.')
        return frames/24
    finally:
        for reader in [*videos.values(),*audios.values()]:
            reader.close()


class ExportWorker:
    def __init__(self, service):
        self.service,self.db=service,service.db
        # Called once at application startup, never per HTTP request.
        with self.db.transaction() as conn:
            conn.execute("UPDATE edit_exports SET status='interrupted', stage='Прервано перезапуском', updated_at=? WHERE status IN ('queued','running')",(now(),))
        self.lock=threading.Lock()
        self.slot=threading.Semaphore(1)

    def get(self, project_id, job_id):
        row=self.db.one('SELECT * FROM edit_exports WHERE id=? AND project_id=?',(job_id,project_id))
        if not row:
            raise DomainError('Экспорт не найден.',404)
        settings=json.loads(row.pop('snapshot')).get('export_settings',{'width':1280,'height':720})
        dimensions=self.db.one('SELECT width,height FROM assets WHERE id=?',(row['asset_id'],)) if row['asset_id'] else None
        row['width'],row['height']=(dimensions or settings)['width'],(dimensions or settings)['height']
        row['url']=f"/api/assets/{row['asset_id']}?download=1" if row['asset_id'] else None
        return row

    def start(self, project_id, data):
        edit=self.service.editing.get(project_id)
        if data.get('expected_revision')!=edit['revision']:
            raise DomainError('Сначала сохраните текущую версию монтажа.',409)
        doc=self.service.editing.validate(project_id,edit['document'])
        if not normalize(doc)['clips']:
            raise DomainError('Добавьте готовые клипы в монтаж.')
        video_end = normalize(doc)['duration_ms']
        voice_end = max((a.get('offset_ms', 0)+a['trim_out_ms']-a['trim_in_ms'] for a in doc['audio_tracks']
                         if a['role'] == 'voice' and a.get('enabled', True)), default=0)
        if voice_end-video_end > 1000/24:
            raise DomainError('Видеоряд заканчивается раньше озвучки. Дополните сцены или исправьте монтаж перед экспортом.', 409)
        for c in doc['clips']:
            state=edit['sources'][c['edit_clip_id']]
            if not c.get('excluded') and (state['missing'] or state['stale']):
                scene=self.db.one('SELECT position FROM scenes WHERE id=?',(c['scene_id'],))
                raise DomainError(f"Обновите источник сцены {(scene or {}).get('position','?')}: медиа отсутствует или устарело.",409)
        # Capability check does not install anything on export.
        try:
            av.Codec('libx264','w');av.Codec('aac','w')
        except Exception as error:
            raise DomainError('Установите зависимости FrameForge (PyAV с FFmpeg H.264/AAC).',503) from error
        with self.lock, self.db.transaction() as conn:
            row=conn.execute("SELECT id FROM edit_exports WHERE project_id=? AND edit_revision=? AND status IN ('queued','running')",(project_id,edit['revision'])).fetchone()
            if row:
                job=row['id']
            else:
                job=uid()
                conn.execute('INSERT INTO edit_exports(id,project_id,edit_revision,snapshot,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(job,project_id,edit['revision'],dump(doc),'queued','В очереди',now(),now()))
        if not row:
            threading.Thread(target=self.run,args=(job,),daemon=True).start()
        return self.get(project_id,job)

    def cancel(self, project_id, job_id):
        self.get(project_id,job_id)
        with self.db.transaction() as conn:
            conn.execute("UPDATE edit_exports SET cancel_requested=1,stage=CASE WHEN status='queued' THEN 'Отменено' ELSE 'Отмена…' END,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,updated_at=? WHERE id=? AND status IN ('queued','running')",(now(),job_id))
        return self.get(project_id,job_id)

    def run(self, job_id):
        row=self.db.one('SELECT * FROM edit_exports WHERE id=?',(job_id,))
        directory=self.service.root/'assets'/row['project_id']
        directory.mkdir(parents=True,exist_ok=True)
        temporary=directory/(job_id+'.partial.mp4')
        final=directory/(job_id+'.mp4')
        next_cancel_check = 0.0
        cancel_requested = False
        def cancelled(force=False):
            nonlocal next_cancel_check, cancel_requested
            if cancel_requested:
                return True
            current_time = time.monotonic()
            if force or current_time >= next_cancel_check:
                cancel_requested = bool(self.db.one(
                    'SELECT cancel_requested FROM edit_exports WHERE id=?', (job_id,))['cancel_requested'])
                next_cancel_check = current_time + 0.2
            return cancel_requested
        def progress(percent,stage):
            with self.db.transaction() as conn:
                conn.execute('UPDATE edit_exports SET progress=?,stage=?,updated_at=? WHERE id=?',(percent,stage,now(),job_id))
        try:
            with self.slot:
                if cancelled(force=True):
                    raise InterruptedError()
                with self.db.transaction() as conn:
                    conn.execute("UPDATE edit_exports SET status='running' WHERE id=?",(job_id,))
                document=json.loads(row['snapshot'])
                settings=document['export_settings']
                size=(settings['width'],settings['height'])
                render(self.service,document,temporary,progress,cancelled,size=size)
                if cancelled(force=True):
                    raise InterruptedError()
                digest=hashlib.sha256()
                with temporary.open('rb') as source:
                    for chunk in iter(lambda:source.read(1024*1024),b''):
                        digest.update(chunk)
                aid,gid=uid(),uid()
                with self.db.transaction() as conn:
                    if conn.execute('SELECT cancel_requested FROM edit_exports WHERE id=?',(job_id,)).fetchone()[0]:
                        raise InterruptedError()
                    temporary.replace(final)
                    conn.execute("INSERT INTO generations(id,project_id,purpose,state,snapshot,seed,asset_id,created_at) VALUES(?,?,'edit_export','succeeded',?,0,?,?)",(gid,row['project_id'],row['snapshot'],aid,now()))
                    conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(aid,row['project_id'],gid,str(final.relative_to(self.service.root)),digest.hexdigest(),size[0],size[1],final.stat().st_size))
                    conn.execute("UPDATE edit_exports SET status='completed',progress=100,stage='Готово',asset_id=?,updated_at=? WHERE id=?",(aid,now(),job_id))
        except Exception as error:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            with self.db.transaction() as conn:
                conn.execute('UPDATE edit_exports SET status=?,error=?,stage=?,updated_at=? WHERE id=?',('cancelled' if isinstance(error,InterruptedError) else 'failed',str(error),'Отменено' if isinstance(error,InterruptedError) else 'Ошибка',now(),job_id))
