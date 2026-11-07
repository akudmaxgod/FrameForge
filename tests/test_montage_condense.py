import copy,time,unittest
from unittest.mock import patch
import numpy as np
from imagegen.montage_condense import prepare,finish
from imagegen.montage_voice_fit import snapshot
from imagegen.montage_semantic import clip_texts,match,propose
from imagegen.domain import DomainError
from imagegen.editing import normalize
from tests import test_montage_voice_fit as fixtures

class CondenseTests(unittest.TestCase):
 def sample(self):
  snap,_=fixtures.pure_fixture()
  for c in snap['document']['clips']:c.update(source_duration_ms=5000,trim_out_ms=5000,media_type='video')
  return snap
 def test_unique_events_never_auto_removed(self):
  snap=self.sample();before=copy.deepcopy(snap);reduced,rows=prepare(snap)
  self.assertTrue(all(r['included'] for r in rows));self.assertEqual(snap,before)
 def test_repeated_description_prioritizes_cloud_and_final(self):
  snap=self.sample();snap['scenes'][1]['data']['source_quote']=snap['scenes'][0]['data']['source_quote']
  # Put the duplicate away from protected first/final anchors.
  duplicate=copy.deepcopy(snap['document']['clips'][0]);duplicate['edit_clip_id']='duplicate'
  snap['document']['clips'].insert(1,duplicate)
  reduced,rows=prepare(snap);self.assertTrue(next(c for c in reduced['document']['clips'] if c['edit_clip_id']=='duplicate')['excluded'])
  snap['scenes'][0]['data']['animation']={'selected_engine':'omni_flash'}
  reduced,rows=prepare(snap);self.assertTrue(next(r for r in rows if r['clip_id']=='duplicate')['included'])
 def test_explicit_selection_can_restore_and_preserves_original_exclusions(self):
  snap=self.sample();reduced,rows=prepare(snap,['0','2']);self.assertTrue(reduced['document']['clips'][1]['excluded'])
  restored,rows=prepare(snap,['0','1','2']);self.assertTrue(all(r['included'] for r in rows))
 def test_locked_empty_foreign_and_longer_audio_rejected(self):
  snap=self.sample();snap['document']['clips'][1]['timing_locked']=True
  for keep in ([],['foreign'],['0','2'],['0','0']):
   with self.assertRaises(DomainError):prepare(snap,keep)
  snap['audio_binding']['trim_out_ms']=30000
  with self.assertRaises(DomainError):prepare(snap)
 def test_finish_keeps_audio_sources_order_and_duration(self):
  snap=self.sample();reduced,rows=prepare(snap,['0','2']);p=self.semantic(reduced);p=finish(snap,p,rows)
  self.assertEqual([c['edit_clip_id'] for c in p['timeline']['clips']],['0','2']);self.assertEqual(p['timeline']['duration_ms'],4500)
  self.assertEqual(p['document']['audio_tracks'],snap['document']['audio_tracks']);self.assertEqual(len(p['document']['clips']),3)
  self.assertEqual(p['reduction']['excluded_count'],1)
 @staticmethod
 def semantic(snap):
  texts=clip_texts(snap);n=len(texts);units=[{'text':'Фраза '+str(i),'start_ms':i*4500/n,'end_ms':(i+1)*4500/n-1,'confidence':.99} for i in range(n)]
  mapping=match(snap,{'audio_duration':4.5},np.eye(n),np.eye(n),units)
  return propose(snap,mapping)
 def test_worker_preview_review_apply_export_rollback_and_stale(self):
  f=fixtures.IntegrationTests();f.setUp()
  try:
   manager=f.manager;ids=[c['edit_clip_id'] for c in f.doc['clips']];before=f.editor.get(f.pid)
   scenes=f.service.db.all('SELECT * FROM scenes ORDER BY id');gens=f.service.db.all('SELECT * FROM generations ORDER BY id')
   with patch.object(manager,'semantic_proposal',side_effect=lambda pid,jid,snap,event:self.semantic(snap)):
    job=manager.start(f.pid,{'expected_revision':before['revision'],'timing_mode':'condense','keep_clip_ids':[ids[0],ids[-1]]})
    for _ in range(100):
     job=manager.get(f.pid,job['id'])
     if job['status'] not in ('queued','recognizing'):break
     time.sleep(.02)
   self.assertEqual(job['status'],'ready',job);self.assertTrue(job['proposal']['can_apply'],job)
   self.assertEqual(f.editor.get(f.pid),before)
   decisions=[{'clip_id':b['clip_id'],'accepted':True} for b in job['proposal']['mapping']['bindings']]
   job=manager.review(f.pid,job['id'],{'expected_review_revision':0,'decisions':decisions})
   applied=manager.apply(f.pid,job['id'],{'expected_revision':before['revision']})
   self.assertEqual(len(normalize(applied['document'])['clips']),2)
   out=f.service.root/'condense.mp4'
   duration=fixtures.render(f.service,applied['document'],out,lambda *a:None,lambda:False,size=(160,90))
   self.assertAlmostEqual(duration,4.5,places=3)
   with fixtures.av.open(str(out)) as c:self.assertEqual(sum(1 for _ in c.decode(video=0)),108)
   back=manager.rollback(f.pid,job['id'],{'expected_revision':applied['revision']});self.assertEqual(back['document'],before['document'])
   self.assertEqual(scenes,f.service.db.all('SELECT * FROM scenes ORDER BY id'));self.assertEqual(gens,f.service.db.all('SELECT * FROM generations ORDER BY id'))
  finally:f.doCleanups()

if __name__=='__main__':unittest.main()
