import copy,unittest,tempfile
from pathlib import Path
from imagegen.science_planner import Plan,normalize_relations
from imagegen.science_local_repair import overlay_request
from tests.test_science_quality import data,asset

class LocalRepairTests(unittest.TestCase):
 def test_recovery_cannot_silently_purchase_uncached_following_batches(self):
  from unittest.mock import patch
  from tests.test_science_quality import Client,STORY
  from imagegen.science_planner import stage
  from imagegen.script_analysis.cache import DiskCache
  from imagegen.script_analysis.errors import AnalysisError
  raw=data();raw['shots']=raw['shots'][:1]
  paid=Client([raw,{'issues':[{'n':1,'code':'layout','reason':'Check this scene','correction':'Review locally'}]}]);local=Client([{'issues':[]}])
  with tempfile.TemporaryDirectory() as tmp,patch('imagegen.science_planner.BATCH_SIZE',1):
   cache=DiskCache(Path(tmp));analysis={'characters':[],'scenes':[{'source_quote':STORY}]}
   with self.assertRaises(AnalysisError):stage(analysis,STORY,client=paid,cache=cache)
   with self.assertRaisesRegex(AnalysisError,'нет сохранённого плана'):stage(analysis,STORY,client=paid,local_review=local,cache=cache)
  self.assertEqual(paid.calls,2);self.assertEqual(local.calls,1)
 def test_invalid_plan_does_not_purchase_a_second_generation(self):
  from tests.test_science_quality import Client,STORY
  from imagegen.science_planner import stage
  from imagegen.script_analysis.cache import DiskCache
  from imagegen.script_analysis.errors import AnalysisError
  raw=data();raw['assets'][0]['subject']='График изменения сигнала с подписанными стрелками'
  client=Client([raw])
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(AnalysisError):stage({'characters':[],'scenes':[{'source_quote':STORY}]},STORY,client=client,cache=DiskCache(Path(tmp)))
  self.assertEqual(client.calls,1)
 def test_instrument_pointer_is_not_a_diagram_overlay(self):
  self.assertFalse(overlay_request('Табурет на весах; стрелка циферблата не показывает конкретного числа.'))
  self.assertFalse(overlay_request('Объект без текста, графиков и стрелок.'))
  self.assertTrue(overlay_request('Объект со стрелками движения и графиком измерений.'))
  self.assertTrue(overlay_request('Стрелка циферблата и график массы.'))
 def test_alignment_derives_camera_without_modifying_accepted_assets(self):
  raw=data();other=asset('heated','heated');other['view']='front';other['subject']='A heated convex glass lens with a changed shape';raw['assets'].append(other)
  raw['shots'][0].update(layout='comparison',asset_keys=['lens','heated'],labels=['Cold','Hot'],presentation_motion='staged_explain')
  known={a.key:a for a in Plan.model_validate(raw).assets};before=copy.deepcopy(known)
  fixed=normalize_relations(Plan.model_validate(raw),known=known);shot=fixed.shots[0];catalog={**known,**{a.key:a for a in fixed.assets}}
  self.assertEqual(known,before);self.assertEqual(shot.presentation_motion,'none');self.assertEqual(catalog[shot.asset_keys[0]].view,catalog[shot.asset_keys[1]].view)
  self.assertEqual(catalog[shot.asset_keys[1]].subject,other['subject']);self.assertEqual(catalog[shot.asset_keys[1]].state,other['state'])
  self.assertEqual(normalize_relations(fixed.model_copy(deep=True),known=known),fixed)
 def test_single_asset_and_local_geometry_keep_their_content(self):
  raw=data();raw['shots'][0].update(layout='comparison',labels=['Thicker','Wider'])
  raw['shots'][1].update(layout='local_board',asset_keys=[],presentation_motion='staged_explain',elements=[{'kind':'line','x':.1,'y':.2,'w':.3}])
  fixed=normalize_relations(Plan.model_validate(raw));self.assertEqual(fixed.shots[0].asset_keys,['lens']);self.assertEqual(fixed.shots[0].layout,'hero_caption')
  self.assertEqual(fixed.shots[1].presentation_motion,'none');self.assertEqual(len(fixed.shots[1].elements),1)
