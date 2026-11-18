"""Checksummed video checkpoints; audio is encoded once on the final global clock."""
import hashlib,json,math
from fractions import Fraction
from functools import lru_cache
import re
from pathlib import Path
import av
from .editing import normalize
from .export_worker import RATE,AudioReader,mix_audio_block

VERSION=1

def digest(path):
    with path.open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()

def source_ids(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ('asset_id','source_image_asset_id','source_video_asset_id','source_asset_id') and isinstance(item,str) and item:yield item
            else:yield from source_ids(item)
    elif isinstance(value,list):
        for item in value:yield from source_ids(item)

@lru_cache(maxsize=1)
def implementation_key():
    from .release_paths import install_root
    root=install_root();value=hashlib.sha256()
    for name in ('release.json','imagegen/export_worker.py','imagegen/gpu_pixels.py','imagegen/science_gpu.py','imagegen/science_script_gpu.py','imagegen/export_checkpoint.py'):
        path=root/name
        if path.is_file():value.update(name.encode());value.update(path.read_bytes())
    return value.hexdigest()

def signature(service,document,size):
    assets=[]
    for aid in sorted(set(source_ids(document))):
        asset=service.asset(aid);path=service.root/asset['path'];stat=path.stat()
        assets.append({'id':aid,'sha256':asset.get('sha256'),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns})
    return hashlib.sha256(json.dumps({'version':VERSION,'implementation':implementation_key(),'document':document,'size':size,'assets':assets},sort_keys=True).encode()).hexdigest()

def save(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,sort_keys=True),encoding='utf-8');temporary.replace(path)

def mux(service,document,parts,output,size,cancelled,progress):
    timeline=normalize(document);fps=document.get('export_settings',{}).get('fps',24)
    samples=math.ceil(timeline['duration_ms']*RATE/1000);audio_cursor=0;audios={}
    use_audio=document.get('source_audio') or any(a.get('enabled',True) for a in document['audio_tracks'])
    paths={}
    def path(aid):
        if aid not in paths:paths[aid]=service.root/service.asset(aid)['path']
        return paths[aid]
    try:
        with av.open(str(parts[0]['path'])) as first, av.open(str(output),'w',format='mp4',options={'movflags':'+faststart'}) as out:
            video=out.add_stream_from_template(first.streams.video[0])
            audio=out.add_stream('aac',rate=RATE) if use_audio else None
            if audio:audio.layout='stereo';audio.bit_rate=192000
            def emit_audio(until):
                nonlocal audio_cursor
                if not audio:return
                while audio_cursor<min(samples,until):
                    if cancelled():raise InterruptedError('Экспорт отменён.')
                    count=min(2048,min(samples,until)-audio_cursor)
                    block=mix_audio_block(timeline,document,audio_cursor,count,audios,path)
                    frame=av.AudioFrame.from_ndarray(block,format='fltp',layout='stereo');frame.sample_rate=RATE;frame.pts=audio_cursor;frame.time_base=Fraction(1,RATE)
                    for packet in audio.encode(frame):out.mux(packet)
                    audio_cursor+=count
            for i,part in enumerate(parts):
                with av.open(str(part['path'])) as source:
                    for packet in source.demux(video=0):
                        if packet.dts is None:continue
                        if cancelled():raise InterruptedError('Экспорт отменён.')
                        offset=Fraction(part['first_frame'],fps)/packet.time_base
                        if offset.denominator!=1:raise ValueError('Несовместимая шкала времени контрольной части.')
                        packet.pts+=int(offset);packet.dts+=int(offset)
                        emit_audio(min(samples,int((packet.pts*packet.time_base+Fraction(1,fps))*RATE)))
                        packet.stream=video;out.mux(packet)
                progress(95+(i+1)/len(parts)*3,'Соединение готовых частей и озвучки')
            emit_audio(samples)
            if audio:
                for packet in audio.encode():out.mux(packet)
        with av.open(str(output)) as result:
            if not result.streams.video or (use_audio and not result.streams.audio):raise ValueError('Неполный итоговый файл.')
    finally:
        for reader in audios.values():reader.close()

def render_resumable(service,document,output,progress,cancelled,size=(1280,720),job_id=None,chunk_seconds=30):
    from .export_worker import render
    output=Path(output);fps=document.get('export_settings',{}).get('fps',24);frames=math.ceil(normalize(document)['duration_ms']*fps/1000)
    key=signature(service,document,size);job_id=job_id or output.stem
    directory=output.parent/'export-parts'/job_id/key;directory.mkdir(parents=True,exist_ok=True);checkpoint=directory/'checkpoint.json'
    try:metadata=json.loads(checkpoint.read_text(encoding='utf-8'))
    except (OSError,ValueError):metadata={}
    if metadata.get('signature')!=key:metadata={'signature':key,'parts':{},'fps':fps,'frames':frames,'size':list(size)}
    parts=[];length=max(1,round(chunk_seconds*fps))
    for first in range(0,frames,length):
        if cancelled():raise InterruptedError('Экспорт отменён.')
        last=min(frames,first+length);name=f'{first:012d}-{last:012d}.mp4';target=directory/name;record=metadata['parts'].get(name)
        valid=bool(record and record.get('frames')==last-first and target.is_file() and record.get('sha256')==digest(target))
        if not valid:
            temporary=target.with_suffix('.partial.mp4')
            try:
                render(service,document,temporary,lambda value,stage:progress(min(last/max(1,frames)*94,max(first/max(1,frames)*94,value/95*94)),stage),cancelled,size=size,frame_range=(first,last),include_audio=False)
                with av.open(str(temporary)) as video:
                    count=sum(1 for _ in video.decode(video=0))
                    if count!=last-first:raise ValueError('Контрольная часть содержит неверное число кадров.')
                temporary.replace(target);metadata['parts'][name]={'sha256':digest(target),'frames':last-first};save(checkpoint,metadata)
            finally:temporary.unlink(missing_ok=True)
        parts.append({'path':target,'first_frame':first,'last_frame':last})
        progress(last/max(1,frames)*94,'Готовые части сохранены')
    if cancelled():raise InterruptedError('Экспорт отменён.')
    mux(service,document,parts,output,size,cancelled,progress)
    return frames/fps


def discard_completed(output,job_id):
    """Delete only our completed job's generated shards, never arbitrary contents."""
    output=Path(output).resolve();base=(output.parent/'export-parts').resolve();directory=(base/job_id).resolve()
    if not directory.is_relative_to(base) or directory==base:return
    if not directory.is_dir():return
    for child in directory.iterdir():
        if not re.fullmatch(r'[0-9a-f]{64}',child.name) or not child.is_dir() or child.resolve().parent!=directory:continue
        for file in child.iterdir():
            if file.resolve().parent!=child.resolve() or not file.is_file():continue
            if file.name in ('checkpoint.json','checkpoint.tmp') or re.fullmatch(r'\d{12}-\d{12}(?:\.partial)?\.mp4',file.name):file.unlink()
        try:child.rmdir()
        except OSError:pass
    try:directory.rmdir()
    except OSError:pass
