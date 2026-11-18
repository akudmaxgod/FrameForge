"""Checks before queuing a long export; no installs and no user-file mutations."""
import io,shutil
from .domain import DomainError
from .editing import normalize

def gpu_encoder(width,height,fps):
    from .gpu_pixels import cuda
    import av
    cp=cuda();cp.zeros((1,),dtype=cp.uint8);cp.cuda.get_current_stream().synchronize()
    with av.open(io.BytesIO(),'w',format='mp4') as container:
        video=container.add_stream('h264_nvenc',rate=fps)
        video.width=width;video.height=height;video.pix_fmt='yuv420p';video.codec_context.open()
    return True

def require_ready(service,document):
    settings=document.get('export_settings') or {'width':1920,'height':1080,'fps':24}
    duration=normalize(document)['duration_ms']/1000
    # Reserve final output + resumable parts + filesystem/container overhead.
    estimate=int(duration*(settings['width']*settings['height']*settings.get('fps',24)*.12/8+24000))
    required=2*estimate+256*1024*1024;free=shutil.disk_usage(service.root).free
    if free<required:
        error=DomainError('Недостаточно места для сборки видео. Освободите место перед экспортом.',409)
        error.details={'code':'INSUFFICIENT_DISK_SPACE','required_bytes':required,'free_bytes':free};raise error
    try:gpu_encoder(settings['width'],settings['height'],settings.get('fps',24))
    except Exception as cause:
        error=DomainError('CUDA/NVENC недоступны. Проверьте NVIDIA GPU и драйвер перед сборкой видео.',503)
        error.details={'code':'GPU_ENCODER_UNAVAILABLE'};raise error from cause
    return {'required_bytes':required,'free_bytes':free,'gpu_encoder_ready':True}
