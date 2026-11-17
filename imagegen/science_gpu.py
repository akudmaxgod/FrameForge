"""Retained CUDA textures for slide_explain; PIL is used only during preparation."""
import math
import numpy as np
from PIL import Image, ImageDraw, ImageColor
from functools import lru_cache
from .gpu_pixels import cuda
from .science_slide_reveal import state, command_state

# Prepare antialiased masks once. Their affine sampling/reveal stays on CUDA.
SS=3
def _image(size):
    return Image.new('RGBA',(max(1,math.ceil(size[0])*SS),max(1,math.ceil(size[1])*SS)))
class _Draw:
    def __init__(self,image):self.draw=ImageDraw.Draw(image)
    def text(self,xy,value,**kwargs):self.draw.text(tuple(v*SS for v in xy),value,**kwargs)
    def line(self,xy,**kwargs):
        kwargs['width']=max(1,round(kwargs.get('width',1)*SS));self.draw.line(tuple(v*SS for v in xy),**kwargs)
    def ellipse(self,xy,**kwargs):
        kwargs['width']=max(1,round(kwargs.get('width',1)*SS));self.draw.ellipse(tuple(v*SS for v in xy),**kwargs)
    def rectangle(self,xy,**kwargs):
        kwargs['width']=max(1,round(kwargs.get('width',1)*SS));self.draw.rectangle(tuple(v*SS for v in xy),**kwargs)


@lru_cache(maxsize=1)
def composite_kernel():
    return cuda().RawKernel(r'''
    extern "C" __global__ void science_compose(const unsigned char* src,unsigned char* dst,
        int sw,int sh,int dw,int dh,int ox,int oy,int rw,int rh,
        float x,float y,float w,float h,float alpha,float progress,int mask,
        float ax,float ay,float bx,float by,float radius) {
      int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=rw*rh)return;
      int px=ox+i%rw,py=oy+i/rw;if(px<0||py<0||px>=dw||py>=dh)return;
      float u=(px+.5f-x)/w*sw-.5f,v=(py+.5f-y)/h*sh-.5f;
      if(u<-.5f||v<-.5f||u>sw-.5f||v>sh-.5f)return;
      if(mask==1 && u>sw*progress)return;
      if(mask==2){float dx=bx-ax,dy=by-ay;
        if(((u-ax)*dx+(v-ay)*dy)/fmaxf(1.f,dx*dx+dy*dy)>progress)return;}
      if(mask==3){float angle=atan2f(v-ay,u-ax)+1.570796327f;
        if(angle<0)angle+=6.283185307f;if(angle>progress*6.283185307f)return;}
      if(mask==4 && hypotf(u-ax,v-ay)>radius*progress)return;
      int ix=(int)floorf(u),iy=(int)floorf(v);float fx=u-ix,fy=v-iy;
      float rgba[4]={0,0,0,0};
      for(int j=0;j<2;j++)for(int k=0;k<2;k++){
        int sx=max(0,min(sw-1,ix+k)),sy=max(0,min(sh-1,iy+j));
        float weight=(k?fx:1-fx)*(j?fy:1-fy);
        for(int c=0;c<4;c++)rgba[c]+=src[(sy*sw+sx)*4+c]*weight;}
      float a=rgba[3]/255.f*alpha;int p=(py*dw+px)*3;
      for(int c=0;c<3;c++)dst[p+c]=(unsigned char)fminf(255.f,fmaxf(0.f,
        roundf(dst[p+c]*(1-a)+rgba[c]*alpha)));
    }''','science_compose')

