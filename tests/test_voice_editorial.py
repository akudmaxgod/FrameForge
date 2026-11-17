import copy
import unittest
from unittest.mock import patch

from imagegen.db import dump, now
from imagegen.domain import DomainError
from imagegen.voice_editorial import VERSION, participant_roles
from tests import test_voice_workflow


class VoiceEditorialTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_voice_workflow.VoiceWorkflowTests()
        self.fixture.setUp();self.fixture.prepare()
        self.service=self.fixture.service;self.workflow=self.fixture.workflow;self.pid=self.fixture.pid
        settings=self.service.settings()
        self.settings_patch=patch.object(self.service,'settings',return_value={**settings,'groq_api_key':'gsk_fixture_only'})
        self.settings_patch.start()
        detail=self.service.detail(self.pid)
        self.before=copy.deepcopy(detail['scenes'])
        report=[]
        with self.service.db.transaction() as conn:
            for scene in detail['scenes']:
                data=scene['data']
                data.update(audit_subject='Ancient brick city',audit_visual_direction='Outdoor city streets',
                    audit_source_quote=data['source_quote'],audit_required_artifacts=[],audit_time_layer='historical_reconstruction',
                    audit_source_binding='architecture',audit_people=0,audit_roles=[],audit_location_ref='Город',audit_forbidden=[])
                conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                    (dump(data),scene['id'],scene['current_revision']))
                report.append({'frame':scene['position'],'source_quote':data['source_quote'],'subject':'Ancient brick city',
                    'reason':'City description','location_ref':'Город','time_layer':'historical_reconstruction','required_artifacts':[]})
            metadata={'forgotten_cities':{'anchors':[
                {'canonical_name':'Город','anchor_type':'city_master','time_layer':'historical_reconstruction','reference_required':True},
                {'canonical_name':'Лишняя локация','anchor_type':'district','time_layer':'modern_ruins','reference_required':True}]},
                'voice_editorial':{'version':VERSION,'scene_plan':report,'checked_frames':len(report)}}
            conn.execute('UPDATE projects SET analysis_metadata=? WHERE id=?',(dump(metadata),self.pid))

    def tearDown(self):
        self.settings_patch.stop()
        self.fixture.tearDown()

    def corrected(self):
        return {'key':self.before[0]['id'],'status':'pass','subject':'Source object','subject_state':'artifact',
            'visual_direction':'Closeup of the source object on a neutral background','forbidden':['city skyline'],
            'expected_people':0,'roles':[],'required_artifacts':[],'time_layer':'museum_display','location_ref':'',
            'reason':'An object closeup does not use the city reference.'}

    def response(self, entries):
        import json
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'checked_keys':[s['id'] for s in self.before],'scenes':entries})}}]}

    def test_grouped_and_repeated_roles_keep_every_planned_person(self):
        from imagegen.domain import extract_scene_episodic_roles
        for count, roles in ((6, ['старейшины', 'торговцы', 'жрецы']),
                             (2, ['археолог']), (3, ['anonymous city resident'] * 3)):
            expanded = participant_roles(count, roles)
            self.assertEqual(len(extract_scene_episodic_roles({'episodic_people': expanded})), count)
        with self.assertRaises(DomainError):
            participant_roles(2, [])

    def test_presence_repair_preserves_ids_and_narration_timing_and_is_idempotent(self):
        scene = self.before[0]
        with self.service.db.transaction() as conn:
            data = self.service.scene(scene['id'])['data']
            data.update(audit_people=3, audit_roles=['anonymous city resident'] * 3,
                        visible_people=3, episodic_people=['anonymous city resident'] * 3)
            conn.execute('UPDATE scene_revisions SET data=? WHERE scene_id=? AND revision=?',
                         (dump(data), scene['id'], scene['current_revision']))
        self.assertEqual(self.workflow.editorial.prepare_presence(self.pid), [scene['position']])
        self.assertEqual(self.workflow.editorial.prepare_presence(self.pid), [])
        after = self.service.detail(self.pid)['scenes']
        self.assertEqual([s['id'] for s in after], [s['id'] for s in self.before])
        self.assertEqual([s['data']['voice_timing'] for s in after], [s['data']['voice_timing'] for s in self.before])

    def test_second_review_repairs_invalid_people_and_preserves_voice_intervals(self):
        invalid=self.corrected();invalid['expected_people']=1;invalid['roles']=['worker']
        with patch('imagegen.analyzer_v3.arbitration.V3GroqArbitrator._post_json',
            side_effect=[self.response([invalid]),self.response([self.corrected()])]) as request:
            self.workflow.editorial.refine(self.pid)
            self.workflow.editorial.refine(self.pid)
        self.assertEqual(request.call_count,2)
        after=self.service.detail(self.pid)
        self.assertEqual([s['id'] for s in after['scenes']],[s['id'] for s in self.before])
        self.assertEqual([s['data']['voice_timing'] for s in after['scenes']],
            [s['data']['voice_timing'] for s in self.before])
        self.assertEqual(after['scenes'][0]['data']['audit_location_ref'],'')
        audit=after['analysis_metadata']['voice_editorial']
        self.assertEqual(audit['refined_frames'],len(self.before))
        self.assertEqual(audit['corrected_frames'],1)
        self.assertEqual(audit['required_location_names'],['Город'])
        self.assertFalse(after['analysis_metadata']['forgotten_cities']['anchors'][1]['reference_required'])

    def test_incomplete_review_cannot_change_scenes(self):
        response=self.response([self.corrected()])
        import json
        content=json.loads(response['choices'][0]['message']['content']);content['checked_keys']=[]
        response['choices'][0]['message']['content']=json.dumps(content)
        from imagegen.domain import DomainError
        with patch('imagegen.analyzer_v3.arbitration.V3GroqArbitrator._post_json',return_value=response):
            with self.assertRaisesRegex(DomainError,'не покрыла все кадры'):
                self.workflow.editorial.refine(self.pid)
        after=self.service.detail(self.pid)
        self.assertEqual(after['scenes'][0]['current_revision'],self.before[0]['current_revision'])


if __name__=='__main__':unittest.main()
