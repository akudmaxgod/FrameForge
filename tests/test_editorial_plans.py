import copy
import concurrent.futures
import json
import tempfile
import unittest

from imagegen.db import dump
from imagegen.domain import DomainError
from imagegen.editorial_plans import EditorialPlans
from imagegen.server import Application
from imagegen.source_catalog import SourceCatalog
from imagegen.forgotten_cities.event_context import for_source


class EditorialPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(self.temp.name)
        self.service = self.app.service
        self.plans = EditorialPlans(self.service)
        self.story = 'Во втором веке строители могли вырубить пещеры.'
        self.channel = self.service.create_channel({'name':'Editorial fixture','profile_type':'forgotten_cities'})
        self.project = self.service.create_project({'title':'Editorial fixture','story':self.story,
            'channel_profile_id':self.channel['id'],'mode':'illustration'})
        self.service.import_analysis(self.project['id'], {'characters':[], 'scenes':[
            {'beat':self.story, 'source_quote':self.story, 'source_start':0, 'source_end':len(self.story),
             'location':'Old city','visible_people':0}]}, self.project['version'])
        self.service.save_settings({'source_contract_enabled':True, 'google_queue_enabled':True})
        self.sid = self.service.detail(self.project['id'])['scenes'][0]['id']

    def evidence(self, quote):
        return {'start':self.story.index(quote),'end':self.story.index(quote)+len(quote),'quote':quote}

    def payload(self, **changes):
        return {'mode':'illustrative', 'focus':self.evidence(self.story), 'subject':self.evidence('пещеры'),
                'time_layer':'historical_reconstruction', 'time_evidence':self.evidence('Во втором веке'),
                'roles':['строители'], 'reason':'Возможная функция, отдельное событие не установлено.', **changes}

    def preview(self, **changes):
        return self.plans.preview(self.sid, self.payload(**changes), self.service.scene(self.sid)['current_revision'])

    def apply(self, plan):
        return self.plans.apply(self.sid, plan['id'], plan['document']['base']['revision'])

    def snapshot(self):
        return self.service.scene_snapshot(self.service.scene(self.sid), allow_unlocked=True)

    def test_preview_is_immutable_and_does_not_change_scene_or_media(self):
        before = copy.deepcopy(self.service.scene(self.sid))
        project = self.service.project(self.project['id'])
        plan = self.preview()
        self.assertEqual(self.service.scene(self.sid), before)
        self.assertEqual(self.service.project(self.project['id']), project)
        self.assertFalse(plan['applied'])
        self.assertEqual(self.service.db.all('SELECT * FROM generations'), [])

    def test_duplicate_preview_is_idempotent_even_with_parallel_requests(self):
        first = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _:self.preview(), range(6)))
        self.assertEqual({r['id'] for r in results}, {first['id']})

    def test_apply_retains_source_old_revision_and_findings(self):
        before = self.service.scene(self.sid)
        plan = self.preview()
        self.apply(plan)
        after = self.service.scene(self.sid)
        self.assertEqual(after['current_revision'], before['current_revision']+1)
        for key in ('source_quote', 'source_start', 'source_end'):
            self.assertEqual(after['data'][key], before['data'][key])
        old = self.service.db.one('SELECT data FROM scene_revisions WHERE scene_id=? AND revision=?', (self.sid,before['current_revision']))
        self.assertEqual(json.loads(old['data']), before['data'])
        contract = self.snapshot()['source_contract']
        self.assertEqual(contract['status'], 'bound')
        self.assertTrue(contract['semantic_review']['findings'])
        self.assertFalse(contract['editorial_decision']['historical_accuracy_verified'])

    def test_people_time_and_subject_are_coherent_in_native_and_flow(self):
        self.apply(self.preview())
        contract = self.snapshot()['source_contract']
        flow = self.service.project_prompts(self.project['id'], include_all_scenes=True)['scenes'][0]
        self.assertEqual(contract['participants'], ['строители'])
        self.assertEqual(contract['subject'], 'пещеры')
        self.assertEqual(contract['time_layer'], 'historical_reconstruction')
        self.assertEqual(contract['hash'],flow['source_contract']['hash'])
        self.assertTrue(flow['semantic_valid'])
        # This fixture intentionally has no episode IR/reference catalogue.
        # A bound editorial scene does not authorize bypassing project preflight.
        self.assertFalse(flow['ready_for_flow'])
        self.assertIn('профильного анализа', flow['flow_error'])

    def test_neutral_focus_does_not_reintroduce_old_human_roles(self):
        self.apply(self.preview(mode='neutral_detail', roles=[]))
        contract = self.snapshot()['source_contract']
        self.assertEqual(contract['participants'], [])
        self.assertEqual(contract['animals'], [])

    def test_second_apply_keeps_revision(self):
        plan = self.preview(); self.apply(plan)
        before = self.service.scene(self.sid)
        self.apply(plan)
        self.assertEqual(before,self.service.scene(self.sid))

    def test_appearance_pin_preserves_immutable_source_decision(self):
        plan = self.preview(); self.apply(plan)
        before = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'participant_identity': {'asset_id': 'visual-only-reference'}}, before['current_revision'])
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(error)
        self.assertEqual(decision['id'], plan['id'])
        self.assertEqual(decision['hash'], plan['content_hash'])

    def test_pin_cannot_hide_a_semantic_edit(self):
        plan = self.preview(); self.apply(plan)
        before = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'participant_identity': {'asset_id': 'visual-only-reference'},
            'composition': 'Different action'}, before['current_revision'])
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(decision)
        self.assertIn('устарела', error)

    def test_animation_selection_and_queue_preserve_source_decision(self):
        plan = self.preview(); self.apply(plan)
        self.service.select_scene_animation_engine(self.sid, 'omni_flash', duration_seconds=6)
        scene = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'animation': {**scene['data']['animation'], 'status': 'queued_omni'}}, scene['current_revision'])
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(error)
        self.assertEqual(decision['hash'], plan['content_hash'])

    def test_animation_update_cannot_hide_changed_story_action(self):
        plan = self.preview(); self.apply(plan)
        scene = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'animation': {'selected_engine': 'omni_flash'}, 'beat': 'A different action.'}, scene['current_revision'])
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(decision)
        self.assertIn('устарела', error)

    def test_project_animation_settings_preserve_source_decision(self):
        plan = self.preview(); self.apply(plan)
        self.service.build_animation_plan(self.project['id'], plan_mode='selective')
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(error)
        self.assertEqual(decision['hash'], plan['content_hash'])

    def test_legacy_empty_settings_hash_recovers_only_unchanged_source_project(self):
        from imagegen.domain import fingerprint
        original = self.service.project(self.project['id'])
        legacy = {'project_hash': fingerprint(original)}
        changed = {**original, 'animation_settings': '{"plan_mode":"selective"}'}
        self.assertTrue(self.plans.project_matches(legacy, changed))
        self.assertFalse(self.plans.project_matches(legacy, {**changed, 'story': 'Different source.'}))
        self.assertFalse(self.plans.project_matches(legacy, {**changed, 'style': 'Different style.'}))

    def test_animation_settings_cannot_hide_project_story_change(self):
        plan = self.preview(); self.apply(plan)
        with self.service.db.transaction() as db:
            db.execute('UPDATE projects SET story=?,animation_settings=? WHERE id=?',
                       (self.story+' A new event.', '{"plan_mode":"selective"}', self.project['id']))
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(decision)
        self.assertIn('устарела', error)

    def test_pin_cannot_hide_a_semantic_edit_later_reverted(self):
        plan = self.preview(); self.apply(plan)
        before = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'composition': 'Different action'}, before['current_revision'])
        changed = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'composition': before['data'].get('composition'),
            'participant_identity': {'asset_id': 'visual-only-reference'}}, changed['current_revision'])
        decision, error = self.plans.bound(self.service.scene(self.sid), self.service.project(self.project['id']))
        self.assertIsNone(decision)
        self.assertIn('устарела', error)

    def test_concurrent_apply_writes_one_revision(self):
        plan = self.preview()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _:self.apply(plan), range(6)))
        self.assertEqual(self.service.scene(self.sid)['current_revision'],plan['document']['base']['revision']+1)
        self.assertEqual(len(self.service.db.all('SELECT * FROM editorial_scene_applications')),1)

    def test_edit_after_preview_is_rejected(self):
        plan = self.preview()
        scene = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'composition':'changed'},scene['current_revision'])
        with self.assertRaises(DomainError):self.apply(plan)

    def test_edit_after_apply_invalidates_runtime_binding(self):
        self.apply(self.preview())
        scene = self.service.scene(self.sid)
        self.service.edit_scene(self.sid, {'composition':'changed'},scene['current_revision'])
        contract=self.snapshot()['source_contract']
        self.assertEqual(contract['status'],'needs_resolution')
        self.assertTrue(any('устарела' in r for r in contract['conflicts']))
        self.assertIsNone(contract['editorial_decision'])

    def test_story_change_after_apply_invalidates_binding(self):
        plan=self.preview();self.apply(plan)
        with self.service.db.transaction() as db:
            db.execute('UPDATE projects SET story=story||? WHERE id=?', (' Другой эпизод.', self.project['id']))
        self.assertEqual(self.snapshot()['source_contract']['status'],'needs_resolution')
        with self.assertRaises(DomainError):self.apply(plan)

    def test_catalog_rebind_invalidates_binding_but_unpinned_revision_does_not(self):
        with self.service.db.transaction() as db:
            db.execute('UPDATE projects SET analysis_metadata=? WHERE id=?', (dump({'forgotten_cities':{'anchors':[
                {'anchor_id':'a','canonical_name':'Cave','anchor_type':'artifact','source_quote':self.story}]}}), self.project['id']))
        catalog=SourceCatalog(self.service)
        bound=catalog.sync(self.project['id'],self.service.project(self.project['id'])['version'])['outcomes'][0]
        self.apply(self.preview())
        catalog.revise(bound['entry_id'], 1, {'description_unverified':'New annotation'})
        self.assertEqual(self.snapshot()['source_contract']['status'],'bound')
        catalog.bind(self.project['id'],'a',bound['entry_id'],2,expected_binding=catalog.view(self.project['id'])['bindings'][0]['binding_token'])
        self.assertEqual(self.snapshot()['source_contract']['status'],'needs_resolution')

    def test_media_blocks_application_but_preview_remains_available(self):
        plan=self.preview()
        with self.service.db.transaction() as db:
            db.execute('UPDATE scenes SET active_generation_id=? WHERE id=?',('existing-media',self.sid))
        self.assertFalse(self.preview()['applied'])
        with self.assertRaises(DomainError):self.apply(plan)

    def test_plan_id_from_another_scene_cannot_be_applied(self):
        plan=self.preview()
        with self.assertRaises(DomainError):self.plans.apply('other',plan['id'],1)

    def test_edit_cannot_forge_an_application(self):
        plan=self.preview()
        scene=self.service.scene(self.sid)
        self.service.edit_scene(self.sid,{'editorial_plan_id':plan['id']},scene['current_revision'])
        self.assertEqual(self.snapshot()['source_contract']['status'],'needs_resolution')

    def test_literal_mode_cannot_launder_a_hypothesis(self):
        with self.assertRaises(DomainError):self.preview(mode='literal')

    def test_future_and_fabricated_evidence_are_rejected(self):
        for subject in ({'start':0,'end':7,'quote':'a dragon'}, {'start':len(self.story),'end':len(self.story)+4,'quote':'пещеры'}):
            with self.assertRaises(DomainError):self.preview(subject=subject)

    def test_arbitrary_human_role_and_unknown_fields_are_rejected(self):
        with self.assertRaises(DomainError):self.preview(roles=['пещеры'])
        with self.assertRaises(DomainError):self.preview(resolved_codes=['invalid_evidence'])

    def test_unrecorded_historical_claim_cannot_be_omitted(self):
        with self.assertRaises(DomainError):self.preview(omit_claims=['Во втором веке'])

    def test_date_is_source_backed_and_temporally_consistent(self):
        with self.assertRaises(DomainError):self.preview(time_evidence=self.evidence('пещеры'))
        with self.assertRaises(DomainError):self.preview(time_layer='modern_ruins')

    def test_api_preview_list_apply(self):
        path='/api/scenes/'+self.sid+'/editorial-plans'
        revision=self.service.scene(self.sid)['current_revision']
        plan=self.app.dispatch('POST',path,{'decision':self.payload(),'revision':revision})
        self.assertEqual(self.app.dispatch('GET',path,{})['plans'][0]['id'],plan['id'])
        self.assertTrue(self.app.dispatch('POST',path+'/apply',{'plan_id':plan['id'],'revision':revision})['applied'])

    def test_reanalysis_can_replace_scenes_with_editorial_versions(self):
        self.apply(self.preview())
        project=self.service.project(self.project['id'])
        self.service.import_analysis(project['id'], {'characters':[], 'scenes':[
            {'beat':self.story,'source_quote':self.story,'source_start':0,'source_end':len(self.story),'location':'Cave'}]},project['version'])
        self.assertEqual(self.service.db.all('SELECT * FROM editorial_scene_plans'),[])

    def replace_story(self, story):
        self.story=story
        with self.service.db.transaction() as db:
            db.execute('UPDATE projects SET story=?,analysis_metadata=? WHERE id=?',(story,'{}',self.project['id']))
        project=self.service.project(self.project['id'])
        self.service.import_analysis(project['id'],{'characters':[],'scenes':[{'beat':story,'source_quote':story,
            'source_start':0,'source_end':len(story),'location':'Cave'}]},project['version'])
        self.sid=self.service.detail(project['id'])['scenes'][0]['id']

    def test_mixed_time_requires_a_separate_clause(self):
        self.replace_story('В пятом веке жители укрывались в подвале. Сегодня археологи исследуют этот подвал.')
        payload={'mode':'neutral_detail','focus':self.evidence(self.story),'subject':self.evidence('подвале'),
                 'time_layer':'modern_ruins','time_evidence':self.evidence('Сегодня'),'roles':[], 'reason':'Выбрать современную деталь.'}
        plan=self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision'])
        self.apply(plan)
        self.assertEqual(self.snapshot()['source_contract']['status'],'needs_resolution')

    def test_selected_modern_clause_removes_ancient_people_from_image_only(self):
        self.replace_story('В пятом веке жители укрывались в подвале. Сегодня археологи исследуют этот подвал.')
        focus='Сегодня археологи исследуют этот подвал.'
        plan=self.plans.preview(self.sid,{'mode':'selected_clause','focus':self.evidence(focus),'subject':self.evidence('археологи'),
            'time_layer':'modern_ruins','time_evidence':self.evidence('Сегодня'),'roles':['археологи'],
            'reason':'Выбран современный фрагмент без изменения озвучки.'},self.service.scene(self.sid)['current_revision'])
        self.apply(plan)
        contract=self.snapshot()['source_contract']
        self.assertEqual(contract['status'],'bound')
        self.assertEqual(contract['participants'],['археологи'])
        self.assertEqual(contract['source']['quote'],self.story)
        self.assertEqual(contract['visual_source']['quote'],focus)

    def test_reported_claim_cannot_become_illustrative_literal_episode(self):
        self.replace_story('Газеты утверждали, что подвал построили пришельцы.')
        payload={'mode':'illustrative','focus':self.evidence(self.story),'subject':self.evidence('подвал'),
            'time_layer':'modern_ruins','roles':[], 'reason':'Иллюстрация сообщения.'}
        with self.assertRaises(DomainError):self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision'])
        payload.update(mode='reported_insert',focus=self.evidence('Газеты'),subject=self.evidence('Газеты'))
        self.apply(self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision']))
        self.assertEqual(self.snapshot()['source_contract']['status'],'bound')
        self.assertIn('do not enact the embedded claim',self.snapshot()['source_contract']['direction'])

    def test_uncertain_historical_property_remains_flagged_until_explicit_omission(self):
        from imagegen.forgotten_cities.structures import build_structure_anchors
        self.replace_story('Каменные двери могли иметь диаметр один метр.')
        anchors=[a.to_dict() for a in build_structure_anchors(self.story)]
        with self.service.db.transaction() as db:
            db.execute('UPDATE projects SET analysis_metadata=? WHERE id=?', (dump({'forgotten_cities':{'anchors':anchors}}),self.project['id']))
        catalog=SourceCatalog(self.service)
        b=catalog.sync(self.project['id'],self.service.project(self.project['id'])['version'])['outcomes'][0]
        catalog.record_check(b['entry_id'],1,'один метр','uncertain',[{'url':'https://example.org/paper','title':'Fixture evidence','reason':'Size not established.'}])
        token=catalog.view(self.project['id'])['bindings'][0]['binding_token']
        catalog.bind(self.project['id'],b['anchor_id'],b['entry_id'],2,expected_binding=token)
        payload={'mode':'neutral_detail','focus':self.evidence('Каменные двери'),'subject':self.evidence('двери'),
            'time_layer':'modern_ruins','roles':[],'reason':'Показать дверь без точного масштаба.'}
        self.apply(self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision']))
        self.assertEqual(self.snapshot()['source_contract']['status'],'needs_resolution')
        payload['omit_claims']=['один метр']
        self.apply(self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision']))
        decision=self.snapshot()['source_contract']['editorial_decision']
        self.assertEqual(self.snapshot()['source_contract']['status'],'bound')
        self.assertEqual(decision['omitted_checks'][0]['verdict'],'uncertain')
        self.assertFalse(decision['historical_accuracy_verified'])

    def test_same_measurement_cannot_transfer_a_door_check_to_a_shaft(self):
        from imagegen.forgotten_cities.structures import build_structure_anchors
        self.replace_story('Диаметр вентиляционных шахт составлял один метр. Диаметр каменных дверей составлял один метр.')
        quote=self.story.split('. ')[0]+'.'
        anchors=[a.to_dict() for a in build_structure_anchors(self.story)]
        project=self.service.project(self.project['id'])
        self.service.import_analysis(project['id'],{'characters':[], 'v3_metadata':{'forgotten_cities':{'anchors':anchors}},
            'scenes':[{'beat':quote,'source_quote':quote,'source_start':0,'source_end':len(quote),'location':'Cave'}]},project['version'])
        self.sid=self.service.detail(project['id'])['scenes'][0]['id']
        catalog=SourceCatalog(self.service)
        bindings=catalog.sync(project['id'],self.service.project(project['id'])['version'])['outcomes']
        door=next(b for b in bindings if catalog.get(b['entry_id'])['document']['source_kind']=='stone_door')
        catalog.record_check(door['entry_id'],1,'один метр','uncertain',[{'url':'https://example.org/door','title':'Door evidence','reason':'Door size uncertain.'}])
        binding=next(b for b in catalog.view(project['id'])['bindings'] if b['anchor_id']==door['anchor_id'])
        catalog.bind(project['id'],door['anchor_id'],door['entry_id'],2,binding['binding_token'])
        payload={'mode':'neutral_detail','focus':self.evidence('вентиляционных шахт'),'subject':self.evidence('шахт'),
            'time_layer':'modern_ruins','roles':[],'reason':'Отдельное изображение вентиляционного ствола.'}
        plan=self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision'])
        self.assertEqual(plan['document']['source_historical_checks'],[])
        self.apply(plan)
        self.assertEqual(self.snapshot()['source_contract']['status'],'bound')

    def test_animal_selection_stays_distinct_from_human_presence(self):
        self.replace_story('Коза стояла в пещере.')
        plan=self.plans.preview(self.sid,{'mode':'neutral_detail','focus':self.evidence(self.story),'subject':self.evidence('Коза'),
            'time_layer':'modern_ruins','roles':[],'animals':['Коза'],'reason':'Выбран живой субъект из текста.'},self.service.scene(self.sid)['current_revision'])
        self.apply(plan)
        self.assertEqual(self.snapshot()['source_contract']['participants'],[])
        self.assertEqual(self.snapshot()['source_contract']['animals'][0]['name'],'Коза')
        self.assertNotIn('INANIMATE PHENOMENA ONLY',self.snapshot()['prompt'])

    def test_geological_common_subject_is_not_masked_by_multiple_proper_names(self):
        story='В эпоху миоцена извержения вулканов Арарат, Эльбрус и Казбек засыпали долины пеплом.'
        names=[p['name'] for p in for_source(story,story)['subjects']]
        self.assertIn('извержения',names)

    def test_weapon_objects_after_the_first_verb_do_not_become_its_actor(self):
        story='Сквозь него выставляли длинные копья и метали стрелы, уничтожая противника.'
        context=for_source(story,story)
        self.assertEqual(context['subjects'],[])
        self.assertTrue(context['implicit_actor'])

    def test_until_negation_cannot_lose_its_governing_operator_in_literal_focus(self):
        self.replace_story('В 1963 году житель нашёл проход. Город спал, пока житель не снёс стену.')
        payload={'mode':'literal','focus':self.evidence('житель не снёс стену.'),'subject':self.evidence('житель не снёс стену.'),
            'time_layer':'documented_event','time_evidence':self.evidence('В 1963 году'),'roles':[],
            'reason':'Повторный рассказ о датированном открытии.'}
        with self.assertRaises(DomainError):self.plans.preview(self.sid,payload,self.service.scene(self.sid)['current_revision'])


if __name__ == '__main__':unittest.main()
