import copy,unittest
from imagegen.science_atlas_render import compile_commands,font
from imagegen.science_density import command_bounds
from imagegen.domain import DomainError
from tests.test_science_slide_reveal import fixture

class DensityTests(unittest.TestCase):
 def test_an_empty_explanation_is_an_error_not_an_empty_column(self):
  p=fixture();p.update(explanatory_labels=[],annotations=[],explanatory_scheme=None)
  with self.assertRaisesRegex(DomainError,'два разных пояснения'):compile_commands(p)
 def test_repeating_the_object_name_is_not_a_filled_frame(self):
  p=fixture();p.update(explanatory_labels=['Общий вид','Общий вид'],annotations=[],explanatory_scheme=None)
  with self.assertRaises(DomainError):compile_commands(p)
 def test_explicit_filled_policy_activates_the_retained_compositor(self):
  p=fixture();p.pop('presentation_motion');p['frame_density']='filled';original=copy.deepcopy(p)
  result=compile_commands(p);self.assertEqual(p,original);self.assertEqual(result['presentation_motion'],'staged_explain');self.assertEqual(result['slide_contract_version'],2)
 def test_a_single_subject_requires_a_specific_reason(self):
  p=fixture();p.update(frame_density='single_subject',annotations=[],explanatory_labels=[],explanatory_scheme=None)
  with self.assertRaisesRegex(DomainError,'причину'):compile_commands(p)
  p['single_subject_reason']='Нужно целиком рассмотреть контур органа перед сравнением с другим ракурсом.'
  result=compile_commands(p);self.assertEqual(result['presentation_motion'],'none');self.assertEqual([c['type'] for c in result['commands']],['image'])
 def test_a_square_illustration_uses_the_available_height_in_both_directions(self):
  for direction in ('left','right'):
   p=fixture(direction);p['annotations']=[];p=compile_commands(p)
   box=p['slide_layout']['final_rect'];self.assertGreaterEqual(box[2],900);self.assertGreaterEqual(box[3],900)
   self.assertGreaterEqual(box[0],63);self.assertGreaterEqual(box[1],71);self.assertLessEqual(box[0]+box[2],1857);self.assertLessEqual(box[1]+box[3],1009)
 def test_two_verified_labels_can_fill_a_full_width_illustration(self):
  p=fixture();p.update(explanatory_labels=[],explanatory_scheme=None,annotations=[{'x':.3,'y':.3,'label':'Первая часть','position_verified':True},{'x':.7,'y':.7,'label':'Вторая часть','position_verified':True}])
  p=compile_commands(p);self.assertFalse(p['slide_layout']['has_support_column']);self.assertFalse(any(c.get('motion_group')=='support' for c in p['commands']))
 def test_annotation_text_does_not_replace_the_explanatory_scheme(self):
  p=compile_commands(fixture());support=[c for c in p['commands'] if c.get('motion_group')=='support']
  self.assertTrue(any(c['type']=='ring' for c in support));self.assertFalse(any(c.get('text')=='Метка' for c in support))
 def test_raw_filled_scene_export_uses_the_requested_duration(self):
  import tempfile
  from pathlib import Path
  from PIL import Image
  from unittest.mock import patch
  import av
  from imagegen.science_atlas_render import ScienceReader,render_video
  p=fixture();p.pop('illustration_motion');p.pop('presentation_motion');p['frame_density']='filled';readers=[]
  with tempfile.TemporaryDirectory() as directory:
   image=Path(directory)/'source.png';Image.new('RGB',(100,100),'#37565c').save(image)
   def make_reader(plan,resolve):
    reader=ScienceReader(plan,resolve);readers.append(reader);return reader
   output=Path(directory)/'scene.mp4'
   with patch('imagegen.science_atlas_render.ScienceReader',side_effect=make_reader):meta=render_video(p,lambda _:image,output,duration=.5,target_size=(640,360))
   self.assertEqual(meta['renderer_version'],7);self.assertEqual(readers[0].plan['illustration_motion']['duration_seconds'],.5)
   with av.open(str(output)) as container:self.assertEqual(len(list(container.decode(video=0))),12)
 def test_new_planner_policy_requires_content_or_justified_solo(self):
  from imagegen.science_planner import Plan,errors
  from tests.test_science_quality import data,STORY,source_groups
  raw=data();raw['shots'][0].update(frame_density='filled',labels=[],explanatory_scheme=None,support_asset_key='')
  frames=[{'n':i+1,'narration':STORY[a:b]} for i,(a,b) in enumerate(source_groups(STORY))]
  issues,_=errors(Plan.model_validate(raw),frames,{},{});self.assertTrue(any('Наполненный кадр' in str(i) for i in issues))
  raw['shots'][0].update(frame_density='single_subject',single_subject_reason='');issues,_=errors(Plan.model_validate(raw),frames,{},{});self.assertTrue(any('обоснования' in str(i) for i in issues))

if __name__=='__main__':unittest.main()
