import copy
import json
import math
import re
import unittest
from unittest.mock import patch

from imagegen.db import dump
from imagegen.domain import DomainError
from imagegen.narration_expansion import narration_frames, validate_timeline, additional_scene, NarrationExpansion
from imagegen.voice_plans import VoicePlans
from tests import test_voice_workflow


class NarrationTimelineTests(unittest.TestCase):
    def source(self):
        story = 'Рабочий открыл стену, увидел проход, осветил камень, спустился в зал. Археолог измерил шахту.'
        scenes = []
        for index, sentence in enumerate(re.finditer(r'\S[^.]*\.', story), 1):
            scenes.append({'id': str(index), 'position': index, 'current_revision': 1,
                'data': {'source_start': sentence.start(), 'source_end': sentence.end(), 'source_quote': sentence.group(),
                    'beat': 'Approved source event', 'time_layer': 'documented_event', 'visible_people': 1}})
        tokens = list(re.finditer(r'\w+', story))
        words = [{'char_start': m.start(), 'char_end': m.end(), 'start': index*2,
            'end': index*2+1.7, 'text': m.group(), 'method': 'exact', 'confidence': .99} for index, m in enumerate(tokens)]
        return story, words, scenes, len(words)*2

    def test_long_event_gets_multiple_shots_and_full_audio_is_preserved(self):
        story, words, scenes, duration = self.source()
        frames = narration_frames(story, words, scenes, duration, 7)
        self.assertGreater(len(frames), len(scenes))
        self.assertAlmostEqual(sum(f['timing']['duration'] for f in frames), duration)
        self.assertEqual([f['parent_scene_id'] for f in frames if f['preserve_existing']], ['1','2'])
        self.assertTrue(all(f['narration_source']['quote'] == story[f['narration_source']['start']:f['narration_source']['end']] for f in frames))

    def test_audio_pauses_and_final_tail_are_not_dropped(self):
        story, words, scenes, duration = self.source()
        for word in words: word['start'] += 2; word['end'] += 2
        frames = narration_frames(story, words, scenes, duration+7, 7)
        self.assertEqual(frames[0]['timing']['start'], 0)
        self.assertEqual(frames[-1]['timing']['end'], duration+7)

    def test_cut_never_splits_shared_numeric_timing_group(self):
        story, words, scenes, duration = self.source()
        for w in words: w['timing_group'] = {'char_start': 0, 'char_end': len(story)}
        frames = narration_frames(story, words, scenes, duration, 7)
        self.assertEqual(len(frames), len(scenes))

    def test_missing_source_or_nonmonotonic_audio_fails_without_mutation(self):
        story, words, scenes, duration = self.source()
        before = copy.deepcopy(scenes)
        with self.assertRaises(DomainError): narration_frames(story, [], scenes, duration)
        words[-1]['start'] = -1
        with self.assertRaises(DomainError): narration_frames(story, words, scenes, duration)
        self.assertEqual(scenes, before)

    def test_additional_shot_preserves_era_and_editorial_exclusions_and_rebinds_hero(self):
        story, words, scenes, duration = self.source()
        parent = scenes[0]
        parent['data'].update(audit_visual_direction='A real stone corridor. Never aliens.',
            participant_identity={'asset_id': 'reference', 'source': {}})
        frame = next(f for f in narration_frames(story, words, scenes, duration) if not f['preserve_existing'])
        data = additional_scene(parent, frame)
        self.assertEqual(data['time_layer'], parent['data']['time_layer'])
        self.assertIn('Never aliens.', data['audit_visual_direction'])
        self.assertEqual(data['participant_identity']['source'], frame['narration_source'])
        self.assertEqual(data['participant_identity']['asset_id'], 'reference')
        self.assertTrue(data['intentional_visual_reuse'])
        self.assertEqual(data['environment_reference_policy'], 'isolate_unbound_participants/1')
        self.assertEqual(data['narration_prompt_policy'], 'preserve_parent_scope/1')

    def test_subordinate_clause_cannot_lose_prevention_in_flow_prefix(self):
        from imagegen.domain import _forgotten_scene_priority
        parent = 'Это делалось для того, чтобы враги не смогли отравить колодец, сбросив туда павших животных.'
        scene = {'narration_prompt_policy':'preserve_parent_scope/1',
            'semantic_parent_source':{'quote':parent}, 'visible_people':0,
            'forgotten_cities_prompt_contract':{'source_quote':'сбросив туда павших животных',
                'visual_direction':'Repeated framing. '*100, 'source_constraints':['Long old context. '*100]}}
        prefix = _forgotten_scene_priority(scene)[:2200]
        self.assertIn('Preserve the stated purpose or prevention', prefix)
        self.assertIn('не смогли отравить', prefix)
        self.assertLess(prefix.index('prevention'), prefix.index('Depict:'))

    def test_attribution_and_denial_survive_long_parent_context(self):
        from imagegen.domain import _forgotten_scene_priority
        for quote, expected in [('Говорили о пришельцах, но это лишь гипотеза.', 'preserve attribution and uncertainty'),
                                ('Город ни разу не был взят врагом.', 'Do not enact the denied event')]:
            scene = {'narration_prompt_policy':'preserve_parent_scope/1',
                'semantic_parent_source':{'quote':quote+' More context. '*100},
                'forgotten_cities_prompt_contract':{'source_quote':'врагом', 'visual_direction':'Framing. '*200}}
            self.assertIn(expected, _forgotten_scene_priority(scene)[:2200])

    def test_threatened_participants_are_not_forced_into_a_neutral_cutaway(self):
        from imagegen.domain import verified_source_participants
        from imagegen.narration_expansion import neutral_additional
        story='Это делалось для того, чтобы враги не смогли отравить колодец, сбросив туда животных.'
        start=story.index('сбросив')
        data={'narration_parent':{'scene_id':'parent','revision':1}, 'narration_visual_mode':'neutral_detail',
            'semantic_parent_source':{'start':0,'end':len(story),'quote':story},
            'source_quote':story[start:], 'source_start':start, 'source_end':len(story)}
        self.assertTrue(neutral_additional(data,story))
        self.assertEqual(verified_source_participants(data,story), [])
        # Neither an unbound parent nor a factual action authorizes this mode.
        self.assertFalse(neutral_additional(data,'Другой текст'))
        factual='Враги отравили колодец.'
        data['semantic_parent_source']={'start':0,'end':len(factual),'quote':factual}
        self.assertFalse(neutral_additional(data,factual))

    def test_inherited_source_cast_is_verified_before_fallback_can_mine_mentions(self):
        import hashlib
        from imagegen.domain import verified_source_participants
        from imagegen.forgotten_cities.event_context import for_source
        story='Рядом располагалась семинария с каменными скамьями для учеников, а также зал городского совета.'
        start=story.index('а также')
        context=for_source(story,story,0)
        self.assertEqual(context['participants'], [])
        data={'narration_parent':{'scene_id':'parent','revision':1},
            'narration_prompt_policy':'preserve_parent_scope/1',
            'semantic_parent_source':context['source'], 'source_start':start,'source_end':len(story),'source_quote':story[start:],
            'forgotten_cities_prompt_contract':{'source_binding':'narrated_event','source_quote':story[start:],
                'source_roles':[],'narrative_context':context}}
        self.assertEqual(verified_source_participants(data,story), [])
        data['semantic_parent_source']={'start':0,'end':5,'quote':story[:5]}
        self.assertIsNone(verified_source_participants(data,story))

    def test_timeline_rejects_gap_overlap_wrong_text_and_nan(self):
        story, words, scenes, duration = self.source()
        frames = narration_frames(story, words, scenes, duration)
        for mutation in ('gap', 'text', 'nan'):
            altered = copy.deepcopy(frames)
            if mutation == 'gap': altered[1]['timing']['start'] += 1
            if mutation == 'text': altered[1]['narration_source']['quote'] = 'Wrong'
            if mutation == 'nan': altered[1]['timing']['duration'] = math.nan
            with self.assertRaises(DomainError): validate_timeline(story, altered, duration)

    def test_same_episodic_role_in_different_case_is_one_participant(self):
        from imagegen.domain import resolve_scene_presence_contract
        scene = {'source_quote': 'что означает глубокий колодец.', 'beat': 'Мужчина смотрит в проход.',
                 'composition': 'Мужчина смотрит в проход.', 'episodic_people': ['Мужчина'],
                 'visible_people': 1, 'raw_visible_people': 1, 'shot': 'medium', 'characters': []}
        result = resolve_scene_presence_contract(scene, [], [], story='Мужчина смотрит в проход, что означает глубокий колодец.')
        self.assertEqual(result['visible_people'], len(set(result['episodic_role_ids'])))
        self.assertEqual(result['visible_people'], 1)

    def test_enumeration_of_era_materials_or_place_does_not_add_three_people(self):
        from imagegen.domain import extract_scene_staging_count
        self.assertIsNone(extract_scene_staging_count({'composition': 'Keep the same era, participants and place.'}))
        self.assertIsNone(extract_scene_staging_count({'composition': 'Show stone, clay and timber.'}))
        self.assertEqual(extract_scene_staging_count({'composition': 'A woman, man and child watch the wall.'}), 3)


class NarrationPreservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_voice_workflow.VoiceWorkflowTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service, self.workflow, self.pid = self.fixture.service, self.fixture.workflow, self.fixture.pid
        self.fixture.fixture._upload_scene_images(self.pid)
        with patch.object(self.workflow, 'capability', return_value={'ready': True}):
            self.workflow.start(self.pid, {'audio_asset_id': self.fixture.audio['asset_id'], 'preserve_existing': True})
        with patch.object(self.workflow, 'recognize', return_value=self.fixture.asr): self.workflow.prepare(self.pid)
        self.manager = VoicePlans(self.workflow)
        self.plan = self.manager.get(self.pid, self.workflow.get(self.pid)['result']['candidate_plan_id'])

    def test_preview_and_apply_preserve_existing_scene_revisions_and_selected_images(self):
        before = self.service.detail(self.pid)['scenes']
        self.assertFalse(self.plan['apply_blocked_reason'])
        self.manager.apply(self.pid, self.plan['id'])
        after = self.service.detail(self.pid)['scenes']
        for old, new in zip(before, after):
            self.assertEqual(old['id'], new['id'])
            self.assertEqual(old['current_revision'], new['current_revision'])
            self.assertEqual(old['data'], new['data'])
            self.assertEqual(old['active_generation_id'], new['active_generation_id'])
            self.assertEqual(old['video_asset_id'], new['video_asset_id'])
            self.assertFalse(new['stale'])

    def test_applying_twice_is_idempotent(self):
        self.manager.apply(self.pid, self.plan['id'])
        before = self.service.detail(self.pid)
        self.manager.apply(self.pid, self.plan['id'])
        self.assertEqual(self.service.detail(self.pid), before)

    def test_scene_edit_after_preview_rejects_stale_plan(self):
        with self.service.db.transaction() as conn: conn.execute('UPDATE projects SET version=version+1 WHERE id=?', (self.pid,))
        with self.assertRaises(DomainError): self.manager.apply(self.pid, self.plan['id'])

    def test_selected_media_change_after_preview_rejects_plan(self):
        scene = self.service.detail(self.pid)['scenes'][0]
        with self.service.db.transaction() as conn: conn.execute('UPDATE scenes SET active_generation_id=NULL WHERE id=?', (scene['id'],))
        with self.assertRaises(DomainError): self.manager.apply(self.pid, self.plan['id'])

    def test_failed_transaction_rolls_back_scene_positions(self):
        before = [(s['id'],s['position']) for s in self.service.detail(self.pid)['scenes']]
        with patch.object(self.service.editing, 'validate', side_effect=DomainError('Invalid montage')):
            with self.assertRaises(DomainError): self.manager.apply(self.pid, self.plan['id'])
        self.assertEqual(before, [(s['id'],s['position']) for s in self.service.detail(self.pid)['scenes']])

    def test_partial_animation_cannot_be_assembled_as_complete_audio_timeline(self):
        self.manager.apply(self.pid, self.plan['id'])
        with self.assertRaises(DomainError): NarrationExpansion(self.workflow).montage(self.pid)

    def montage_fixture(self):
        video = self.fixture.fixture._create_valid_mp4(width=160, height=90, duration=1)
        for s in self.service.detail(self.pid)['scenes']:
            self.service.import_scene_video(s['id'], video, filename='source.mp4')
        old = self.service.editing.get(self.pid)
        doc = self.service.editing.sync(self.pid, {'expected_revision': old['revision'], 'add_only': True})['document']
        saved = self.service.editing.save(self.pid, {'expected_revision': old['revision'], 'document': doc})
        asset = self.service.asset(self.fixture.audio['asset_id'])
        manager = NarrationExpansion(self.workflow)
        plan = manager.prepare(self.pid, asset, self.plan['document']['alignment'])
        self.workflow.update(self.pid, state='plan_review', result_json={**self.workflow.get(self.pid)['result'], 'candidate_plan_id': plan['id']})
        self.manager.apply(self.pid, plan['id'])
        return saved

    def test_existing_montage_clips_are_retained_and_timed_to_complete_voice(self):
        from imagegen.editing import normalize
        old = self.montage_fixture()
        saved = NarrationExpansion(self.workflow).montage(self.pid)
        self.assertAlmostEqual(normalize(saved['document'])['duration_ms'], 1000)
        self.assertEqual([c['edit_clip_id'] for c in old['document']['clips']], [c['edit_clip_id'] for c in saved['document']['clips']])
        self.assertEqual([c['source_video_asset_id'] for c in old['document']['clips']], [c['source_video_asset_id'] for c in saved['document']['clips']])
        again = NarrationExpansion(self.workflow).montage(self.pid)
        self.assertEqual(again['revision'], saved['revision'])

    def test_manual_montage_order_is_not_silently_overwritten(self):
        self.montage_fixture()
        old = self.service.editing.get(self.pid)
        old['document']['clips'].reverse()
        saved = self.service.editing.save(self.pid, {'expected_revision': old['revision'], 'document': old['document']})
        with self.assertRaises(DomainError): NarrationExpansion(self.workflow).montage(self.pid)
        self.assertEqual(saved['document'], self.service.editing.get(self.pid)['document'])

    def test_export_rejects_video_that_would_silently_truncate_voice(self):
        self.montage_fixture()
        old = self.service.editing.get(self.pid)
        old['document']['audio_tracks'][0]['offset_ms'] = 2000
        saved = self.service.editing.save(self.pid, {'expected_revision': old['revision'], 'document': old['document']})
        with self.assertRaisesRegex(DomainError, 'раньше озвучки'):
            self.fixture.app.edit_exports.start(self.pid, {'expected_revision': saved['revision']})

    def test_storyboard_assembly_keeps_voice_duration_and_selected_sources(self):
        import av
        self.montage_fixture()
        old = self.service.editing.get(self.pid)
        old['document']['export_settings'].update(width=1920, height=1080)
        self.service.editing.save(self.pid, {'expected_revision': old['revision'], 'document': old['document']})
        before = [(s['id'], s['active_generation_id'], s['video_asset_id']) for s in self.service.detail(self.pid)['scenes']]
        result = self.service.assemble_animation_timeline(self.pid)
        asset = self.service.asset(result['asset_id'])
        with av.open(str(self.service.root / asset['path'])) as movie:
            self.assertEqual(movie.streams.video[0].codec_context.name, 'h264')
            self.assertEqual(movie.streams.audio[0].codec_context.name, 'aac')
            self.assertEqual((movie.streams.video[0].width, movie.streams.video[0].height), (1920, 1080))
            self.assertAlmostEqual(float(movie.duration / av.time_base), 1, delta=1/24)
        self.assertEqual(before, [(s['id'], s['active_generation_id'], s['video_asset_id']) for s in self.service.detail(self.pid)['scenes']])

    def test_flow_queue_does_not_compile_finished_scenes(self):
        detail = self.service.detail(self.pid)
        detail['channel_profile'] = {'profile_type': 'forgotten_cities'}
        with patch.object(self.service, '_ground_forgotten_scene', side_effect=lambda s, p:s), \
                patch.object(self.service, 'scene_snapshot', wraps=self.service.scene_snapshot) as snapshots:
            result = self.service.project_prompts(self.pid, _detail=detail)
        self.assertEqual(result['scenes'], [])
        snapshots.assert_not_called()

    def test_flow_queue_compiles_only_pending_scene(self):
        detail = self.service.detail(self.pid)
        detail['channel_profile'] = {'profile_type': 'forgotten_cities'}
        scene = detail['scenes'][-1]
        with self.service.db.transaction() as conn:
            self.service.insert_generation(conn, self.pid, 'scene', {'scene': scene['data']},
                scene_id=scene['id'], scene_revision=scene['current_revision'])
        with patch.object(self.service, '_ground_forgotten_scene', side_effect=lambda s, p:s), \
                patch.object(self.service, 'scene_snapshot', wraps=self.service.scene_snapshot) as snapshots:
            result = self.service.project_prompts(self.pid, _detail=detail)
        self.assertEqual([s['scene_id'] for s in result['scenes']], [scene['id']])
        self.assertEqual(snapshots.call_count, 1)
