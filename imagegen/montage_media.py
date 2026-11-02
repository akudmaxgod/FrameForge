"""Small shared camera contract for montage stills and duration-aware 2.5D."""
import math
from PIL import Image, ImageOps
from .domain import DomainError


def camera_plan(width, height, preset):
    from .ken_burns import calculate_safe_crop, KEN_BURNS_PRESETS
    if preset not in KEN_BURNS_PRESETS:
        raise DomainError('Неизвестное движение камеры 2.5D.')
    overscan=0 if preset.startswith('gentle_') else .15
    safe=calculate_safe_crop(width,height,preset,overscan=overscan)
    return {'preset':preset,'actual_preset':safe['actual_preset'],'overscan':overscan,
            **KEN_BURNS_PRESETS[safe['actual_preset']]}


def camera_box(width,height,plan,fraction):
    t=max(0,min(1,fraction))
    scale=(1+plan['overscan'])*(plan['scale_start']+t*(plan['scale_end']-plan['scale_start']))
    cw,ch=width/scale,height/scale
    px=plan['pan_x_start']+t*(plan['pan_x_end']-plan['pan_x_start'])
    py=plan['pan_y_start']+t*(plan['pan_y_end']-plan['pan_y_start'])
    x=max(0,min(width-cw,width/2+px*width/2-cw/2))
    y=max(0,min(height-ch,height/2+py*height/2-ch/2))
    return x,y,cw,ch


class StillReader:
    def __init__(self,path,clip):
        with Image.open(path) as image:self.source=image.convert('RGB')
        self.clip=clip

    def image(self,seconds,size,fill=False):
        plan=self.clip.get('camera_motion')
        if plan:
            total_ms=self.clip.get('camera_motion_total_ms') or self.clip['base_duration_ms']
            duration=(total_ms-self.clip.get('hold_last_ms',0))/1000
            # Same last-frame phase as the application's streamed 2.5D path.
            phase=seconds/max(1/24,duration-1/24)
            x,y,w,h=camera_box(*self.source.size,plan,phase)
            return self.source.transform(size,Image.Transform.AFFINE,
                (w/size[0],0,x,0,h/size[1],y),resample=Image.Resampling.BICUBIC)
        if fill:return ImageOps.fit(self.source,size,Image.Resampling.BICUBIC)
        image=ImageOps.contain(self.source,size,Image.Resampling.BICUBIC)
        canvas=Image.new('RGB',size,'black')
        canvas.paste(image,((size[0]-image.width)//2,(size[1]-image.height)//2))
        return canvas

    def close(self):self.source.close()
