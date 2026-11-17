import copy,unittest
from unittest.mock import Mock
from imagegen.science_plan_reuse import current_density,find,build,verify
from imagegen.science_explanatory import clean_notes
from imagegen.domain import DomainError
from tests.test_science_slide_reveal import fixture

def source():
 p=fixture();p.update(frame_density='filled',asset_keys=['test'],density_upgrade_version=1)
 return {'id':'source','version':3,'story':'source text','world':'science','characters':[],'props':[],
 'preflight':{'ok':True},'scenes':[{'data':{'science_atlas':p,'source_quote':'source text','source_start':0,'source_end':11}}],
 'analysis_metadata':{'diagnostics':{'science_atlas':{'version':'science-staging/3','complete':True,'reviewed':True}}}}

class ReuseDensityTests(unittest.TestCase):
 def test_legacy_plan_cannot_be_reused_as_a_current_plan(self):
  s=source();s['scenes'][0]['data']['science_atlas'].pop('frame_density')
  self.assertFalse(current_density(s))
  with self.assertRaisesRegex(DomainError,'старые правила'):build(s)
 def test_find_skips_legacy_and_selects_compatible_source(self):
  old=source();old['id']='old';old['scenes'][0]['data']['science_atlas'].pop('frame_density')
  new=source();service=Mock();service.db.all.return_value=[{'id':'old'},{'id':'source'}];service.detail.side_effect=lambda sid:old if sid=='old' else new
  result=find(service,{'id':'target','story':'source text'});self.assertEqual(result['_verified_science_reuse']['source_project_id'],'source');self.assertEqual(result['deepseek_usage']['cost_usd'],0)
 def test_reuse_preserves_editorial_notes_and_new_motion(self):
  s=source();original=copy.deepcopy(s);result=build(s)
  self.assertEqual(s,original);self.assertEqual(result['scenes'][0]['science_atlas']['explanatory_labels'],s['scenes'][0]['data']['science_atlas']['explanatory_labels']);self.assertEqual(result['scenes'][0]['science_atlas']['presentation_motion'],'slide_explain')
 def test_source_plan_changes_are_detected_even_when_project_version_stays_same(self):
  s=source();result=build(s);s['scenes'][0]['data']['science_atlas']['explanatory_labels']=['changed','other caption'];service=Mock();service.detail.return_value=s
  with self.assertRaisesRegex(ValueError,'изменился'):verify(service,{'story':'source text'},result)
 def test_material_preparation_does_not_discard_reviewed_captions(self):
  from imagegen.science_details import prepare
  s=source();p=s['scenes'][0]['data']['science_atlas'];p.update(annotations=[],function='structure',labels=[],explanatory_labels=['Прозрачные среды','Нервная ткань'])
  analysis={'props':[],'scenes':[{'science_atlas':p}]};result=prepare(analysis)
  self.assertEqual(result['scenes'][0]['science_atlas']['explanatory_labels'],['Прозрачные среды','Нервная ткань'])
 def test_opposite_directed_relations_are_not_deduplicated(self):
  notes=['Левое поле → правое полушарие','Правое поле → левое полушарие'];self.assertEqual(clean_notes(notes),notes)
 def test_raw_current_plan_passes_preflight_without_resolved_asset_ids(self):
  from imagegen.science_planner import project_preflight,PLANNER_VERSION
  from tests.test_science_quality import asset,shot,STORY,source_groups
  a,b=source_groups(STORY)[0];quote=STORY[a:b];p=shot(1,quote,frame_density='filled',labels=['Glass lens','Focused signal']);p.update(version='science-atlas/1',presentation='visual_first',composition_mode='explanatory')
  project={'story':quote,'characters':[],'scenes':[{'data':{'science_atlas':p,'source_quote':quote,'source_start':0,'source_end':len(quote)}}],
  'analysis_metadata':{'diagnostics':{'science_atlas':{'version':PLANNER_VERSION,'reviewed':True,'assets':[asset()]}}}}
  result=project_preflight(project);self.assertTrue(result['ok'],result)
  p['labels']=[];result=project_preflight(project);self.assertFalse(result['ok'])
 def test_existing_fixation_test_is_not_rejected_for_having_only_one_mark(self):
  from imagegen.science_atlas_render import compile_commands
  p={'frame_density':'filled','version':'science-atlas/1','presentation':'visual_first','layout':'instruction','asset_keys':[],
  'elements':[{'kind':'text','x':.3,'y':.4,'w':.2,'text':'СЛОВО'},{'kind':'line','x':.3,'y':.5,'w':.1,'h':0}]}
  result=compile_commands(p);self.assertEqual(len(result['commands']),2)

if __name__=='__main__':unittest.main()
