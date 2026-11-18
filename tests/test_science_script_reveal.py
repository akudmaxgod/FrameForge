import copy,json,subprocess,tempfile,unittest,queue,threading
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image,ImageDraw
from imagegen.science_script import prepare_plan,compile_script,state,bind_audio
from imagegen.science_atlas_render import compile_commands,ScienceReader
from imagegen.science_grounding import consensus
from imagegen.gpu_pixels import cuda

def mechanism():
 source='Вода входит в канал. Рабочее колесо вращается. Вал передаёт вращение. Генератор создаёт ток.'
 titles=['Вход воды','Рабочее колесо','Вал','Генератор']
 steps=[{'kind':'focus','cue_quote':q,'title':t,'explanation':'','region_key':str(i)} for i,(q,t) in enumerate(zip(source.split('. ')[:3]+[source.split('. ')[-1]],titles))]
 p={'version':'science-atlas/1','presentation':'visual_first','frame_density':'filled','presentation_motion':'panorama_explain','layout':'hero_caption','asset_keys':['mechanism'],'headline':'Как работает гидроэлектростанция','explanatory_labels':titles,'support_quote':source,'illustration_motion':{'duration_seconds':8},'illustrations':[{'asset_id':'source','width':1280,'height':720}],'reveal_steps':steps}
 p=prepare_plan(p,source)
 p['scene_regions']=[{'key':str(i),'box':[.05+i*.23,.34,.14,.25],'asset_id':'source','verified':True,'method':'vector_geometry'} for i in range(4)]
 p['scene_layers']=[{'kind':'rotate','source_asset_id':'source','box':[.28,.34,.14,.25],'verified':True,'step_index':1,'degrees_per_second':120}]
 p['scene_paths']=[{'source_asset_id':'source','verified':True,'step_index':0,'points':[[.02,.46],[.3,.46]],'count':6,'period_ms':1600}]
 return p

