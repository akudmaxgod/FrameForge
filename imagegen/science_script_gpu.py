"""One animated scene texture, sampled by overview and moving close-up windows."""
import math
from functools import lru_cache
import numpy as np
from PIL import Image,ImageDraw,ImageColor
from .gpu_pixels import cuda
from .science_script import state

@lru_cache(maxsize=1)
def stamp_kernel():
 return cuda().RawKernel(r'''
 extern "C" __global__ void stamp(const unsigned char* src,unsigned char* dst,int sw,int sh,int dw,int dh,
 float x,float y,float w,float h,float angle,float alpha,float reveal){
 int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=dw*dh)return;int px=i%dw,py=i/dw;
 float u=(px+.5f-x)/w-.5f,v=(py+.5f-y)/h-.5f;
 float cs=cosf(angle),sn=sinf(angle);float a=cs*u+sn*v+.5f,b=-sn*u+cs*v+.5f;
 if(a<0||b<0||a>=1||b>=1||a>reveal)return;
 float xx=a*sw-.5f,yy=b*sh-.5f;int ix=floorf(xx),iy=floorf(yy);float fx=xx-ix,fy=yy-iy,val[4]={0,0,0,0};
 for(int j=0;j<2;j++)for(int k=0;k<2;k++){int sx=max(0,min(sw-1,ix+k)),sy=max(0,min(sh-1,iy+j));float q=(k?fx:1-fx)*(j?fy:1-fy);for(int c=0;c<4;c++)val[c]+=src[(sy*sw+sx)*4+c]*q;}
 float opacity=val[3]/255.f*alpha;
 for(int c=0;c<3;c++)dst[i*3+c]=(unsigned char)fminf(255.f,fmaxf(0.f,roundf(dst[i*3+c]*(1-opacity)+val[c]*alpha)));
 }''','stamp')

@lru_cache(maxsize=1)
def view_kernel():
 return cuda().RawKernel(r'''
 extern "C" __global__ void view(const unsigned char* src,unsigned char* dst,int sw,int sh,int dw,int dh,
 float x,float y,float w,float h,float bx,float by,float bw,float bh){
 int i=blockIdx.x*blockDim.x+threadIdx.x;if(i>=dw*dh)return;int px=i%dw,py=i/dw;
 float u=(px+.5f-x)/w,v=(py+.5f-y)/h;if(u<0||v<0||u>=1||v>=1)return;
 float nx=bx+u*bw,ny=by+v*bh;if(nx<0||ny<0||nx>=1||ny>=1)return;
 float xx=nx*sw-.5f,yy=ny*sh-.5f;int ix=floorf(xx),iy=floorf(yy);float fx=xx-ix,fy=yy-iy;
 for(int c=0;c<3;c++){float value=0;
 for(int j=0;j<2;j++)for(int k=0;k<2;k++){int sx=max(0,min(sw-1,ix+k)),sy=max(0,min(sh-1,iy+j));value+=src[(sy*sw+sx)*3+c]*(k?fx:1-fx)*(j?fy:1-fy);}
 dst[i*3+c]=(unsigned char)fminf(255.f,fmaxf(0.f,roundf(value)));}
 }''','view')

def contain(rect,width,height):
 x,y,w,h=rect;k=min(w/width,h/height)
 return [x+(w-width*k)/2,y+(h-height*k)/2,width*k,height*k]

