"""CUDA pixel operations. Heavy image transforms never fall back to CPU."""
import os
from pathlib import Path
from functools import lru_cache
import numpy as np
from PIL import Image

from .gpu_runtime import prepare_dlls

@lru_cache(maxsize=1)
def cuda():
    prepare_dlls()
    from .release_paths import data_root
    os.environ.setdefault('CUPY_CACHE_DIR',str(data_root()/'cuda-cache'))
    import cupy as cp
    if cp.cuda.runtime.getDeviceCount()<1:raise RuntimeError('CUDA GPU недоступна. CPU-рендер отключён.')
    return cp

@lru_cache(maxsize=1)
def crop_kernel():
    return cuda().RawKernel(r"""
    __device__ float cubic(float x){x=fabsf(x);if(x<=1)return (1.5f*x-2.5f)*x*x+1;
      if(x<2)return ((-.5f*x+2.5f)*x-4)*x+2;return 0;}
    extern "C" __global__ void crop(const unsigned char* src,unsigned char* dst,
      int sw,int sh,int dw,int dh,int channels,float x0,float y0,float bw,float bh){
      int i=blockDim.x*blockIdx.x+threadIdx.x;if(i>=dw*dh*channels)return;
      int c=i%channels,p=i/channels,x=p%dw,y=p/dw;
      float sx=x0+(x+.5f)*bw/dw-.5f,sy=y0+(y+.5f)*bh/dh-.5f;
      int ix=(int)floorf(sx),iy=(int)floorf(sy);float value=0,weight=0;
      for(int j=-1;j<=2;j++)for(int k=-1;k<=2;k++){
        float w=cubic(sx-ix-k)*cubic(sy-iy-j);int px=max(0,min(sw-1,ix+k)),py=max(0,min(sh-1,iy+j));
        value+=w*src[(py*sw+px)*channels+c];weight+=w;
      }
      dst[i]=(unsigned char)min(255.f,max(0.f,roundf(value/weight)));
    }""",'crop')

