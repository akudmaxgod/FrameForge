import copy,json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from imagegen.science_sequence import arrange_scenes,MODES
from imagegen.science_slide_reveal import state,command_state
from imagegen.science_atlas_render import compile_commands,ScienceReader
from tests.test_science_slide_reveal import fixture

def sequence():
 rows=[]
 for i in range(36):
  p=fixture();p.update(frame_density='filled',asset_keys=[f'asset{i//2}'],function='mechanism',focus='Передача сигнала и изменение состояния',detail_assets=[])
  if i%7==0:p['detail_assets']=[{'key':'detail'}];p['explanatory_scheme']=None
  if i%9==0:p['function']='observation'
  rows.append({'position':i+1,'data':{'science_atlas':p,'prompt_timing':{'duration_seconds':6},'source_start':i*20,'source_end':i*20+20,'animation':{'selected_engine':'ken_burns'}}})
 return rows

class SequenceTests(unittest.TestCase):
 def test_shared_analysis_policy_is_story_independent_and_idempotent(self):
  from imagegen.science_sequence import arrange_analysis
  for topic in ('space','chemistry','engineering'):
   rows=sequence()
   for i,row in enumerate(rows):row['data']['science_atlas']['asset_keys']=[f'{topic}_{i}']
   analysis={'scenes':rows,'world':topic};before=copy.deepcopy(analysis)
   result=arrange_analysis(analysis)
   self.assertEqual(analysis,before)
   self.assertGreaterEqual(len(result['diagnostics']['science_atlas']['sequence_arrangement']['counts']),3)
   self.assertEqual(result,arrange_analysis(result))
 def test_reveals_are_spaced_and_other_modes_are_present(self):
  rows=sequence();before=copy.deepcopy(rows);result,report=arrange_scenes(rows)
  self.assertEqual(rows,before);positions=report['reveal_positions'];self.assertGreater(len(positions),1)
  self.assertTrue(all(b-a>=3 for a,b in zip(positions,positions[1:])));self.assertLessEqual(len(positions),9)
  self.assertGreaterEqual(len(report['counts']),3)
 def test_timing_assets_and_source_are_unchanged(self):
  before=sequence();after,_=arrange_scenes(before)
  for a,b in zip(before,after):
   self.assertEqual(a['data']['prompt_timing'],b['data']['prompt_timing']);self.assertEqual(a['data']['source_start'],b['data']['source_start'])
   for key in ('asset_keys','explanatory_labels','explanatory_scheme','detail_assets'):self.assertEqual(a['data']['science_atlas'][key],b['data']['science_atlas'][key])
 def test_manual_choices_native_tests_and_cloud_video_are_preserved(self):
  rows=sequence();rows[0]['data']['science_atlas'].update(presentation_locked=True,presentation_motion='focus_explain')
  rows[1]['data']['animation']['selected_engine']='omni_flash';rows[2]['data']['science_atlas'].update(layout='comparison',asset_keys=['a','b'],presentation_motion='none')
  after,_=arrange_scenes(rows)
  for i in range(3):self.assertEqual(after[i],rows[i])
 def test_reapplying_arrangement_is_idempotent(self):
  a,_=arrange_scenes(sequence());b,_=arrange_scenes(a);self.assertEqual(a,b)
 def test_fixed_variants_do_not_slide_the_picture(self):
  for mode in ('staged_explain','detail_explain'):
   p=fixture();p['presentation_motion']=mode;p=compile_commands(p)
   self.assertEqual(state(p,.5)['rect'],state(p,4)['rect'])
  p=fixture();p['presentation_motion']='slide_explain';p=compile_commands(p);self.assertNotEqual(state(p,.5)['rect'],state(p,4)['rect'])
 def test_focus_push_in_never_exceeds_the_fitted_group(self):
  p=fixture();p['presentation_motion']='focus_explain';p=compile_commands(p)
  for t in (0,1,3,6):
   s=state(p,t);self.assertLessEqual(s['rect'][2],s['final_rect'][2]);self.assertLessEqual(s['rect'][3],s['final_rect'][3])
 def test_python_and_browser_variants_match(self):
  cases=[]
  for mode in MODES:
   for duration in (.5,2,6):
    for direction in ('left','right'):
     p=fixture(direction,duration);p['presentation_motion']=mode;p=compile_commands(p)
     for seconds in (.05,.5,duration-.01):cases.append({'p':p,'t':seconds,'s':state(p,seconds),'c':[command_state(p,c,seconds) for c in p['commands']]})
  code="""import fs from 'node:fs';import {slideState,slideCommandState} from './imagegen/static/science-slide-reveal.mjs';const eq=(a,b)=>{if(typeof a==='number'){if(Math.abs(a-b)>1e-7)throw Error(`${a} != ${b}`);}else if(Array.isArray(a))a.forEach((x,i)=>eq(x,b[i]));else Object.keys(a).forEach(k=>eq(a[k],b[k]));};for(const x of JSON.parse(fs.readFileSync(0,'utf8'))){eq(x.s,slideState(x.p,x.t));eq(x.c,x.p.commands.map(c=>slideCommandState(x.p,c,x.t)));}"""
  r=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(cases),text=True,capture_output=True);self.assertEqual(r.returncode,0,r.stderr)
 def test_every_variant_uses_retained_cuda_textures(self):
  from imagegen.gpu_pixels import cuda
  cp=cuda()
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'image.png';Image.new('RGB',(100,100),'#37575a').save(path)
   for mode in MODES:
    p=fixture();p['presentation_motion']=mode;reader=ScienceReader(compile_commands(p),lambda _:path)
    try:
     comp=reader.compositor((640,360));uploads=comp.stats['texture_uploads']
     with patch('PIL.ImageDraw.ImageDraw.text',side_effect=AssertionError('frame rasterization')),patch.object(cp,'asnumpy',side_effect=AssertionError('RGB download')):
      for t in (.4,2,5,.4):reader.gpu_image(t,(640,360))
      cp.cuda.get_current_stream().synchronize()
     self.assertEqual(comp.stats['texture_uploads'],uploads);self.assertGreater(comp.stats['cuda_composite_calls'],0)
    finally:reader.close()

if __name__=='__main__':unittest.main()