class SlideCompositor:
    def __init__(self,plan,commands,resolve,size,budget=256*1024*1024):
        from .science_atlas_render import CONFIG,font
        self.plan=plan;self.size=size;self.layers=[];self.bytes=0;self.budget=budget
        self.stats={'cuda_composite_calls':0,'texture_uploads':0,'texture_bytes':0,'frames':0,
                    'text_rasterizations':0,'rgb_downloads':0,'yuv_download_bytes':0}
        self.background=ImageColor.getrgb(CONFIG['background']);self.cp=cuda()
        self.output=self.cp.empty((size[1],size[0],3),dtype=self.cp.uint8)
        self.background_gpu=self.cp.asarray(self.background,dtype=self.cp.uint8)
        shared={}
        for c in commands:
            kind=c['type'];mask=0;extra=[0.,0.,0.,0.,0.]
            if kind=='image':
                key=('image',c['asset_id'])
                if key not in shared:
                    with Image.open(resolve(c['asset_id'])) as im:shared[key]=self.upload(im.convert('RGBA'))
                tex=shared[key];sh,sw=tex.shape[:2];x,y,w,h=c['rect'];k=min(w/sw,h/sh)
                box=[x+(w-sw*k)/2,y+(h-sh*k)/2,sw*k,sh*k]
            else:
                pad=4
                if kind=='text':
                    f=font(max(1,round(c['size']))*SS,c.get('weight',400));bbox=tuple(v/SS for v in f.getbbox(c['text'],anchor='ls'))
                    left,top,right,bottom=bbox;x=c['x']+left-pad;y=c['y']+top-pad
                    w=max(1,right-left+pad*2);h=max(1,bottom-top+pad*2)
                    im=_image((w,h))
                    _Draw(im).text((pad-left,pad-top),c['text'],font=f,anchor='ls',fill=c['color'])
                    self.stats['text_rasterizations']+=1;mask=1
                elif kind in ('line','pulse'):
                    r=c.get('radius',0) if kind=='pulse' else c.get('width',3)/2
                    x=min(c['x1'],c['x2'])-r-pad;y=min(c['y1'],c['y2'])-r-pad
                    w=abs(c['x2']-c['x1'])+2*(r+pad);h=abs(c['y2']-c['y1'])+2*(r+pad)
                    if kind=='pulse':w=h=2*(r+pad);x=c['x1']-r-pad;y=c['y1']-r-pad
                    im=_image((w,h));draw=_Draw(im)
                    if kind=='line':
                        draw.line((c['x1']-x,c['y1']-y,c['x2']-x,c['y2']-y),fill=c['color'],width=max(1,round(c['width'])))
                        mask=2;extra[:4]=[c['x1']-x,c['y1']-y,c['x2']-x,c['y2']-y]
                    else:draw.ellipse((pad,pad,w-pad-1,h-pad-1),fill=c['color'])
                elif kind in ('ring','circle'):
                    r=c['radius'];pad+=c.get('width',0);x=c['x']-r-pad;y=c['y']-r-pad;w=h=2*(r+pad)
                    im=_image((w,h));draw=_Draw(im)
                    draw.ellipse((pad,pad,w-pad-1,h-pad-1),fill=c['color'] if kind=='circle' else None,
                                 outline=c['color'],width=max(1,round(c.get('width',1))))
                    mask=3 if kind=='ring' else 4;extra=[r+pad,r+pad,0.,0.,r]
                elif kind=='rect':
                    pad+=c.get('width',0);x=c['x']-pad;y=c['y']-pad;w=c['w']+2*pad;h=c['h']+2*pad
                    im=_image((w,h))
                    _Draw(im).rectangle((pad,pad,w-pad-1,h-pad-1),outline=c['color'],fill=c['color'] if c.get('fill') else None,width=max(1,round(c['width'])));mask=1
                else:raise ValueError('Unsupported retained scientific primitive: '+kind)
                tex=self.upload(im.resize((im.width//SS,im.height//SS),Image.Resampling.LANCZOS));box=[x,y,tex.shape[1],tex.shape[0]]
            self.layers.append((c,tex,box,mask,extra))
        self.stats['texture_bytes']=self.bytes

    def upload(self,image):
        a=np.array(image,dtype=np.uint8,copy=True)
        # Premultiply once, preventing dark edges during bilinear interpolation.
        a[:,:,:3]=np.rint(a[:,:,:3].astype(np.float32)*a[:,:,3:4]/255).astype(np.uint8)
        if self.bytes+a.nbytes>self.budget:raise RuntimeError('GPU-кэш научпопа превышает 256 МиБ. Уменьшите размер иллюстраций.')
        self.bytes+=a.nbytes;self.stats['texture_uploads']+=1
        return self.cp.asarray(a)

    def image(self,seconds):
        cp=self.cp;sw,sh=self.size;scale=sw/1920;sy=sh/1080
        self.output[:]=self.background_gpu
        group=state(self.plan,seconds);final=group['final_rect'];rect=group['rect'];k=rect[2]/final[2]
        for c,tex,original,mask,extra in self.layers:
            cs=command_state(self.plan,c,seconds);progress=cs['progress']
            if progress<=0:continue
            x,y,w,h=original;alpha=cs['opacity']
            if c['type']=='pulse':
                phase=(seconds*1000/c.get('period_ms',2200)+c.get('phase',0))%1
                x+=(c['x2']-c['x1'])*phase;y+=(c['y2']-c['y1'])*phase
            if c.get('motion_group')=='illustration':
                x=rect[0]+(x-final[0])*k;y=rect[1]+(y-final[1])*k;w*=k;h*=k;alpha*=group['opacity']
            x*=scale;y*=sy;w*=scale;h*=sy
            ox=max(0,math.floor(x));oy=max(0,math.floor(y));rw=min(sw,math.ceil(x+w))-ox;rh=min(sh,math.ceil(y+h))-oy
            if rw<=0 or rh<=0:continue
            # Images fade as a whole; all other reveals use CUDA masks.
            if c['type']=='image':alpha*=min(1.,progress*3)
            args=(tex,self.output,*map(np.int32,(tex.shape[1],tex.shape[0],sw,sh,ox,oy,rw,rh)),
                  *map(np.float32,(x,y,w,h,alpha,progress)),np.int32(mask),*map(np.float32,extra))
            composite_kernel()(((rw*rh+255)//256,),(256,),args);self.stats['cuda_composite_calls']+=1
        self.stats['frames']+=1
        return self.output

    def pil_image(self,seconds):
        self.stats['rgb_downloads']+=1
        return Image.fromarray(self.cp.asnumpy(self.image(seconds)))

    def close(self):
        self.layers.clear();self.output=None;self.background_gpu=None