class ScriptCompositor:
 def __init__(self,plan,resolve,size):
  from .science_atlas_render import CONFIG,font,lines
  self.cp=cp=cuda();self.plan=plan;self.scene=plan['script_scene'];self.size=size
  with Image.open(resolve(self.scene['asset_id'])) as image:self.source=image.convert('RGB')
  self.width,self.height=self.source.size
  self.scene['width']=self.width;self.scene['height']=self.height
  self.base=cp.asarray(np.asarray(self.source));self.canvas=cp.empty_like(self.base)
  self.output=cp.empty((size[1],size[0],3),dtype=cp.uint8);self.background=cp.asarray(ImageColor.getrgb(CONFIG['background']),dtype=cp.uint8)
  self.stats={'frames':0,'texture_uploads':1,'texture_bytes':self.base.nbytes,'rgb_downloads':0,'text_rasterizations':0,'cuda_composite_calls':0,'yuv_download_bytes':0}
  self.layers=[]
  estimated=self.base.nbytes*2+self.output.nbytes+1920*1080*4+664*246*4*len(self.scene['steps'])
  if estimated>256*1024*1024:raise RuntimeError('Исходник слишком велик для GPU-кэша научной сцены.')
  for layer in self.scene['layers']:
   x,y,w,h=layer['box'];bounds=(round(x*self.width),round(y*self.height),round((x+w)*self.width),round((y+h)*self.height))
   if layer.get('asset_id'):
    with Image.open(resolve(layer['asset_id'])) as image:im=image.convert('RGBA')
   else:
    im=self.source.crop(bounds).convert('RGBA')
    if layer['kind']=='rotate':
     mask=Image.new('L',im.size,0);ImageDraw.Draw(mask).ellipse((1,1,im.width-2,im.height-2),fill=255);im.putalpha(mask)
   self.layers.append((self.upload(im),[x*self.width,y*self.height,w*self.width,h*self.height]))
   if layer['kind']=='part':
    x0,y0,x1,y1=bounds;self.base[y0:y1,x0:x1]=(self.base[y0:y1,x0:x1].astype(cp.float32)*.3).astype(cp.uint8)
  static=Image.new('RGBA',(1920,1080));draw=ImageDraw.Draw(static)
  for c in self.scene['title_draw']:draw.text((c['x'],c['y']),c['text'],font=font(c['size'],c.get('weight',400)),fill=c['color'],anchor='ls')
  draw.text((self.scene['overview'][0]+8,184),'Общий вид',font=font(22),fill=self.scene['colors']['muted'],anchor='ls')
  draw.text((self.scene['window'][0],184),'Крупный план',font=font(22),fill=self.scene['colors']['muted'],anchor='ls')
  x,y,w,h=self.scene['window'];draw.rectangle((x-2,y-2,x+w+2,y+h+2),outline=CONFIG['accent'],width=2)
  if self.scene['paths']:draw.text((self.scene['overview'][0]+8,1010),'Условное обозначение процесса',font=font(20),fill=self.scene['colors']['muted'],anchor='ls')
  self.focus=[]
  for step in self.scene['steps']:
   region=step.get('region')
   if not region:self.focus.append(None);continue
   x,y,w,h=region['box'];im=Image.new('RGBA',(max(2,round(w*self.width)),max(2,round(h*self.height))))
   ImageDraw.Draw(im).rectangle((0,0,im.width-1,im.height-1),outline=CONFIG['accent'],width=3)
   self.focus.append((self.upload(im),[x*self.width,y*self.height,w*self.width,h*self.height]))
  self.static=self.upload(static);self.stats['text_rasterizations']+=1
  self.path_data=[]
  for path in self.scene['paths']:
   points=path['points'];lengths=[math.hypot((b[0]-a[0])*self.width,(b[1]-a[1])*self.height) for a,b in zip(points,points[1:])]
   self.path_data.append((path,lengths,sum(lengths),cp.asarray(ImageColor.getrgb(path.get('color','#6CC8D0')),dtype=cp.uint8)))
  self.captions=[]
  for index,step in enumerate(self.scene['steps']):
   im=Image.new('RGBA',(664,246));d=ImageDraw.Draw(im)
   for c in self.scene['caption_draw'][index]:d.text((c['x'],c['y']),c['text'],font=font(c['size'],c.get('weight',400)),fill=c['color'],anchor='ls')
   self.captions.append(self.upload(im));self.stats['text_rasterizations']+=1
 def upload(self,image):
  a=np.array(image.convert('RGBA'));a[:,:,:3]=np.rint(a[:,:,:3].astype(np.float32)*a[:,:,3:4]/255).astype(np.uint8)
  self.stats['texture_uploads']+=1;self.stats['texture_bytes']+=a.nbytes
  if self.stats['texture_bytes']>256*1024*1024:raise RuntimeError('GPU-кэш научной сцены превышает 256 МиБ.')
  return self.cp.asarray(a)
 def stamp(self,texture,target,box,angle=0,alpha=1,reveal=1):
  h,w=target.shape[:2];args=(texture,target,*map(np.int32,(texture.shape[1],texture.shape[0],w,h)),*map(np.float32,(*box,angle,alpha,reveal)))
  stamp_kernel()(((w*h+255)//256,),(256,),args);self.stats['cuda_composite_calls']+=1
 def view(self,box,crop):
  scale_x=self.size[0]/1920;scale_y=self.size[1]/1080;x,y,w,h=box
  args=(self.canvas,self.output,*map(np.int32,(self.width,self.height,*self.size)),*map(np.float32,(x*scale_x,y*scale_y,w*scale_x,h*scale_y,*crop)))
  view_kernel()(((self.size[0]*self.size[1]+255)//256,),(256,),args);self.stats['cuda_composite_calls']+=1
 def image(self,seconds):
  cp=self.cp;s=state(self.scene,seconds);self.canvas[:]=self.base
  for index,(texture,box) in enumerate(self.layers):
   layer=self.scene['layers'][index];motion=s['rotations'][index];progress=s['step_progress'][layer['step_index']]
   self.stamp(texture,self.canvas,box,math.radians(motion['angle']) if layer['kind']=='rotate' else 0,motion['opacity'],progress if layer['kind']=='part' else 1)
  for path,lengths,total,color in self.path_data:
   step=self.scene['steps'][path['step_index']];elapsed=s['time_ms']-step['at_ms']
   if elapsed<0 or total<=0:continue
   points=path['points']
   for i in range(path['count']):
    distance=((elapsed/path['period_ms']+i/path['count'])%1)*total
    for a,b,length in zip(points,points[1:],lengths):
     if distance<=length:
      u=distance/max(1e-8,length);x=(a[0]+(b[0]-a[0])*u)*self.width;y=(a[1]+(b[1]-a[1])*u)*self.height;break
     distance-=length
    radius=max(2,self.width*.003);x0=max(0,int(x-radius));x1=min(self.width,int(x+radius)+2);y0=max(0,int(y-radius));y1=min(self.height,int(y+radius)+2)
    yy,xx=cp.ogrid[y0:y1,x0:x1];mask=(xx-x)**2+(yy-y)**2<=radius**2
    self.canvas[y0:y1,x0:x1]=cp.where(mask[:,:,None],color,self.canvas[y0:y1,x0:x1])
  focus=self.focus[s['step_index']]
  if focus:self.stamp(focus[0],self.canvas,focus[1],alpha=.45*s['step_progress'][s['step_index']])
  self.output[:]=self.background;self.view(contain(self.scene['overview'],self.width,self.height),[0,0,1,1]);self.view(self.scene['window'],s['crop'])
  self.stamp(self.static,self.output,[0,0,*self.size])
  if s['caption_visible']:self.stamp(self.captions[s['step_index']],self.output,[self.scene['window'][0]*self.size[0]/1920,790*self.size[1]/1080,664*self.size[0]/1920,246*self.size[1]/1080])
  self.stats['frames']+=1;return self.output
 def pil_image(self,seconds):
  self.stats['rgb_downloads']+=1;return Image.fromarray(self.cp.asnumpy(self.image(seconds)))
 def close(self):
  self.source.close();self.layers.clear();self.captions.clear();self.base=self.canvas=self.output=self.static=None
