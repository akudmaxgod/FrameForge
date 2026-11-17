import copy,json,tempfile,unittest
from pathlib import Path
from imagegen.science_planner import Asset,Plan,Review,errors,source_groups,stage,PLANNER_VERSION
from imagegen.science_atlas_render import compile_commands,ScienceReader
from imagegen.script_analysis.cache import DiskCache

STORY='A lens focuses light onto a sensor, and the sensor records a signal.\n\nA copper sphere is heated and expands while its material remains copper.'
def asset(key='lens',state='rest'):return {'key':key,'subject':'A convex glass lens viewed from the side, intact edges','identity':'glass lens','view':'side, same scale','state':state,'people':False}
def shot(n,quote,**kwargs):return {'n':n,'layout':'hero_caption','function':'structure','representation':'literal','focus':'The convex lens focuses the incoming light','support_quote':quote,'headline':'The lens focuses light','caption':'','asset_keys':['lens'],**kwargs}
def data():return {'assets':[asset()], 'shots':[shot(i+1,STORY[a:b]) for i,(a,b) in enumerate(source_groups(STORY))]}
class Client:
 def __init__(self,items):self.items=list(items);self.calls=0
 def ask_json(self,messages,schema,*args,**kwargs):self.calls+=1;return schema.model_validate(self.items.pop(0))
