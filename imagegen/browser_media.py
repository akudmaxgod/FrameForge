"""Content-addressed browser playback copies; original assets stay untouched."""
import hashlib
import threading
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import av

_guard=threading.Lock()
_locks={}

@lru_cache(maxsize=2048)
def _info(filename,size,modified):
    with av.open(filename) as media:
        video=media.streams.video[0]
        context=video.codec_context
        audio=media.streams.audio
        return {'compatible':context.name=='h264' and context.pix_fmt=='yuv420p' and all(s.codec_context.name=='aac' for s in audio),
                'width':video.width,'height':video.height,'audio':bool(audio),
                'duration_ms':float(video.duration*video.time_base)*1000 if video.duration is not None else (media.duration or 0)/av.time_base*1000}

def playable_path(source,cache_root):
    source=Path(source).resolve();stat=source.stat()
    info=_info(str(source),stat.st_size,stat.st_mtime_ns)
    if info['compatible'] and source.suffix.lower()=='.mp4':return source
    key=hashlib.sha256(f'browser-media/1:{source}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()
    target=Path(cache_root)/f'{key}.mp4'
    if target.is_file():return target
    with _guard:lock=_locks.setdefault(key,threading.Lock())
    with lock:
        if target.is_file():return target
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=target.with_name(key+'.'+str(uuid4())+'.tmp.mp4')
        try:
            if info['compatible']:
                # H.264 inside MOV needs a browser-friendly container and MIME.
                with av.open(str(source)) as src, av.open(str(temporary),'w',format='mp4',options={'movflags':'+faststart'}) as dst:
                    streams={s.index:dst.add_stream_from_template(s) for s in src.streams if s.type in ('video','audio')}
                    for packet in src.demux():
                        if packet.dts is None or packet.stream.index not in streams:continue
                        packet.stream=streams[packet.stream.index];dst.mux(packet)
            else:
                from .export_worker import render
                adapter=SimpleNamespace(root=source.parent,asset=lambda _:{'path':source.name})
                scale=min(1,1920/max(info['width'],info['height']))
                dimensions=tuple(max(2,int(info[k]*scale)//2*2) for k in ('width','height'))
                doc={'clips':[{'edit_clip_id':'preview','source_video_asset_id':'source','trim_in_ms':0,'trim_out_ms':info['duration_ms'],'muted':not info['audio'],'volume':1}],
                     'transitions':[],'audio_tracks':[],'source_audio':info['audio']}
                render(adapter,doc,temporary,lambda *_:None,lambda:False,size=dimensions)
            with av.open(str(temporary)) as check:
                if check.streams.video[0].codec_context.name!='h264':raise ValueError('Не удалось подготовить видео для браузера.')
            temporary.replace(target)
        finally:temporary.unlink(missing_ok=True)
        return target
