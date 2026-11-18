import copy,json,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
from imagegen.science_slide_reveal import state,command_state,compile_slide,timeline
from imagegen.science_atlas_render import compile_commands,ScienceReader,render_video,font

def fixture(direction='left',duration=6):
 return {'version':'science-atlas/1','presentation':'visual_first','presentation_motion':'slide_explain',
 'slide_direction':direction,'layout':'hero_caption','illustrations':[{'asset_id':'test','width':1000,'height':1000}],
 'illustration_motion':{'duration_seconds':duration},'explanatory_labels':['Путь сигнала','Свет поступает в глаз','Сеть нейронов обрабатывает сигнал','Мозг формирует изображение'],
 'support_quote':'Свет поступает в глаз, сеть нейронов обрабатывает сигнал.',
 'explanatory_scheme':{'kind':'signal','labels':['Свет','Глаз','Сеть нейронов'],'cue_quote':'Свет поступает в глаз'},
 'annotations':[{'x':.4,'y':.5,'label':'Метка','position_verified':True}],
 'cue_ms':[700]}

class RevealTests(unittest.TestCase):
 def test_late_spoken_caption_does_not_appear_before_its_words(self):
  p=fixture();p['reveal_steps']=[{'cue_quote':'Свет поступает','title':'Свет','at_ms':5900,'timing_source':'audio_alignment'}]
  command={'type':'text','motion_group':'support','reveal_group':0}
  self.assertEqual(command_state(p,command,5.8)['progress'],0);self.assertGreater(command_state(p,command,5.95)['progress'],0)
  code="import {slideCommandState} from './imagegen/static/science-slide-reveal.mjs';const p="+json.dumps(p)+";console.log(JSON.stringify(slideCommandState(p,"+json.dumps(command)+",5.95)));"
  result=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],text=True));self.assertEqual(result,command_state(p,command,5.95))
 def test_mirror_motion_safe_bounds_and_seek(self):
  for duration in (.05,.2,1,2,6,60):
   a,b=fixture('left',duration),fixture('right',duration)
   for t in (0,.1,duration*.25,duration*.5,duration-.001,duration):
    x=state(a,t);y=state(b,t)
    self.assertAlmostEqual(x['rect'][0]+y['rect'][0]+x['rect'][2],1920)
    self.assertEqual(x['rect'][2:],y['rect'][2:]);self.assertLessEqual(x['rect'][0]+x['rect'][2],1848.001)
    self.assertGreaterEqual(x['rect'][1],71.99);self.assertLessEqual(x['rect'][1]+x['rect'][3],1008.01)
    self.assertEqual(state(a,t),state(a,t))
   self.assertEqual(state(a,duration)['progress'],1)
 def test_preparation_is_immutable_and_legacy_is_opt_in(self):
  p=fixture();original=copy.deepcopy(p);prepared=compile_commands(p);self.assertEqual(p,original)
  self.assertTrue(any(c.get('motion_group')=='illustration' and c['type']=='ring' for c in prepared['commands']))
  support_text=' '.join(c['text'] for c in prepared['commands'] if c.get('motion_group')=='support' and c['type']=='text')
  for note in p['explanatory_labels']:self.assertIn(note,support_text)
  self.assertNotIn('Метка',support_text)
  p.pop('presentation_motion');self.assertNotIn('slide_contract_version',compile_commands(p))
 def test_cues_complete_on_short_and_long_scenes(self):
  for duration in (.05,.2,1,2,6,60):
   p=fixture(duration=duration);p['support_reveal_ms']=duration*1000-10;p['cue_ms']=[999999]
   p=compile_commands(p)
   for c in p['commands']:
    self.assertEqual(command_state(p,c,duration-.001)['progress'],0 if duration*1000<c.get('min_duration_ms',0) else 1)
   p['support_reveal_ms']=0;p['cue_ms']=[700]
   first=next(c for c in p['commands'] if c.get('motion_group')=='support')
   self.assertEqual(command_state(p,first,0)['progress'],0)
 def test_typeset_text_and_primitives_fit_safe_columns(self):
  for direction in ('left','right'):
   p=compile_commands(fixture(direction))
   for c in p['commands']:
    if c['type']=='text':
     l,t,r,b=font(round(c['size']),c['weight']).getbbox(c['text'],anchor='ls')
     self.assertGreaterEqual(c['x']+l,48);self.assertLessEqual(c['x']+r,1872)
     self.assertGreaterEqual(c['y']+t,24);self.assertLessEqual(c['y']+b,1032)
 def test_long_bold_note_wraps_inside_its_column(self):
  p=fixture();p['explanatory_labels']=['Таламуса с обратными связями','Карта поля зрения в первичной'];p=compile_commands(p)
  for c in p['commands']:
   if c['type']=='text' and c.get('motion_group')=='support':
    self.assertLessEqual(c['x']+font(round(c['size']),c['weight']).getbbox(c['text'],anchor='ls')[2],1849)
 def test_future_plans_allow_four_distinct_notes_and_safe_defaults(self):
  from imagegen.science_planner import Shot
  from tests.test_science_quality import data
  raw=data()['shots'][0];shot=Shot.model_validate(raw);self.assertEqual(shot.presentation_motion,'none')
  raw.update(presentation_motion='slide_explain',slide_direction='right',labels=['Роговица','Зрачок','Световой сигнал','Обработка сигнала'])
  shot=Shot.model_validate(raw);self.assertEqual(len(shot.labels),4);self.assertEqual(shot.slide_direction,'right')
 def test_python_browser_timing_parity(self):
  cases=[]
  for duration in (.05,.2,1,2,6,60):
   for direction in ('left','right'):
    p=compile_commands(fixture(direction,duration));p['support_reveal_ms']=duration*700
    for seconds in (0,.01,duration*.2,duration*.5,duration-.001):
     cases.append({'plan':p,'seconds':seconds,'state':state(p,seconds),'commands':[command_state(p,c,seconds) for c in p['commands']]})
  code="""import fs from 'node:fs';import {slideState,slideCommandState} from './imagegen/static/science-slide-reveal.mjs';
  const cases=JSON.parse(fs.readFileSync(0,'utf8'));const close=(a,b)=>{if(typeof a==='number'){if(Math.abs(a-b)>1e-7)throw Error(`${a} != ${b}`);}else if(Array.isArray(a))a.forEach((v,i)=>close(v,b[i]));else Object.keys(a).forEach(k=>close(a[k],b[k]));};
  for(const x of cases){close(x.state,slideState(x.plan,x.seconds));close(x.commands,x.plan.commands.map(c=>slideCommandState(x.plan,c,x.seconds)));}console.log(cases.length+' parity cases passed');"""
  r=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(cases),text=True,capture_output=True);self.assertEqual(r.returncode,0,r.stderr)
 def test_missing_gpu_is_explicit_and_never_renders_on_cpu(self):
  reader=ScienceReader(compile_commands(fixture()),lambda _:None)
  try:
   with patch('imagegen.science_gpu.cuda',side_effect=RuntimeError('CUDA unavailable')):
    with self.assertRaisesRegex(RuntimeError,'CUDA unavailable'):reader.image(1,(640,360))
  finally:reader.close()
 def test_cuda_textures_remain_resident_and_no_frame_text_rasterization(self):
  from imagegen.gpu_pixels import cuda,video_frame
  cp=cuda()
  with tempfile.TemporaryDirectory() as d:
   image=Path(d)/'image.png';im=Image.new('RGB',(256,256),'#3b5960');ImageDraw.Draw(im).rectangle((5,5,250,250),outline='white',width=5);im.save(image)
   r=ScienceReader(compile_commands(fixture()),lambda _:image)
   try:
    comp=r.compositor((640,360));uploads=comp.stats['texture_uploads'];texts=comp.stats['text_rasterizations']
    with patch('PIL.ImageDraw.ImageDraw.text',side_effect=AssertionError('per-frame text')),patch.object(cp,'asnumpy',side_effect=AssertionError('RGB round trip')):
     frames=[r.gpu_image(t,(640,360)).copy() for t in (0,.5,1,2,5.99,2,.5)]
     cp.cuda.get_current_stream().synchronize()
     self.assertTrue(bool(cp.array_equal(frames[1],frames[-1])));self.assertFalse(bool(cp.array_equal(frames[1],frames[4])))
    self.assertEqual(uploads,comp.stats['texture_uploads']);self.assertEqual(texts,comp.stats['text_rasterizations'])
    self.assertGreater(comp.stats['cuda_composite_calls'],10);self.assertEqual(comp.stats['rgb_downloads'],0)
    self.assertEqual(video_frame(frames[4]).format.name,'yuv420p')
   finally:r.close()

if __name__=='__main__':unittest.main()
