import copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from imagegen.service import Service
from imagegen.science_atlas import stage_legacy as stage,Plan,VERSION,asset_name
from imagegen.science_atlas_render import assemble_project,hydrate,ScienceReader,compile_commands
from imagegen.script_analysis.cache import DiskCache
from imagegen.editing import Editing

STORY='Свет проходит через роговицу и зрачок. Зрачок меняет размер.'
def analysis():
 quotes=STORY.split('. ');quotes[0]+='.'
 return {'world':'Science','characters':[],'locations':[],'props':[], 'scenes':[
  {'beat':q,'source_quote':q,'source_start':STORY.index(q),'source_end':STORY.index(q)+len(q),'characters':[], 'wardrobe':{},'location':'','time':'day','shot':'medium','emotion':'neutral','continuity_before':'','continuity_after':'','visible_people':0,'props':[]} for q in quotes]}
def shot(n,layout,assets,nodes=None):return {'n':n,'layout':layout,'function':'explanation','representation':'schematic','headline':'Как проходит свет' if n==1 else 'Размер зрачка меняется','caption':'','asset_keys':assets,'labels':['Узкий','Широкий'] if layout=='comparison' else [],'nodes':nodes or [],'cast':[],'roles':[]}
class Client:
 def __init__(self,plans):self.plans=list(plans);self.calls=0
 def ask_json(self,*args,**kwargs):self.calls+=1;return Plan.model_validate(self.plans.pop(0))
