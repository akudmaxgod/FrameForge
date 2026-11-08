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
        self.static_frame = None
        self.static_geometry = None

    def image(self,seconds,size,fill=False):
        plan=self.clip.get('camera_motion')
        if plan:
            total_ms=self.clip.get('camera_motion_total_ms') or self.clip['base_duration_ms']
            duration=(total_ms-self.clip.get('hold_last_ms',0))/1000
            # Same last-frame phase as the application's streamed 2.5D path.
            phase=seconds/max(1/24,duration-1/24)
            x,y,w,h=camera_box(*self.source.size,plan,phase)
            # A portrait/non-16:9 photo keeps its proportions while moving.
            # Framing must agree with the static fit/fill path, never stretch.
            if fill:
                ratio=size[0]/size[1]
                if w/h>ratio:
                    next_w=h*ratio;x+=(w-next_w)/2;w=next_w
                else:
                    next_h=w/ratio;y+=(h-next_h)/2;h=next_h
                output_size=size
            else:
                scale=min(size[0]/self.source.width,size[1]/self.source.height)
                output_size=(max(1,round(self.source.width*scale)),max(1,round(self.source.height*scale)))
            # Resize directly from a fractional box, as in the local 2.5D renderer.
            moving = self.source.resize(output_size, Image.Resampling.BICUBIC,
                                        box=(x, y, min(self.source.width, x+w),
                                             min(self.source.height, y+h)))
            if output_size == size:
                return moving
            canvas = Image.new('RGB', size, 'black')
            canvas.paste(moving, ((size[0]-moving.width)//2, (size[1]-moving.height)//2))
            return canvas
        geometry = (size, fill)
        if geometry != self.static_geometry:
            image = (ImageOps.fit if fill else ImageOps.contain)(self.source, size, Image.Resampling.BICUBIC)
            if image.size != size:
                canvas = Image.new('RGB', size, 'black')
                canvas.paste(image, ((size[0]-image.width)//2, (size[1]-image.height)//2))
                image = canvas
            self.static_geometry = geometry
            self.static_frame = image
        return self.static_frame

    def close(self):self.source.close()