class ScienceQualityTests(unittest.TestCase):
 def test_sentence_partition_keeps_quotes_and_concepts(self):
  story='Он изучал картину «Не ждали». Поглощение фотона меняет молекулу.\n\nСледующий опыт нужен для проверки.';spans=source_groups(story)
  self.assertTrue(any('«Не ждали»' in story[a:b] for a,b in spans));self.assertTrue(any('Поглощение фотона' in story[a:b] for a,b in spans))
  covered=''.join(story[a:b] for a,b in spans);self.assertEqual(''.join(covered.split()),''.join(story.split()))
 def check(self,raw):
  frames=[{'n':i+1,'narration':STORY[a:b]} for i,(a,b) in enumerate(source_groups(STORY))]
  return errors(Plan.model_validate(raw),frames,{}, {},visual_first=False)[0]
 def test_same_image_or_same_state_cannot_be_comparison(self):
  raw=data();raw['shots'][0].update(layout='comparison',asset_keys=['lens','lens'],labels=['Cold','Hot']);self.assertTrue(self.check(raw))
  raw['assets'].append(asset('lens2'));raw['shots'][0]['asset_keys']=['lens','lens2'];self.assertTrue(self.check(raw))
  raw['assets'][1]['state']='heated';raw['assets'][1]['subject']='A convex glass lens with altered curvature viewed from the side';self.assertFalse(self.check(raw))
 def test_cues_and_edges_must_be_supported_locally(self):
  raw=data();raw['shots'][0].update(layout='process',asset_keys=[],relation='sequence',nodes=[{'label':'Lens','cue_quote':'lens'},{'label':'Signal','cue_quote':'signal'}],edges=[{'source':0,'target':1,'evidence_quote':'not present'}]);self.assertTrue(self.check(raw))
  raw['shots'][0]['edges'][0]['evidence_quote']=raw['shots'][0]['support_quote'];self.assertFalse(self.check(raw))
  raw['shots'][0]['function']='factors';self.assertTrue(self.check(raw))
 def test_parallel_factors_have_no_connecting_arrows(self):
  base={'version':'science-atlas/1','planner_version':PLANNER_VERSION,'layout':'factors','headline':'Independent factors','caption':'','relation':'parallel','nodes':[{'label':'Material'},{'label':'Shape'}],'edges':[]}
  plan=compile_commands(base);self.assertEqual(len([c for c in plan['commands'] if c['type']=='line']),1)
  base.update(layout='process',relation='sequence',edges=[{'source':0,'target':1}]);plan=compile_commands(base);self.assertEqual(len([c for c in plan['commands'] if c['type']=='line']),4)
 def test_local_test_contains_real_words_and_marks_without_flow(self):
  base={'version':'science-atlas/1','planner_version':PLANNER_VERSION,'layout':'local_board','headline':'Fixation experiment','caption':'','nodes':[],'elements':[{'kind':'text','x':.2,'y':.2,'w':.3,'text':'TARGET'},{'kind':'cross','x':.25,'y':.5,'w':.02},{'kind':'dot','x':.75,'y':.5,'w':.02}]}
  plan=compile_commands(base);self.assertIn('TARGET',[c['text'] for c in plan['commands'] if c['type']=='text']);self.assertFalse(any(c['type']=='image' for c in plan['commands']))
  r=ScienceReader(plan,lambda aid:self.fail('No external illustration needed'))
  try:self.assertEqual(r.image(0,(640,360)).size,(640,360))
  finally:r.close()
 def test_semantic_review_repairs_before_caching_or_applying(self):
  raw=data();fixed=copy.deepcopy(raw);fixed['shots'][0]['headline']='Light reaches the sensor';fixed['shots']=fixed['shots'][:1]
  c=Client([raw,{'issues':[{'n':1,'code':'meaning','reason':'Focus missing from headline','correction':'Name the recorded signal'}]},fixed,{'issues':[]}])
  analysis={'characters':[],'scenes':[{'source_quote':STORY,'source_start':0,'source_end':len(STORY)}]}
  before=copy.deepcopy(analysis)
  with tempfile.TemporaryDirectory() as temp:
   result=stage(analysis,STORY,client=c,cache=DiskCache(Path(temp)));self.assertEqual(c.calls,4);self.assertEqual(analysis,before);self.assertTrue(result['diagnostics']['science_atlas']['reviewed']);self.assertEqual(result['scenes'][0]['science_atlas']['headline'],'Light reaches the sensor')
   again=stage(analysis,STORY,client=c,cache=DiskCache(Path(temp)));self.assertEqual(c.calls,4);self.assertEqual(result['scenes'],again['scenes'])
 def test_spatial_review_uses_actual_signed_arrow_endpoints(self):
  from imagegen.science_planner import review_shot,Shot
  s=Shot.model_validate(shot(1,'A lens',layout='local_board',asset_keys=[],elements=[{'kind':'text','x':.05,'y':.1,'w':.2,'text':'left field'},{'kind':'text','x':.7,'y':.1,'w':.2,'text':'right field'},{'kind':'text','x':.05,'y':.8,'w':.2,'text':'left hemisphere'},{'kind':'text','x':.7,'y':.8,'w':.2,'text':'right hemisphere'},{'kind':'arrow','x':.2,'y':.15,'w':.55,'h':.7},{'kind':'arrow','x':.8,'y':.15,'w':-.6,'h':.7}]))
  links=review_shot(s)['rendered_arrow_connections'];self.assertEqual([(v['from_label'],v['to_label']) for v in links],[('left field','right hemisphere'),('right field','left hemisphere')])
 def test_unresolved_review_has_one_repair_and_no_paid_arbitration(self):
  from imagegen.script_analysis.errors import AnalysisError
  raw=data();part=copy.deepcopy(raw);part['shots']=part['shots'][:1]
  issue={'issues':[{'n':1,'code':'meaning','reason':'Needs correction','correction':'Repair just this shot'}]}
  c=Client([raw,issue,part,issue])
  with tempfile.TemporaryDirectory() as temp:
   with self.assertRaises(AnalysisError):stage({'characters':[],'scenes':[{'source_quote':STORY}]},STORY,client=c,cache=DiskCache(Path(temp)))
   self.assertEqual(c.calls,4)
   draft=next(Path(temp).glob('science-v2-draft-*.json'));saved=json.loads(draft.read_text(encoding='utf-8'))
   self.assertEqual(len(saved['accepted_shots']),1)

 def test_directional_arrows_can_point_left_without_negative_box_sizes(self):
  raw=data();q=raw['shots'][0]['support_quote'];raw['shots'][0].update(layout='local_board',asset_keys=[],elements=[{'kind':'arrow','x':.8,'y':.7,'w':-.5,'h':-.4,'text':''}]);self.assertFalse(self.check(raw))
  raw['shots'][0]['elements'][0]['kind']='box';self.assertTrue(self.check(raw))
 def test_text_contrast_respects_a_white_local_board(self):
  plan=compile_commands({'version':'science-atlas/1','layout':'local_board','headline':'Paper','caption':'','elements':[{'kind':'box','x':.1,'y':.1,'w':.7,'h':.7,'fill':True,'color':'#FFFFFF'},{'kind':'text','x':.2,'y':.2,'w':.4,'text':'Visible label'}]})
  label=next(c for c in plan['commands'] if c.get('text')=='Visible label');self.assertEqual(label['color'],'#09161A')
 def test_local_color_samples_render_as_colors(self):
  p=compile_commands({'version':'science-atlas/1','planner_version':PLANNER_VERSION,'layout':'local_board','headline':'Qualitative color illustration','caption':'','nodes':[],'elements':[{'kind':'box','x':.1,'y':.1,'w':.2,'h':.2,'color':'#C04040','fill':True}]})
  r=ScienceReader(p,lambda aid:None)
  try:self.assertEqual(r.image(0,(640,360)).getpixel((120,150)),(192,64,64))
  finally:r.close()
 def test_rebuild_archives_automatic_cards_and_preserves_assets(self):
  from imagegen.service import Service
  from imagegen.worker import Worker
  from imagegen.science_atlas import stage_legacy
  from imagegen.science_atlas_render import assemble_project
  from tests.test_science_atlas import STORY as SOURCE,analysis,plan,Client as OldClient
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as temp:
   service=Service(Path(temp)/'data');p=service.create_project({'title':'Scientific archive','story':SOURCE,'style':'test','visual_format':'science_atlas'})
   prepared=stage_legacy(analysis(),SOURCE,client=OldClient([plan()]),cache=DiskCache(Path(temp)/'cache'));p=service.import_analysis(p['id'],prepared,p['version']);assemble_project(service,p['id']);old_scene=service.scene(p['scenes'][0]['id']);old_gid=old_scene['active_generation_id'];old_asset=service.db.one('SELECT asset_id FROM generations WHERE id=?',(old_gid,))['asset_id']
   current=service.project(p['id'])
   with service.db.transaction() as c:gid=service.insert_generation(c,p['id'],'analysis',{'project':current,'profile':service.profile(),'analysis_rebuild':True})
   generation=service.db.one('SELECT * FROM generations WHERE id=?',(gid,))
   with patch('imagegen.science_atlas.stage',return_value=prepared):Worker(service)._save_analysis(generation,analysis(),current['version'])
   archive=json.loads(service.project(p['id'])['analysis_metadata'])['diagnostics']['science_atlas']['previous_plan_archive_id']
   self.assertEqual(service.db.one('SELECT project_id FROM generations WHERE id=?',(old_gid,))['project_id'],archive);self.assertEqual(service.asset(old_asset)['project_id'],archive);self.assertTrue((service.root/service.asset(old_asset)['path']).is_file());self.assertTrue(service.db.one('SELECT * FROM project_trash WHERE project_id=?',(archive,)))
 def test_rebuild_will_not_replace_an_existing_montage(self):
  from imagegen.service import Service
  from imagegen.science_plan_store import require_replaceable
  from imagegen.domain import DomainError
  from imagegen.db import dump,now
  with tempfile.TemporaryDirectory() as temp:
   service=Service(Path(temp));p=service.create_project({'title':'Keep edit','story':STORY,'style':'test','visual_format':'science_atlas'})
   with service.db.transaction() as c:c.execute('INSERT INTO project_edits VALUES(?,1,1,?,?)',(p['id'],dump({}),now()))
   with service.db.transaction() as c:
    with self.assertRaises(DomainError):require_replaceable(service,c,p['id'])
 def test_image_material_cannot_require_model_drawn_graph(self):
  raw=data();raw['assets'][0]['subject']='График изменения сигнала с подписанными стрелками';self.assertTrue(self.check(raw))
if __name__=='__main__':unittest.main()