def plan():return {'assets':[{'key':'small','subject':'Eye frontal narrow pupil'},{'key':'large','subject':'Eye frontal large pupil'}], 'shots':[shot(1,'process',[],[{'label':'Роговица','cue_quote':'роговицу'},{'label':'Зрачок','cue_quote':'зрачок'}]),shot(2,'comparison',['small','large'])]}
class ScienceAtlasTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def staged(self,client=None):return stage(analysis(),STORY,client=client or Client([plan()]),cache=DiskCache(self.root/'cache'))
 def test_source_cues_assets_and_no_mutation(self):
  a=analysis();b=self.staged();self.assertNotIn('science_atlas',a['scenes'][0]);self.assertEqual(b['scenes'][0]['science_atlas']['nodes'][0]['source_char_start'],STORY.index('роговицу'));self.assertEqual(len(b['props']),2);self.assertEqual(b['scenes'][1]['source_quote'],a['scenes'][1]['source_quote'])
 def test_cache_and_idempotence(self):
  c=Client([plan()]);b=self.staged(c);self.staged(c);self.assertEqual(c.calls,1);self.assertIs(stage(b,STORY,client=c),b)
 def test_repairs_missing_shot(self):
  bad=plan();bad['shots']=bad['shots'][:1];c=Client([bad,plan()]);self.staged(c);self.assertEqual(c.calls,2)
 def test_pipeline_and_reusable_assets(self):
  service=Service(self.root/'data');p=service.create_project({'title':'Science','story':STORY,'style':'legacy','visual_format':'science_atlas','mode':'characters','flow_composition':'foreground'})
  self.assertEqual(p['mode'],'illustration');self.assertEqual(p['flow_composition'],'standard')
  a=self.staged();p=service.import_analysis(p['id'],a,p['version']);pid=p['id']
  r=assemble_project(service,pid);self.assertEqual(r,{'assembled':1,'waiting':1});self.assertEqual(assemble_project(service,pid)['assembled'],0)
  for prop in p['props']:
   png=io.BytesIO();Image.new('RGB',(768,432),'#345455').save(png,format='PNG');service.upload_prop_image(prop['id'],png.getvalue(),prop['revision'])
  p=service.detail(pid);self.assertTrue(all(s['active_generation_id'] for s in p['scenes']));self.assertTrue(all(service._current_still_approval(s)['status']=='approved' for s in p['scenes']))
  manifest=service.project_prompts(pid);self.assertTrue(all(s['has_image'] for s in manifest['scenes']));self.assertEqual(len(manifest['props']),2)
  editing=Editing(service);document=editing.sync(pid,{'expected_revision':0})['document']
  self.assertTrue(all(c.get('science_atlas') and c['media_type']=='image' and not c.get('camera_motion') for c in document['clips']))
  editing.validate(pid,document)
  comparison=next(s for s in p['scenes'] if s['data']['science_atlas']['layout']=='comparison');old=comparison['active_generation_id'];prop=p['props'][0];service.unlock_prop(prop['id'])
  self.assertTrue(next(s for s in service.detail(pid)['scenes'] if s['id']==comparison['id'])['stale'])
  png=io.BytesIO();Image.new('RGB',(600,600),'#556666').save(png,format='PNG');service.upload_prop_image(prop['id'],png.getvalue())
  comparison=next(s for s in service.detail(pid)['scenes'] if s['id']==comparison['id']);self.assertNotEqual(comparison['active_generation_id'],old);self.assertFalse(comparison['stale'])
  prop=service.prop(prop['id']);asset=service.asset(json.loads(prop['references_json'])['prop']);self.assertEqual((asset['width'],asset['height']),(600,600))
 def test_worker_persists_scientific_plan_and_composes_diagrams(self):
  from imagegen.worker import Worker
  service=Service(self.root/'worker');p=service.create_project({'title':'Science worker','story':STORY,'style':'test','visual_format':'science_atlas'})
  with service.db.transaction() as c:gid=service.insert_generation(c,p['id'],'analysis',{'project':p,'profile':service.profile()})
  generation=service.db.one('SELECT * FROM generations WHERE id=?',(gid,));prepared=self.staged()
  with patch('imagegen.science_atlas.stage',return_value=prepared):Worker(service)._save_analysis(generation,analysis(),p['version'])
  p=service.detail(p['id']);self.assertEqual(len(p['props']),2);self.assertEqual(len(p['scenes']),2);self.assertTrue(p['scenes'][0]['active_generation_id']);self.assertTrue(p['analysis_metadata']['diagnostics']['science_atlas']['complete'])
 def test_word_cues_use_audio_alignment(self):
  from tests.test_montage_voice_fit import pure_fixture
  from imagegen.montage_voice_fit import propose
  snap,alignment=pure_fixture();c=snap['document']['clips'][0];c.update(media_type='image',source_video_asset_id=None,source_image_asset_id='image',source_duration_ms=86400000,trim_out_ms=5000,science_atlas={'nodes':[{'source_char_start':0},{'source_char_start':5}]})
  result=propose(snap,alignment);self.assertEqual(result['document']['clips'][0]['science_atlas']['cue_ms'],[200,600])
 def test_cuda_video_and_montage_export(self):
  import av
  from imagegen.science_atlas_render import render_video
  from imagegen.export_worker import render
  service=Service(self.root/'export');p=service.create_project({'title':'Science export','story':STORY,'style':'test','visual_format':'science_atlas'})
  a=self.staged();a['props']=[];a['scenes']=a['scenes'][:1];p=service.import_analysis(p['id'],a,p['version']);assemble_project(service,p['id'])
  scene=service.scene(p['scenes'][0]['id']);atlas=hydrate(service,scene);out=self.root/'nested'/'videos'/'local.mp4';meta=render_video(atlas,lambda aid:None,out,duration=.5,fps=24,target_size=(640,360));self.assertEqual(meta['encoder'],'h264_nvenc')
  with av.open(str(out)) as video:self.assertEqual(len(list(video.decode(video=0))),12)
  doc=Editing(service).sync(p['id'],{'expected_revision':0})['document'];doc['clips'][0]['trim_out_ms']=500;out=self.root/'montage.mp4';render(service,doc,out,lambda *a:None,lambda:False,size=(640,360))
  with av.open(str(out)) as video:self.assertEqual(len(list(video.decode(video=0))),12)
 def test_local_animation_registers_scientific_video_metadata(self):
  service=Service(self.root/'animation');p=service.create_project({'title':'Science animation','story':STORY,'style':'test','visual_format':'science_atlas'})
  a=self.staged();a['props']=[];a['scenes']=a['scenes'][:1];p=service.import_analysis(p['id'],a,p['version']);assemble_project(service,p['id']);sid=p['scenes'][0]['id']
  result=service.start_animation_generation(p['id'],scene_ids=[sid]);self.assertEqual(result['scenes'][0]['status'],'completed')
  scene=service.scene(sid);self.assertEqual(scene['data']['animation']['source'],'science_atlas');self.assertEqual(scene['data']['animation']['compute_device'],'cuda');self.assertTrue((service.root/service.asset(scene['video_asset_id'])['path']).is_file())
  # A cloud animation is a real timeline video; local scientific scenes stay procedural.
  doc=Editing(service).sync(p['id'],{'expected_revision':0})['document'];self.assertEqual(doc['clips'][0]['media_type'],'image')
  data=scene['data'];data['animation']['selected_engine']='omni_flash'
  from imagegen.db import dump,now
  with service.db.transaction() as c:
   c.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(sid,scene['current_revision']+1,dump(data),now()));c.execute('UPDATE scenes SET current_revision=? WHERE id=?',(scene['current_revision']+1,sid))
  doc=Editing(service).sync(p['id'],{'expected_revision':0})['document'];clip=doc['clips'][0]
  self.assertEqual(clip['source_video_asset_id'],scene['video_asset_id']);self.assertNotIn('science_atlas',clip);self.assertEqual(clip['trim_out_ms'],clip['source_duration_ms'])
 def test_cloud_animation_uses_current_scientific_image_even_when_image_generation_is_local(self):
  service=Service(self.root/'cloud');p=service.create_project({'title':'Science cloud','story':STORY,'style':'test','visual_format':'science_atlas'})
  a=self.staged();a['props']=[];a['scenes']=a['scenes'][:1];p=service.import_analysis(p['id'],a,p['version']);assemble_project(service,p['id']);sid=p['scenes'][0]['id']
  image_prompts=service.project_prompts(p['id'],include_all_scenes=True,scene_ids=[sid]);self.assertFalse(image_prompts['scenes'][0]['ready_for_flow'])
  service.select_scene_animation_engine(sid,'omni_flash',duration_seconds=6)
  result=service.start_animation_generation(p['id'],scene_ids=[sid]);self.assertEqual(result['scenes'][0]['status'],'queued_omni')
  before=service.scene(sid)['data']['animation']['idempotency_key'];jobs=service.animation_jobs(p['id'])['jobs']
  self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['scene_id'],sid);self.assertEqual(jobs[0]['idempotency_key'],before)
  self.assertTrue(jobs[0]['source_image_asset_id']);self.assertEqual(jobs[0]['engine'],'omni_flash')
  scene=service.scene(sid);scene['data']['science_atlas']['headline']='A different diagram';self.assertFalse(service._flow_animation_source_ready(scene,{}))
  self.assertFalse(service._flow_animation_source_ready({'data':{}},{'ready_for_flow':False}))

 def test_gpu_cues_and_original_aspect(self):
  atlas=self.staged()['scenes'][0]['science_atlas'];atlas['cue_ms']=[0,1000];atlas=compile_commands(atlas);r=ScienceReader(atlas,lambda aid:None)
  try:
   a=r.image(0,(640,360));b=r.image(2,(640,360));self.assertNotEqual(a.tobytes(),b.tobytes());self.assertIs(r.image(2,(640,360)),b)
  finally:r.close()
if __name__=='__main__':unittest.main()
