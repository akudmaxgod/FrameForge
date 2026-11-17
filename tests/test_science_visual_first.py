import copy,unittest
from imagegen.science_atlas_render import compile_commands,ScienceReader
from imagegen.science_planner import Plan,errors,Shot,review_shot,PLANNER_VERSION
from tests.test_science_quality import data,STORY,source_groups
class VisualFirstTests(unittest.TestCase):
 def test_no_narration_heading_or_caption_is_drawn(self):
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'hero_caption','headline':'This is narration','caption':'The entire paragraph is already spoken','illustrations':[{'asset_id':'asset'}]})
  self.assertFalse(any(c['type']=='text' for c in p['commands']));self.assertEqual(p['commands'][0]['rect'],[32,20,1856,1040])
 def test_annotation_stays_on_a_square_image_in_a_wide_frame(self):
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'hero_caption','illustrations':[{'asset_id':'a','width':1000,'height':1000}],'annotations':[{'label':'','x':.2,'y':.4}]})
  ring=next(c for c in p['commands'] if c['type']=='ring');self.assertEqual(ring['x'],648);self.assertEqual(ring['y'],436)
 def test_attention_motion_is_smooth_and_frame_cache_is_bounded(self):
  from imagegen.science_visual_render import focus_state
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'hero_caption','annotations':[{'label':'','x':.2,'y':.4},{'label':'','x':.8,'y':.4}],'cue_ms':[0,1000]})
  state=focus_state(p,1.175);self.assertAlmostEqual(state['x'],960,places=5);self.assertEqual(state['phase'],.5)
  r=ScienceReader(p,lambda _:None)
  try:
   for i in range(24):r.image(1+i/60,(320,180))
   self.assertLessEqual(len(r.frames),8)
  finally:r.close()
 def test_explanation_uses_independent_detail_and_no_unverified_leaders(self):
  import tempfile
  from pathlib import Path
  from PIL import Image
  with tempfile.TemporaryDirectory() as temp:
   paths={}
   for key,color in [('a','red'),('detail','blue')]:
    paths[key]=Path(temp)/(key+'.png');Image.new('RGB',(100,100),color).save(paths[key])
   base={'version':'science-atlas/1','presentation':'visual_first','composition_mode':'explanatory','layout':'hero_caption','illustrations':[{'asset_id':'a','width':100,'height':100}],'annotations':[{'label':'Muscle','x':.25,'y':.5}],'annotation_policy':'verified_positions_only'}
   missing=compile_commands(base)
   self.assertEqual(len([c for c in missing['commands'] if c['type']=='image']),1)
   self.assertFalse(any(c['type'] in ('ring','text','line') for c in missing['commands']))
   plan=compile_commands({**base,'details':[{'asset_id':'detail'}]})
   images=[c for c in plan['commands'] if c['type']=='image']
   self.assertEqual([c['asset_id'] for c in images],['a','detail']);self.assertFalse(any('crop' in c for c in images))
   plan=compile_commands({**base,'details':[{'asset_id':'detail'}],'support_reveal_ms':1500})
   r=ScienceReader(plan,lambda aid:paths[aid])
   try:
    self.assertEqual(r.image(0,(1920,1080)).getpixel((1550,540)),(9,22,26))
    self.assertEqual(r.image(2,(1920,1080)).getpixel((1550,540)),(0,0,255))
   finally:r.close()
 def test_scientific_camera_and_network_are_visibly_animated(self):
  import tempfile
  from pathlib import Path
  from PIL import Image,ImageDraw
  from imagegen.science_explanatory import draw_scheme
  from imagegen.science_motion import typed_text
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'image.png';im=Image.new('RGB',(100,100),'navy');d=ImageDraw.Draw(im)
   for x in range(0,100,10):d.line((x,0,x,99),fill='ivory',width=3)
   im.save(path)
   plan=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'hero_caption','illustrations':[{'asset_id':'a','width':100,'height':100}]})
   r=ScienceReader(plan,lambda _:path)
   try:
    self.assertNotEqual(r.image(0,(320,180)).tobytes(),r.image(5,(320,180)).tobytes())
    self.assertEqual(r.image(0,(320,180),preview=True).tobytes(),r.image(5,(320,180),preview=True).tobytes())
   finally:r.close()
  commands=draw_scheme({'kind':'network','labels':['Neural network','Signal']},[1280,230,570,570])
  r=ScienceReader({'commands':commands},lambda _:None)
  try:self.assertNotEqual(r.image(0,(640,360)).tobytes(),r.image(.4,(640,360)).tobytes())
  finally:r.close()
  cmd={'text':'Затылочная область','typing_cps':10}
  self.assertEqual(typed_text(cmd,0),'');self.assertEqual(typed_text(cmd,.3),'Зат');self.assertEqual(typed_text(cmd,0,True),cmd['text'])
 def test_existing_measurement_plan_draws_and_types_without_camera_movement(self):
  from imagegen.science_explanatory import draw_scheme
  from imagegen.science_motion import playback_commands,typed_text,draw_progress
  scheme={'kind':'measurement','labels':['Диаметр','24 мм']}
  raw=draw_scheme(scheme,[1280,230,570,570]);plan={'presentation':'visual_first','explanatory_scheme':scheme,'commands':raw}
  commands=playback_commands(plan)
  self.assertEqual(len(commands),len(raw)+4)
  number=next(c for c in commands if c.get('text')=='24 мм')
  self.assertEqual(typed_text(number,1),'');self.assertEqual(typed_text(number,3),'24 мм')
  circle=next(c for c in commands if c['type']=='ring')
  self.assertEqual(draw_progress(circle,.1),0);self.assertGreater(draw_progress(circle,.8),0);self.assertEqual(draw_progress(circle,3),1)
  r=ScienceReader(plan,lambda _:None)
  try:self.assertNotEqual(r.image(.1,(640,360)).tobytes(),r.image(3,(640,360)).tobytes())
  finally:r.close()
  self.assertFalse(any(c.get('draw_ms') or c.get('typing_cps') for c in raw))
 def test_only_short_labels_and_comparison_states_remain(self):
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'comparison','headline':'Spoken thesis','caption':'Spoken explanation','labels':['РќР° СЃРІРµС‚Сѓ','Р’ С‚РµРјРЅРѕС‚Рµ'],'illustrations':[{'asset_id':'a'},{'asset_id':'b'}]})
  self.assertEqual([c['text'] for c in p['commands'] if c['type']=='text'],['РќР° СЃРІРµС‚Сѓ','Р’ С‚РµРјРЅРѕС‚Рµ'])
 def test_highlights_are_retained_and_have_exact_cue_indices(self):
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','layout':'hero_caption','headline':'Hidden','caption':'Hidden','nodes':[],'elements':[],'annotations':[{'x':.2,'y':.5,'label':'Р РѕРіРѕРІРёС†Р°'},{'x':.6,'y':.5,'label':''}],'cue_ms':[0,1000]})
  self.assertEqual([c['cue_index'] for c in p['commands'] if c['type']=='ring'],[0,1]);r=ScienceReader(p,lambda _:None)
  try:
   before=r.image(0,(640,360));after=r.image(2,(640,360));self.assertNotEqual(before.tobytes(),after.tobytes());self.assertEqual(after.getpixel((128,180)),(9,22,26))
  finally:r.close()
 def test_slide_layout_is_rejected_and_source_label_is_bound(self):
  raw=data();frames=[{'n':i+1,'narration':STORY[a:b]} for i,(a,b) in enumerate(source_groups(STORY))]
  raw['shots'][0].update(layout='text_card',asset_keys=[]);self.assertTrue(errors(Plan.model_validate(raw),frames,{}, {})[0])
  raw=data();raw['shots'][0]['annotations']=[{'label':'A complete narrated sentence repeated here','x':.2,'y':.5,'cue_quote':'absent'}];issues,_=errors(Plan.model_validate(raw),frames,{}, {});self.assertGreaterEqual(len(issues),2)
 def test_experiment_text_is_not_a_paragraph(self):
  raw=data();raw['shots'][0].update(layout='local_board',asset_keys=[],elements=[{'kind':'text','x':.1,'y':.1,'w':.8,'text':'This is a full repeated paragraph of the narration that has no visual value at all'},{'kind':'dot','x':.2,'y':.5}]);frames=[{'n':i+1,'narration':STORY[a:b]} for i,(a,b) in enumerate(source_groups(STORY))];self.assertTrue(errors(Plan.model_validate(raw),frames,{}, {})[0])
if __name__=='__main__':unittest.main()
