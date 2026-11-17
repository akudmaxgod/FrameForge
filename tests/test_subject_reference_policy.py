import copy
import tempfile
import unittest
from unittest.mock import patch

from imagegen.subject_policy import policy, enrich, normalize_scene
from imagegen.character_selection import project_selection
from imagegen.domain import resolve_scene_presence_contract, compile_flow_prompt, validate_project_preflight


def character(name, description, **extra):
    return dict(id='uuid-a', cast_id='ch_a', name=name, canonical_name=name,
                description=description, aliases=[name], role='supporting', wardrobe={},
                locked=False, references={}, **extra)


def scene(chars, quote=None):
    quote = quote or ' '.join(c['name'] for c in chars) + ' находятся на лугу.'
    return {'analysis_authority':'deepseek-v2', 'population_policy':'scene_participants',
            'characters':[c['name'] for c in chars], 'character_ids':[c['id'] for c in chars],
            'visible_people':len(chars), 'raw_visible_people':len(chars),
            'beat':quote, 'source_quote':quote, 'episodic_people':[],
            'shot':'wide shot', 'location':'Луг', 'location_ref':'Луг', 'props':[]}


class SubjectPolicyTests(unittest.TestCase):
    def test_collectives_are_not_individuals_across_domains(self):
        for name in ('Стадо бизонов','Стая волков','Табун лошадей','Рой пчёл',
                     'Толпа зрителей','Группа туристов','Отряд солдат','Herd of elephants','Crowd of visitors'):
            with self.subTest(name=name):
                self.assertFalse(policy(character(name, 'Участники текущего кадра.'))['portrait_required'])

    def test_legacy_anonymous_animal_is_not_a_recurring_hero(self):
        self.assertFalse(policy(character('Одинокий бизон','Одинокое животное идёт по лугу.'))['portrait_required'])

    def test_named_animal_and_human_keep_identity_requirements(self):
        for c in (character('Луна','Животное, постоянная героиня истории.'),
                  character('Генрих','Врач осматривает пациента.')):
            self.assertTrue(policy(c)['portrait_required'])

    def test_structured_scope_supports_species_without_name_lists(self):
        c=character('Нарвал','Обитатель моря.',entity_type='animal',identity_scope='generic')
        self.assertFalse(policy(c)['portrait_required'])
        projected=normalize_scene(scene([c]),[c])
        self.assertEqual(projected['source_animals'],['Нарвал'])
        self.assertEqual(projected['visible_people'],0)

    def test_projection_preserves_source_and_registry_and_is_idempotent(self):
        c=character('Стадо бизонов','Группа бизонов пасётся на лугу.')
        data=scene([c]);original=copy.deepcopy(data)
        projected=normalize_scene(data,[c])
        self.assertEqual(data,original)
        self.assertEqual(projected['source_quote'],original['source_quote'])
        self.assertEqual(projected['characters'],[])
        self.assertEqual(projected['visible_people'],0)
        self.assertEqual(projected,normalize_scene(projected,[c]))

    def test_presence_contract_does_not_require_group_portrait(self):
        c=character('Стадо бизонов','Группа бизонов пасётся на лугу.')
        data=scene([c])
        contract=resolve_scene_presence_contract(data,[c],[{'id':'loc','name':'Луг'}],require_locked_portraits=True)
        self.assertTrue(contract['render_ready'],contract)
        self.assertEqual(contract['visible_people'],0)
        self.assertFalse(any(e['code']=='missing_portrait' for e in contract['errors']))

    def test_human_and_herd_count_only_the_human(self):
        herd=character('Стадо бизонов','Группа животных на лугу.')
        doctor={**character('Генрих','Врач смотрит на животных.'),'id':'uuid-b','cast_id':'ch_b','entity_type':'human'}
        data=scene([herd,doctor],'Генрих наблюдает за стадом бизонов.')
        contract=resolve_scene_presence_contract(data,[herd,doctor],[{'id':'loc','name':'Луг'}],require_locked_portraits=True)
        self.assertEqual(contract['visible_people'],1)
        self.assertEqual(contract['visible_characters'],['Генрих'])
        self.assertEqual(contract['render_errors'][0]['details']['missing_portraits'],['Генрих'])

    def test_named_animal_has_zero_humans_but_keeps_portrait_gate(self):
        c=character('Луна','Животное, главная героиня истории.',entity_type='animal',identity_scope='individual')
        contract=resolve_scene_presence_contract(scene([c]),[c],[{'id':'loc','name':'Луг'}],require_locked_portraits=True)
        self.assertEqual(contract['visible_people'],0)
        self.assertFalse(contract['render_ready'])

    def test_preflight_excludes_named_creatures_from_human_count(self):
        dragon = character('Аша', 'Огромный крылатый дракон.', entity_type='creature')
        rider = {**character('Мира', 'Всадница.'), 'id': 'uuid-b', 'cast_id': 'ch_b', 'entity_type': 'human'}
        locations = [{'id': 'loc', 'name': 'Луг'}]
        for cast, quote, human_count in [
            ([dragon], 'Аша пролетает над лугом.', 0),
            ([rider, dragon], 'Мира стоит возле дракона Аши.', 1),
        ]:
            with self.subTest(human_count=human_count):
                data = {**scene(cast, quote), 'visible_people': human_count, 'raw_visible_people': human_count}
                contract = resolve_scene_presence_contract(data, cast, locations)
                self.assertEqual(contract['visible_people'], human_count)
                self.assertEqual(len(contract['visible_character_ids']), len(cast))
                result = validate_project_preflight({
                    'story': quote, 'characters': cast, 'locations': locations, 'scenes': [{'data': data}],
                })
                self.assertNotIn('presence_count_invariant_violation', {e['code'] for e in result['errors']})

    def test_fantasy_creature_keeps_story_scale_and_nonhuman_casting(self):
        from imagegen.service import Service
        from imagegen.visual_style import GRITTY_FLOW_STYLE, MARKER_PREFIX
        dragon = character('Аша', 'Огромный крылатый дракон, species: dragon.')
        bible = {'characters': [{'cast_id': dragon['cast_id'], 'entity_type': 'creature'}]}
        service = object.__new__(Service)
        for style in (GRITTY_FLOW_STYLE, MARKER_PREFIX):
            snapshot = service.casting_snapshot(
                {'mode': 'dynamic', 'style': style, 'cast_bible': bible}, dragon, profile={'test': True},
            )
            self.assertFalse(snapshot['character_requirements']['one_person'])
            self.assertIn('no humanoid anatomy', snapshot['prompt'])
            self.assertIn('entire body visible', snapshot['prompt'])
            self.assertNotIn('exactly one person', snapshot['prompt'].lower())
        creature = enrich([dragon], bible)[0]
        data = {**scene([creature], 'Крылатый дракон Аша пролетает над лугом.'), 'visible_people': 0}
        prompt = compile_flow_prompt({'mode': 'dynamic', 'style': GRITTY_FLOW_STYLE}, data, characters=[creature])
        self.assertIn('No humanoid body', prompt)
        self.assertIn('scale at this point in the story', prompt)
        self.assertNotIn('real-world adult dimensions', prompt)
        self.assertNotIn('Never substitute fantasy creatures', prompt)

    def test_bible_metadata_survives_persisted_registry_shape(self):
        c=character('Нарвал','Обитатель моря.')
        enriched=enrich([c],{'ch_a':{'cast_id':'ch_a','entity_type':'animal','identity_scope':'generic'}})
        self.assertFalse(enriched[0]['portrait_required'])

    def test_crowd_remains_visible_without_an_invented_single_person(self):
        from imagegen.domain import people_count_unresolved
        c=character('Толпа зрителей','Толпа собралась у сцены.')
        data=normalize_scene(scene([c]),[c])
        self.assertEqual(data['source_groups'],['Толпа зрителей'])
        self.assertTrue(people_count_unresolved(data))
        prompt=compile_flow_prompt({'mode':'dynamic','style':'Realistic photography'},data,characters=[c])
        self.assertIn('Толпа зрителей',prompt)
        self.assertNotIn('Uninhabited scene',prompt)

    def test_species_and_collective_are_present_in_pronoun_frame_prompt(self):
        c=character('Стадо бизонов','Группа бизонов пасётся на лугу.')
        data=scene([c],'Животные опускают головы к траве.')
        prompt=compile_flow_prompt({'mode':'dynamic','style':'Realistic photography'},data,characters=[c])
        self.assertIn('Стадо бизонов',prompt)
        self.assertIn('collective',prompt)
        self.assertNotIn('Show each featured animal',prompt)

    def test_provider_adapter_preserves_types_and_projects_animal_subjects(self):
        from imagegen.script_analysis.schemas import Registry,Character,Scene
        from imagegen.script_analysis.analyzer import AnalysisResult
        from imagegen.script_analysis.adapter import export_to_app_analysis
        quote='Стадо бизонов пасётся на лугу.'
        registry=Registry(title='Nature',logline='Nature',genre='Documentary',locations=[],props=[],characters=[
            Character(id='ch_a',name='Стадо бизонов',aliases=[],role='supporting',description='Группа бизонов на лугу.',arc='',entity_type='animal',identity_scope='collective')])
        result=AnalysisResult(registry,[Scene(index=0,heading='EXT. ЛУГ - ДЕНЬ',location_id='',time_of_day='day',character_ids=['ch_a'],summary=quote,mood='calm',duration_sec=5)])
        exported=export_to_app_analysis(result,quote,mode='dynamic')
        self.assertEqual(exported['cast_bible']['ch_a']['identity_scope'],'collective')
        self.assertEqual(exported['scenes'][0]['characters'],[])
        self.assertEqual(exported['scenes'][0]['visible_people'],0)
        self.assertEqual(exported['scenes'][0]['source_animals'],['Стадо бизонов'])

    def test_real_service_manifest_in_all_modes_and_registry_preservation(self):
        from imagegen.service import Service
        from imagegen.db import dump,uid,now
        for mode in ('dynamic','characters','objects','illustration'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                service=Service(temp)
                c=character('Стадо бизонов','Группа бизонов пасётся на лугу.')
                data=scene([c]);project=service.create_project({'title':'Subjects','story':data['source_quote'],'style':'Realistic photography','mode':mode})
                sid=uid()
                with service.db.transaction() as db:
                    db.execute('INSERT INTO characters(id,project_id,name,description,wardrobe,cast_id,canonical_name,aliases,role,status,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (c['id'],project['id'],c['name'],c['description'],'{}',c['cast_id'],c['name'],dump(c['aliases']),'supporting','active','[]'))
                    db.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)',(sid,project['id'],1))
                    db.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)',(sid,1,dump(data),now()))
                with patch.object(service,'profile',return_value={'width':1024,'height':576}):
                    manifest=service.project_prompts(project['id'])
                item=manifest['scenes'][0]
                self.assertTrue(item['ready_for_flow'],item.get('flow_error'))
                self.assertEqual(item['references'],[])
                self.assertIn('бизон',item['flow_prompt'].lower())
                self.assertNotIn('Show each featured animal',item['flow_prompt'])
                detail=service.detail(project['id'])
                self.assertEqual(detail['character_reference_ids'],[])
                self.assertEqual(len(detail['characters']),1)
                self.assertEqual(detail['scenes'][0]['current_revision'],1)
                stored=service.db.one('SELECT data FROM scene_revisions WHERE scene_id=?',(sid,))
                self.assertEqual(stored['data'],dump(data))

    def test_illustration_does_not_require_a_real_human_portrait(self):
        c=character('Генрих','Врач осматривает пациента.')
        data={**scene([c]),'portrait_references_enabled':False}
        self.assertTrue(resolve_scene_presence_contract(data,[c],[{'id':'loc','name':'Луг'}],require_locked_portraits=True)['render_ready'])


if __name__=='__main__': unittest.main()