class CudaImage:
    def __init__(self,image):
        cp=cuda()
        if isinstance(image,cp.ndarray):self.data=image;self.mode='RGBA' if image.shape[-1]==4 else 'RGB'
        else:
            image=image if image.mode in ('RGB','RGBA','L') else image.convert('RGB');self.mode=image.mode;self.data=cp.asarray(np.asarray(image))
        self.height,self.width=self.data.shape[:2];self.channels=1 if self.data.ndim==2 else self.data.shape[2]
        if self.channels==1:self.data=self.data[:,:,None]
        if self.channels==4:self.data[:,:,:3]=cp.rint(self.data[:,:,:3].astype(cp.float32)*self.data[:,:,3:4]/255).astype(cp.uint8)
    def resize(self,size,box=None):
        cp=cuda();out=self.resize_gpu(size,box)
        return Image.fromarray(cp.asnumpy(out))
    def resize_gpu(self,size,box=None):
        cp=cuda();w,h=size;x,y,bw,bh=(0,0,self.width,self.height) if box is None else (box[0],box[1],box[2]-box[0],box[3]-box[1])
        out=cp.empty((h,w,self.channels),dtype=cp.uint8);n=h*w*self.channels
        crop_kernel()(((n+255)//256,),(256,),(self.data,out,np.int32(self.width),np.int32(self.height),np.int32(w),np.int32(h),np.int32(self.channels),np.float32(x),np.float32(y),np.float32(bw),np.float32(bh)))
        if self.channels==4:out[:,:,:3]=cp.clip(cp.rint(out[:,:,:3].astype(cp.float32)*255/cp.maximum(out[:,:,3:4],1)),0,255).astype(cp.uint8)
        return out[:,:,0] if self.channels==1 else out

def blend(left,right,alpha):
    cp=cuda();gpu=isinstance(left,cp.ndarray) or isinstance(right,cp.ndarray)
    a=cp.asarray(left if isinstance(left,cp.ndarray) else np.asarray(left),dtype=cp.float32);b=cp.asarray(right if isinstance(right,cp.ndarray) else np.asarray(right),dtype=cp.float32)
    out=cp.clip(a*(1-alpha)+b*alpha,0,255).astype(cp.uint8)
    return out if gpu else Image.fromarray(cp.asnumpy(out))

def fit(image,size,fill=False):
    src=CudaImage(image);w,h=image.size;ratio=size[0]/size[1]
    if fill:
        cw,ch=(h*ratio,h) if w/h>ratio else (w,w/ratio)
        return src.resize(size,((w-cw)/2,(h-ch)/2,(w+cw)/2,(h+ch)/2))
    scale=min(size[0]/w,size[1]/h);out_size=(max(1,round(w*scale)),max(1,round(h*scale)))
    result=src.resize(out_size)
    if out_size==size:return result
    cp=cuda();out=cp.zeros((size[1],size[0],3),dtype=cp.uint8);x=(size[0]-out_size[0])//2;y=(size[1]-out_size[1])//2
    out[y:y+out_size[1],x:x+out_size[0]]=cp.asarray(np.asarray(result))
    return Image.fromarray(cp.asnumpy(out))

def fit_gpu(image,size,fill=False):
    cp=cuda();src=image if isinstance(image,CudaImage) else CudaImage(image)
    w,h=src.width,src.height;ratio=size[0]/size[1]
    if fill:
        cw,ch=(h*ratio,h) if w/h>ratio else (w,w/ratio)
        return src.resize_gpu(size,((w-cw)/2,(h-ch)/2,(w+cw)/2,(h+ch)/2))
    scale=min(size[0]/w,size[1]/h);out_size=(max(1,round(w*scale)),max(1,round(h*scale)))
    result=src.resize_gpu(out_size)
    if out_size==size:return result
    out=cp.zeros((size[1],size[0],3),dtype=cp.uint8);x=(size[0]-out_size[0])//2;y=(size[1]-out_size[1])//2
    out[y:y+out_size[1],x:x+out_size[0]]=result
    return out

def effects(frame,settings,index):
    import math
    cp=cuda();film=settings.get('old_film',0)/100;flare=settings.get('light_leaks',0)/100
    if not film and not flare:return frame
    a=cp.asarray(frame if isinstance(frame,cp.ndarray) else np.asarray(frame),dtype=cp.float32);h,w=a.shape[:2];y,x=cp.ogrid[:h,:w]
    if flare:
        radius=cp.hypot((x-.05*w)/w,(y-.17*h)/w);reflection=cp.hypot((x-.68*w)/w,(y-.64*h)/w)
        opacity=cp.maximum(.48*cp.maximum(0,1-radius/.61),.11*cp.maximum(0,1-reflection/.09))
        level=flare*(.45+.55*(.5+.5*math.sin(index*.055))**3)
        alpha=(cp.rint(255*opacity).astype(cp.uint8).astype(cp.float32)*level/255)[:,:,None]
        a=a*(1-alpha)+cp.asarray([255,171,98],dtype=cp.float32)*alpha
    if film:
        seed=index+1
        def sample():
            nonlocal seed
            seed=(1664525*seed+1013904223)&0xffffffff
            return seed/4294967296
        for point in range(180):
            px,py=sample()*w,sample()*h;dust=point<3
            radius=(.0012+sample()*.0018 if dust else .0006)*w
            tone=18 if dust or point%2==0 else 240;alpha=round((125 if dust else 29)*film)/255
            x0,x1=max(0,int(px-radius)),min(w,int(px+radius)+2)
            y0,y1=max(0,int(py-radius*.55)),min(h,int(py+radius*.55)+2)
            yy,xx=cp.ogrid[y0:y1,x0:x1]
            mask=((xx-px)/radius)**2+((yy-py)/(radius*.55))**2<=1
            part=a[y0:y1,x0:x1]
            a[y0:y1,x0:x1]=cp.where(mask[:,:,None],part*(1-alpha)+tone*alpha,part)
    out=cp.clip(a,0,255).astype(cp.uint8)
    return out if isinstance(frame,cp.ndarray) else Image.fromarray(cp.asnumpy(out))

def resize(image,size):return CudaImage(image).resize(size)

def affine(image,size,data):
    cp=cuda()
    from cupyx.scipy.ndimage import map_coordinates
    a=cp.asarray(np.asarray(image));y,x=cp.ogrid[:size[1],:size[0]]
    xx=cp.broadcast_to(data[0]*x+data[1]*y+data[2],(size[1],size[0]));yy=cp.broadcast_to(data[3]*x+data[4]*y+data[5],xx.shape)
    coords=cp.stack((yy,xx));channels=1 if a.ndim==2 else a.shape[2]
    if channels==1:a=a[:,:,None]
    a=a.astype(cp.float32)
    if channels==4:a[:,:,:3]*=a[:,:,3:4]/255
    out=cp.stack([map_coordinates(a[:,:,c],coords,order=3,mode='constant',cval=0,prefilter=True) for c in range(channels)],axis=2)
    if channels==4:out[:,:,:3]*=255/cp.maximum(out[:,:,3:4],1)
    out=cp.clip(cp.rint(out),0,255).astype(cp.uint8)
    return Image.fromarray(cp.asnumpy(out[:,:,0] if channels==1 else out))

def rotate(image,angle,center):
    import math
    theta=math.radians(angle);a,b=math.cos(theta),math.sin(theta);x,y=center
    return affine(image,image.size,(a,-b,x-a*x+b*y,b,a,y-b*x-a*y))

def paste(image,layer,box,mask=None):
    cp=cuda();x,y=box[:2]
    if isinstance(layer,tuple):
        width,height=box[2]-x,box[3]-y;src=cp.empty((height,width,len(layer)),dtype=cp.uint8);src[:]=cp.asarray(layer)
    else:src=cp.asarray(np.asarray(layer));height,width=src.shape[:2]
    out=cp.asarray(np.asarray(image));left,top=max(0,x),max(0,y);right,bottom=min(image.width,x+width),min(image.height,y+height)
    if right<=left or bottom<=top:return
    region=src[top-y:bottom-y,left-x:right-x];dst=out[top:bottom,left:right]
    if region.ndim==3 and region.shape[2]!=dst.shape[2]:region=region[:,:,:dst.shape[2]]
    if mask is not None:
        opacity=cp.asarray(np.asarray(mask));opacity=opacity[:,:,3] if opacity.ndim==3 else opacity
        opacity=opacity[top-y:bottom-y,left-x:right-x].astype(cp.float32)/255
        if dst.ndim==3:opacity=opacity[:,:,None]
        region=cp.rint(dst*(1-opacity)+region*opacity).astype(cp.uint8)
    out[top:bottom,left:right]=region
    image.frombytes(cp.asnumpy(out).tobytes())

def video_frame(image):
    import av
    cp=cuda();a=cp.asarray(image if isinstance(image,cp.ndarray) else np.asarray(image.convert('RGB')),dtype=cp.float32);h,w=a.shape[:2]
    if h%2 or w%2:raise ValueError('GPU H.264 требует чётные размеры кадра.')
    red,green,blue=a[:,:,0],a[:,:,1],a[:,:,2]
    y=cp.clip(cp.rint(.256788*red+.504129*green+.097906*blue+16),16,235).astype(cp.uint8)
    u=-.148223*red-.290993*green+.439216*blue+128
    v=.439216*red-.367788*green-.071427*blue+128
    uv=lambda plane:cp.clip(cp.rint(plane.reshape(h//2,2,w//2,2).mean(axis=(1,3))),16,240).astype(cp.uint8)
    flat=cp.concatenate((y.ravel(),uv(u).ravel(),uv(v).ravel())).reshape(h*3//2,w)
    return av.VideoFrame.from_ndarray(cp.asnumpy(flat),format='yuv420p')

def image_from_video(frame,gpu=False):
    cp=cuda();h,w=frame.height,frame.width;name=frame.format.name
    if name in ('yuv420p','nv12'):
        raw=cp.asarray(frame.to_ndarray());flat=raw.ravel();y=flat[:h*w].reshape(h,w).astype(cp.float32)
        if name=='yuv420p':
            u=flat[h*w:h*w+h*w//4].reshape(h//2,w//2);v=flat[h*w+h*w//4:].reshape(h//2,w//2)
        else:
            chroma=flat[h*w:].reshape(h//2,w);u=chroma[:,::2];v=chroma[:,1::2]
        u=u.astype(cp.float32).repeat(2,axis=0).repeat(2,axis=1)-128;v=v.astype(cp.float32).repeat(2,axis=0).repeat(2,axis=1)-128
        y=1.164384*(y-16)
        rgb=cp.stack((y+1.596027*v,y-.391762*u-.812968*v,y+2.017232*u),axis=2)
        result=cp.clip(cp.rint(rgb),0,255).astype(cp.uint8)
        return result if gpu else Image.fromarray(cp.asnumpy(result))
    raise RuntimeError(f'GPU-конвертация формата {name} пока не поддерживается; CPU-конвертация отключена.')
