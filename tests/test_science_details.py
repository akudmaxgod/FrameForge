import unittest,copy
from imagegen.science_details import prepare
class DetailsTests(unittest.TestCase):
 def fixture(self):
  p={'presentation':'visual_first','asset_keys':['eye'],'layout':'hero_caption','function':'mechanism','focus':'Lens and fibres in relaxed state','support_quote':'The muscle relaxes','annotations':[{'label':'Muscle','x':.2,'y':.3}]}
  return {'props':[{'name':'Атлас / eye','description':'Eye, side section. State: relaxed.','first_appearance':1}],'scenes':[{'n':1,'science_atlas':p},{'n':2,'science_atlas':copy.deepcopy(p)}]}
 def test_reuses_a_real_independent_material_without_paid_calls(self):
  data=self.fixture();r=prepare(data)
  self.assertEqual(len(r['props']),2);self.assertEqual(len(data['props']),1)
  a,b=[s['science_atlas'] for s in r['scenes']]
  self.assertEqual(a['detail_assets'],b['detail_assets']);self.assertNotEqual(a['detail_assets'][0]['key'],'eye')
  self.assertEqual(a['annotation_policy'],'verified_positions_only')
  self.assertIn('relaxed',r['props'][1]['description']);self.assertEqual(len(prepare(r)['props']),2)
 def test_comparison_and_instruction_do_not_get_arbitrary_details(self):
  data=self.fixture();data['scenes'][0]['science_atlas']['layout']='comparison';data['scenes'][1]['science_atlas']['function']='instruction'
  self.assertEqual(len(prepare(data)['props']),1)

 def test_notes_use_structured_state_and_skip_long_narration(self):
  data=self.fixture();data['diagnostics']={'science_atlas':{'assets':[{'key':'eye','identity':'Lens','state':'Flattened'}]}}
  p=prepare(data)['scenes'][0]['science_atlas'];self.assertEqual(p['explanatory_labels'],['Lens','Flattened'])
  data['diagnostics']['science_atlas']['assets'][0]['state']='This is a long narration sentence already spoken by the narrator'
  self.assertEqual(prepare(data)['scenes'][0]['science_atlas']['explanatory_labels'],['Lens'])
 def test_explanatory_labels_render_without_a_detail_or_slide_heading(self):
  from imagegen.science_atlas_render import compile_commands
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','composition_mode':'explanatory','layout':'hero_caption','headline':'Do not draw this','caption':'Do not draw narration','illustrations':[{'asset_id':'a'}],'explanatory_labels':['Lens','Flattened']})
  self.assertEqual([c['text'] for c in p['commands'] if c['type']=='text'],['Lens','Flattened'])
  self.assertEqual(len([c for c in p['commands'] if c['type']=='image']),1)

 def test_redundant_subject_description_is_removed(self):
  from imagegen.science_explanatory import clean_notes
  self.assertEqual(clean_notes(['глазное яблоко человека','целое глазное яблоко с видимыми оболочками']),['глазное яблоко человека'])
  self.assertEqual(clean_notes(['Хрусталик','Хрусталик уплощён']),['Хрусталик','Хрусталик уплощён'])
 def test_source_bound_schemes_make_reused_materials_different(self):
  from imagegen.science_explanatory import infer_scheme,valid_scheme
  a={'support_quote':'Свет попадает в глаза, распознавание требует работы нервных клеток.','focus':'Свет попадает в глаз, распознавание обеспечивается сетью.'}
  b={'support_quote':'Диаметр обычно около двадцати четырёх миллиметров.','focus':'Диаметр глаза взрослого.'}
  self.assertEqual(infer_scheme(a,{})['kind'],'signal');self.assertEqual(infer_scheme(b,{})['labels'][1],'24 мм')
  b['support_quote']='Диаметр обычно около тридцати двух миллиметров.';self.assertEqual(infer_scheme(b,{})['labels'][1],'32 мм')
  self.assertFalse(valid_scheme({'kind':'layers','labels':['a','b','c'],'cue_quote':'absent'},'source'))
 def test_nested_structure_has_source_support_and_no_moving_focus_error(self):
  from imagegen.science_explanatory import infer_scheme
  from imagegen.science_atlas_render import compile_commands,ScienceReader
  source='Глаз в жировой подушке внутри костной ниши.'
  scheme=infer_scheme({'support_quote':source},{'state':'глаз в жировой подушке внутри костной ниши'})
  self.assertEqual(scheme['kind'],'layers');self.assertEqual(scheme['labels'][0],'костная ниша');self.assertEqual(scheme['labels'][2],'глаз');self.assertEqual(scheme['labels'][1],'жировая подушка')
  p=compile_commands({'version':'science-atlas/1','presentation':'visual_first','composition_mode':'explanatory','layout':'hero_caption','illustrations':[{'asset_id':'a'}],'support_quote':source,'explanatory_scheme':scheme})
  self.assertEqual(len([c for c in p['commands'] if c['type']=='circle']),3)
  # Static schematic circles are not timed attention markers.
  from imagegen.science_visual_render import focus_state
  self.assertIsNone(focus_state(p,0))

 def test_observation_can_use_an_explicit_explanatory_material_and_labels(self):
  data=self.fixture();data['props'].append({'name':'Атлас / hidden_structure','description':'An explanatory section'})
  p=data['scenes'][0]['science_atlas'];p.update(function='observation',support_asset_key='hidden_structure',labels=['Hidden structure'])
  result=prepare(data)['scenes'][0]['science_atlas']
  self.assertEqual(result['detail_assets'][0]['key'],'hidden_structure')
  self.assertEqual(result['explanatory_labels'],['Hidden structure'])

 def test_zero_reveal_default_is_compatible_but_real_timing_or_image_change_is_not(self):
  from imagegen.science_atlas_render import snapshot_matches_plan
  old={'commands':[{'type':'image','asset_id':'one','rect':[0,0,100,100]}]}
  current=copy.deepcopy(old);current['commands'][0]['at_ms']=0;current['support_cue_quote']='';current['support_asset_key']=''
  snapshot={'science_source_hash':'legacy','science_atlas':old}
  self.assertTrue(snapshot_matches_plan(snapshot,current))
  current['commands'][0]['at_ms']=1500;self.assertFalse(snapshot_matches_plan(snapshot,current))
  current['commands'][0]['at_ms']=0;current['commands'][0]['asset_id']='another';self.assertFalse(snapshot_matches_plan(snapshot,current))

 def test_long_labels_remain_complete_inside_canvas_bounds(self):
  from imagegen.science_typography import fit_commands
  from imagegen.science_atlas_render import font
  values=['Сверхдлинноеназваниеанатомическойструктурыбезпробелов','Длинная поясняющая подпись к структуре изображения']
  raw=[{'type':'text','text':v,'x':1820,'y':1018,'size':42,'weight':650,'color':'#FFFFFF'} for v in values]
  for source in raw:
   fitted=fit_commands([source],font)
   self.assertEqual(''.join(c['text'].replace(' ','') for c in fitted),source['text'].replace(' ',''))
   for c in fitted:
    bbox=font(c['size'],c['weight']).getbbox(c['text'],anchor='ls')
    self.assertGreaterEqual(c['x']+bbox[0],40);self.assertLessEqual(c['x']+bbox[2],1872);self.assertGreaterEqual(c['y']+bbox[1],20);self.assertLessEqual(c['y']+bbox[3],1060)