class ScriptRevealTests(unittest.TestCase):
 def test_spatial_analysis_does_not_block_media_callback_and_is_deduplicated(self):
  import imagegen.science_grounding as grounding
  entered=threading.Event();release=threading.Event();done=threading.Event();service=Mock();service.root=Path(tempfile.gettempdir())
  def analyze(*args):entered.set();release.wait(5);return {'scene_regions':[{},{}]}
  with patch.multiple(grounding,_QUEUE=queue.Queue(10),_JOBS={},_DEFERRED={},_STOP=threading.Event(),_THREAD=None),patch.object(grounding,'identity',return_value=('key',{},[])),patch.object(grounding,'cached',return_value={}),patch.object(grounding,'available',return_value=True),patch.object(grounding,'ensure',side_effect=analyze) as analysis,patch('imagegen.science_atlas_render.assemble_project',side_effect=lambda *a,**k:done.set()):
   try:
    self.assertEqual(grounding.schedule(service,'project','scene',mechanism()),'queued');self.assertTrue(entered.wait(2))
    self.assertEqual(grounding.schedule(service,'project','scene',mechanism()),'running');self.assertEqual(grounding.status(service,'project')['pending'],1)
    release.set();self.assertTrue(done.wait(2));grounding._QUEUE.join();self.assertEqual(analysis.call_count,1);self.assertEqual(grounding.status(service,'project')['pending'],0)
   finally:release.set();grounding.shutdown()
 def test_automatic_beats_are_generic_and_source_bound(self):
  for source in ('Вода входит в канал. Колесо вращается. Генератор создаёт ток.','Снег отражает падающий свет. В тени поверхность выглядит темнее.'):
   p=prepare_plan({'frame_density':'filled','asset_keys':['a'],'explanatory_labels':['Первый тезис','Второй тезис']},source,20)
   self.assertGreaterEqual(len(p['reveal_steps']),2)
   for step in p['reveal_steps']:self.assertEqual(source.count(step['cue_quote']),1);self.assertEqual(step['source_char_start'],20+source.index(step['cue_quote']))
 def test_whitespace_and_quoted_speech_keep_exact_cues(self):
  source='«Вода  проходит через канал».\n\nКолесо вращается под напором воды.'
  p=prepare_plan({'frame_density':'filled','asset_keys':['a'],'explanatory_labels':['Вода','Колесо']},source)
  self.assertEqual(len(p['reveal_steps']),2)
  at=p['reveal_steps'][0]['source_char_start'];bound=bind_audio(p,[{'char_start':at+1,'char_end':at+5,'start':.5}])
  self.assertEqual(bound['reveal_steps'][0]['at_ms'],500)
 def test_unverified_or_wrong_asset_regions_keep_ordinary_layout(self):
  p=mechanism()
  for r in p['scene_regions']:r['asset_id']='old-image'
  self.assertIsNone(compile_script(p));self.assertNotIn('script_scene',compile_commands(p))
 def test_preview_clock_matches_python_and_seek_is_reversible(self):
  scene=compile_script(mechanism());cases=[{'scene':scene,'t':t,'expected':state(scene,t)} for t in (-1,0,.2,2.1,3.2,4.9,7.99,4.9,1,20)]
  code="import fs from 'node:fs';import {scriptState} from './imagegen/static/science-script.mjs';const eq=(a,b)=>{if(typeof a==='number'){if(Math.abs(a-b)>1e-7)throw Error(`${a} != ${b}`)}else if(Array.isArray(a))a.forEach((v,i)=>eq(v,b[i]));else Object.keys(a).forEach(k=>eq(a[k],b[k]));};for(const c of JSON.parse(fs.readFileSync(0,'utf8')))eq(c.expected,scriptState(c.scene,c.t));"
  r=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(cases),text=True,capture_output=True);self.assertEqual(r.returncode,0,r.stderr)
  for c in cases:
   x,y,w,h=c['expected']['crop'];self.assertGreater(w,0);self.assertGreater(h,0);self.assertAlmostEqual(w/h,(scene['window'][2]/scene['window'][3])/(scene['width']/scene['height']))
 def test_camera_moves_through_intermediate_region(self):
  scene=compile_script(mechanism());at=scene['steps'][2]['at_ms']/1000
  a=state(scene,at)['crop'];b=state(scene,at+.5)['crop'];c=state(scene,at+1.2)['crop']
  self.assertNotEqual(a,b);self.assertNotEqual(b,c);self.assertTrue(a[0]<b[0]<c[0])
 def test_manual_non_panorama_choice_does_not_keep_a_derived_camera(self):
  p=compile_commands(mechanism());self.assertIn('script_scene',p)
  p['presentation_motion']='focus_explain';p['presentation_locked']=True
  self.assertNotIn('script_scene',compile_commands(p))
 def test_voice_alignment_replaces_estimates(self):
  p=mechanism();before=copy.deepcopy(p);at=p['reveal_steps'][1]['source_char_start']
  result=bind_audio(p,[{'char_start':at,'char_end':at+10,'start':4.2}],-1000)
  self.assertEqual(p,before);self.assertEqual(result['reveal_steps'][1]['at_ms'],3200);self.assertEqual(result['reveal_steps'][1]['timing_source'],'audio_alignment')
 def test_local_grounding_requires_two_independent_agreeing_regions(self):
  p=mechanism();asset={'id':'source'};targets=[{'key':str(i)} for i in range(4)];good={'regions':[{'key':str(i),'box':[.05+i*.23,.34,.14,.25],'visible':True,'confidence':.99,'kind':'part'} for i in range(4)]}
  self.assertEqual(len(consensus(good,good,asset,p['reveal_steps'],targets)['scene_regions']),4)
  bad=copy.deepcopy(good);bad['regions'][1]['box']=[.7,.1,.2,.2]
  self.assertEqual(len(consensus(good,bad,asset,p['reveal_steps'],targets)['scene_regions']),3)
  for invalid in ('1',1.5,-1,10):
   response={**good,'paths':[{'step_index':invalid,'confidence':1,'points':[[.1,.2],[.2,.3]]}]}
   self.assertEqual(consensus(response,response,asset,p['reveal_steps'],targets)['scene_paths'],[])
 def test_gpu_retains_layers_and_recreates_identical_frames_after_seek(self):
  cp=cuda()
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'source.png';im=Image.new('RGB',(1280,720),'#173039');d=ImageDraw.Draw(im);d.ellipse((350,245,535,425),fill='#cb9973');im.save(path)
   reader=ScienceReader(compile_commands(mechanism()),lambda _:path)
   try:
    self.assertIn('script_scene',reader.plan);comp=reader.compositor((640,360));uploads=comp.stats['texture_uploads'];first=reader.gpu_image(3.2,(640,360)).copy()
    with patch.object(cp,'asnumpy',side_effect=AssertionError('Intermediate RGB transfer')),patch('PIL.ImageDraw.ImageDraw.text',side_effect=AssertionError('Per-frame text')):
     for t in (.1,7.8,3.2):reader.gpu_image(t,(640,360))
     self.assertTrue(bool(cp.array_equal(first,reader.gpu_image(3.2,(640,360)))))
    self.assertEqual(uploads,comp.stats['texture_uploads'])
    initial=cp.asnumpy(reader.gpu_image(0,(640,360)));self.assertGreater(int(initial[25:55,20:600].max()),80)
   finally:reader.close()
