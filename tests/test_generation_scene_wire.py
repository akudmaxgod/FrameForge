"""Cross-genre regressions for source content reaching the actual renderer."""
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from imagegen.db import dump, now, uid
from imagegen.domain import fingerprint
from imagegen.generation_scene import normalize, compile_wire, validate_wire, semantic_issues, reference_locations, RETRY
from imagegen.narrative_frame_focus import CATEGORIES
from imagegen.service import Service


def signed(story, participants=('Алиса','Борис')):
    proof={'start':0,'end':len(story),'quote':story}
    value={'version':'story-visual-plan/1','key':f'1:0:{len(story)}',
        'subject':'люди','action':'несут землю','state':'Земля в корзинах',
        'participants':[{'name':name,'kind':'human','role':'actor','evidence':proof} for name in participants],
        'place':'Дом','location_ref':'','period':'Вечер','time_layer':'documented_event',
        'subject_state':'living','source_status':'fact','visual_direction':'Two people carry baskets of earth.',
        'required_artifacts':[],'evidence':[proof],
        'source_review':{'key':f'1:0:{len(story)}','checks':[{'category':c,'verdict':'supported','evidence':[proof],'reason':'Fixture'} for c in CATEGORIES]}}
    return {'source_start':0,'source_end':len(story),'source_quote':story,
        'beat':value['visual_direction'],'visual_content':value,'visual_content_hash':fingerprint(value),
        'visual_content_story_hash':hashlib.sha256(story.encode()).hexdigest(),
        'raw_visible_people':len(participants),'visible_people':len(participants),
        'characters':[],'episodic_people':list(participants),'props':[],
        'location_ref':'Старое общее место','location':'Дом','time':'Вечер'}


class GenerationSceneWireTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.service=Service(self.temp.name)
        self.service.settings=lambda:{'source_contract_enabled':False,'unet':'test','clip':'test','vae':'test'}
        self.story='Алиса и Борис несут землю в корзинах.'
        self.project=self.service.create_project({'title':'Test','story':self.story,'mode':'illustration','style':'Realism'})
        self.data=signed(self.story)
        self.scene={'id':uid(),'project_id':self.project['id'],'current_revision':1,'data':self.data}

    def test_all_modes_keep_source_selected_people(self):
        for mode in ('illustration','objects','characters','dynamic'):
            project={**self.project,'mode':mode}
            snap=self.service.scene_snapshot(self.scene,allow_unlocked=True,_project=project)
            self.assertEqual(snap['expected_people'],2,mode)
            self.assertNotIn('UNINHABITED SCENE',snap['prompt'])
            self.assertIn(self.story,snap['flow_wire']['prompt'])

    def test_named_actor_is_not_counted_twice_as_episodic(self):
        cid=uid()
        with self.service.db.transaction() as db:
            db.execute('INSERT INTO characters(id,project_id,name,description,wardrobe,references_json,locked) VALUES(?,?,?,?,?,?,?)',
                (cid,self.project['id'],'Алиса','A woman','{}',dump({'portrait':'portrait-asset'}),1))
        self.scene['data']={**self.data,'characters':['Алиса']}
        snap=self.service.scene_snapshot(self.scene,allow_unlocked=True,_project=self.project)
        self.assertEqual(snap['expected_people'],2)
        self.assertEqual(len(snap['scene']['episodic_people']),1)
        self.assertTrue(snap['scene']['episodic_people'][0].startswith('Борис'))
        self.assertEqual(len(snap['references']),1)

    def test_unknown_link_is_not_replaced_with_first_location(self):
        data=normalize(self.project,self.data,[{'id':'l','name':'Другое место'}])
        self.assertEqual(data['location_ref'],'')
        data=normalize(self.project,self.data,[{'id':'l','name':'Дом'}])
        self.assertEqual(data['location_ref'],'Дом')

    def test_exact_prompt_is_required_and_long_input_is_not_cut(self):
        wire=compile_wire(self.project,self.data)
        self.assertIsNone(validate_wire(wire,wire['prompt']))
        self.assertIsNone(validate_wire(wire,wire['prompt']+RETRY))
        self.assertTrue(validate_wire(wire,wire['prompt'][:100]))
        self.data['visual_content']['visual_direction']='A person carries earth. '*150
        wire=compile_wire(self.project,self.data)
        self.assertTrue(wire['error']);self.assertEqual(wire['prompt'],'')

    def test_wrong_reference_and_other_era_are_not_attached(self):
        loc={'id':'l','name':'Дом','references':{'environment':'asset'}}
        project={**self.project,'analysis_metadata':{'reference_semantic_scope':{
            'l':{'asset_id':'asset','status':'accepted','era':'modern'}}}}
        selected,omitted=reference_locations(project,{**self.data,'period':'XII век'},[loc])
        self.assertFalse(selected);self.assertTrue(omitted)
        selected,omitted=reference_locations(project,{**self.data,'period':'Современность'},[loc])
        self.assertEqual(len(selected),1)

    def test_spatial_relation_and_metaphor_are_semantic_constraints(self):
        self.assertTrue(semantic_issues({'visual_direction':'A town outside the cave entrance.'},'Город находится внутри пещеры.'))
        self.assertFalse(semantic_issues({'visual_direction':'A town inside a cave.'},'Город находится внутри пещеры.'))
        self.assertTrue(semantic_issues({'visual_direction':'A metal chain lies on the ground.'},'Эта причинная цепочка не доказана.'))
        self.assertFalse(semantic_issues({'visual_direction':'A metal chain lies on the ground.'},'Металлическая цепь лежит на земле.'))

    def test_admission_archives_exact_prompt_without_changing_retry_identity(self):
        from imagegen.flow_admission import FlowAdmission
        self.service.scene=lambda sid:self.scene
        self.service.google_generation_capacity=lambda pid:{'pending':0}
        self.service.settings=lambda:{'google_queue_enabled':True,'source_contract_enabled':False}
        admission=FlowAdmission(self.service)
        with patch('imagegen.google_queue.configured_queue',return_value=SimpleNamespace(project_paused=lambda pid:False)):
            result=admission.claim(self.scene['id'],'req',1,'Exact outgoing prompt')
            repeated=admission.claim(self.scene['id'],'req',1,'Exact outgoing prompt')
        self.assertTrue(result['allowed']);self.assertTrue(repeated['allowed'])
        with admission.db() as db:
            rows=db.execute('SELECT prompt FROM request_documents').fetchall()
        self.assertEqual([row[0] for row in rows],['Exact outgoing prompt'])

if __name__=='__main__':unittest.main()
