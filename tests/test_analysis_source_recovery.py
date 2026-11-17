import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from imagegen.domain import (
    DomainError, MAX_SCENES, validate_analysis, story_segments,
    normalize_text_for_matching, find_quote_in_story
)
from imagegen.providers import (
    attach_exact_sources, normalize_analysis, expand_storyboard_scenes,
    chunk_segments, allocate_scene_counts, subdivide_analysis_jobs, select_story_segments,
    LlamaCppProvider, GroqProvider
)


class AnalysisSourceRecoveryTests(unittest.TestCase):

    def setUp(self):
        self.story = (
            "Мартин и Лука работают в кузнице ранним утром. "
            "Мартин вытягивает длинный раскалённый клинок из горна. "
            "Лука берет тяжелый молот и наносит первый точный удар. "
            "Искры разлетаются по всей полутемной мастерской. "
            "Они погружают готовое лезвие в большую бочку с маслом."
        )

    def _base_analysis(self, scenes):
        return {
            'world': 'Средневековая кузница',
            'characters': [
                {'name': 'Мартин', 'description': 'Adult man, 40 years old, master blacksmith', 'wardrobe': {}},
                {'name': 'Лука', 'description': 'Adult man, 22 years old, apprentice blacksmith', 'wardrobe': {}}
            ],
            'locations': [
                {'name': 'Кузница', 'description': 'Деревянно-каменная кузница с горном и наковальней.'}
            ],
            'props': [],
            'scenes': scenes
        }

    def _make_scene(self, quote, beat='Blacksmiths working', characters=None):
        return {
            'source_quote': quote,
            'beat': beat,
            'characters': characters or ['Мартин'],
            'wardrobe': {},
            'location': 'Кузница',
            'location_ref': 'Кузница',
            'time': 'Morning',
            'shot': 'Medium shot',
            'emotion': 'focused and determined',
            'continuity_before': '',
            'continuity_after': '',
            'visible_people': 1
        }

    def test_exact_quote_matching(self):
        """1. Exact quote matches seamlessly and calculates precise offsets."""
        scenes = [
            self._make_scene('Мартин и Лука работают в кузнице ранним утром.'),
            self._make_scene('Мартин вытягивает длинный раскалённый клинок из горна.')
        ]
        data = self._base_analysis(scenes)
        validated = validate_analysis(data, self.story)
        self.assertEqual(2, len(validated['scenes']))
        self.assertEqual(0, validated['scenes'][0]['source_start'])
        self.assertEqual(len('Мартин и Лука работают в кузнице ранним утром.'), validated['scenes'][0]['source_end'])
        self.assertIn('Мартин вытягивает', validated['scenes'][1]['source_quote'])

    def test_quote_with_cosmetic_differences(self):
        """2. Quotes with different dashes, quotes, whitespace, casing and punctuation are normalized and recovered."""
        # Unicode quotes («»), em-dash (—), double spaces, uppercase/lowercase variations, trailing punctuation missing
        dirty_quote_1 = '  «мартин и лука» — работают в кузнице   ранним утром  '
        dirty_quote_2 = 'МАРТИН ВЫТЯГИВАЕТ ДЛИННЫЙ РАСКАЛЁННЫЙ КЛИНОК ИЗ ГОРНА'
        dirty_quote_3 = 'Они погружают готовое лезвие в большую бочку с маслом'  # missing dot

        scenes = [
            self._make_scene(dirty_quote_1),
            self._make_scene(dirty_quote_2),
            self._make_scene(dirty_quote_3)
        ]
        data = self._base_analysis(scenes)
        validated = validate_analysis(data, self.story)

        self.assertEqual(3, len(validated['scenes']))
        # Verbatim quotes are recovered to exact original story spans
        self.assertEqual('Мартин и Лука работают в кузнице ранним утром.', validated['scenes'][0]['source_quote'])
        self.assertEqual('Мартин вытягивает длинный раскалённый клинок из горна.', validated['scenes'][1]['source_quote'])
        self.assertEqual('Они погружают готовое лезвие в большую бочку с маслом.', validated['scenes'][2]['source_quote'])
        self.assertTrue(validated['scenes'][0]['source_start'] < validated['scenes'][1]['source_start'] < validated['scenes'][2]['source_start'])

    def test_provider_rehydrates_blank_lines_around_markdown_separator(self):
        """Normalized model input maps back to the exact untouched story span."""
        story = "---\n\nВ шесть утра пришла утренняя смена."

        class SeparatorProvider(LlamaCppProvider):
            def __init__(self):
                super().__init__('http://127.0.0.1:11435')

            def _analyse_segments(self, project, segments, target):
                return {
                    'world': 'Охраняемое здание',
                    'characters': [],
                    'locations': [{'name': 'Пост охраны', 'description': 'Security desk.'}],
                    'props': [],
                    'scenes': [{
                        'source_id': 0,
                        'source_quote': segments[0],
                        'beat': 'The morning shift arrives at six.',
                        'characters': [],
                        'location': 'Пост охраны',
                        'location_ref': 'Пост охраны',
                        'visible_people': 0,
                        'shot': 'Wide shot',
                        'emotion': 'calm',
                    }],
                }

        result = SeparatorProvider().analyse({
            'story': story,
            'style': 'cinematic',
            'mode': 'characters',
            'camera_mode': 'cinema',
        }, target=1)

        self.assertEqual(1, len(result['scenes']))
        self.assertEqual(story, result['scenes'][0]['source_quote'])
        self.assertEqual(0, result['scenes'][0]['source_start'])
        self.assertEqual(len(story), result['scenes'][0]['source_end'])

    def test_truncated_quote(self):
        """3. Shortened / truncated quotes (e.g. first half of sentence) are accurately mapped."""
        short_quote_1 = 'Мартин вытягивает длинный раскалённый клинок'  # dropped "из горна."
        short_quote_2 = 'Искры разлетаются по всей'  # dropped "полутемной мастерской."

        scenes = [
            self._make_scene(short_quote_1),
            self._make_scene(short_quote_2)
        ]
        data = self._base_analysis(scenes)
        validated = validate_analysis(data, self.story)

        self.assertEqual(2, len(validated['scenes']))
        self.assertEqual('Мартин вытягивает длинный раскалённый клинок из горна.', validated['scenes'][0]['source_quote'])
        self.assertEqual('Искры разлетаются по всей полутемной мастерской.', validated['scenes'][1]['source_quote'])

    def test_rephrased_quote_fuzzy_recovery(self):
        """4. Unambiguous rephrased quotes match via token overlap & sequence similarity."""
        rephrased_quote = 'Лука берет тяжелый молот и наносит точный первый удар по наковальне'

        scenes = [
            self._make_scene(rephrased_quote, characters=['Лука'])
        ]
        data = self._base_analysis(scenes)
        validated = validate_analysis(data, self.story)

        self.assertEqual(1, len(validated['scenes']))
        self.assertEqual('Лука берет тяжелый молот и наносит первый точный удар.', validated['scenes'][0]['source_quote'])

    def test_swapped_scene_order_auto_sorting(self):
        """5. Out-of-order scenes are automatically sorted chronologically by story offsets."""
        scene_last = self._make_scene('Они погружают готовое лезвие в большую бочку с маслом.', beat='Quenching blade')
        scene_first = self._make_scene('Мартин и Лука работают в кузнице ранним утром.', beat='Morning start')
        scene_mid = self._make_scene('Лука берет тяжелый молот и наносит первый точный удар.', beat='Striking metal')

        # Inverted order: last, first, mid
        scenes = [scene_last, scene_first, scene_mid]
        data = self._base_analysis(scenes)
        validated = validate_analysis(data, self.story)

        # Must be sorted chronologically: first, mid, last
        self.assertEqual(3, len(validated['scenes']))
        self.assertEqual('Мартин и Лука работают в кузнице ранним утром.', validated['scenes'][0]['source_quote'])
        self.assertEqual('Лука берет тяжелый молот и наносит первый точный удар.', validated['scenes'][1]['source_quote'])
        self.assertEqual('Они погружают готовое лезвие в большую бочку с маслом.', validated['scenes'][2]['source_quote'])
        self.assertTrue(validated['scenes'][0]['source_start'] < validated['scenes'][1]['source_start'] < validated['scenes'][2]['source_start'])

    def test_truly_hallucinated_quote_rejection(self):
        """6. Ungrounded / hallucinated quote is rejected with informative diagnostics."""
        hallucinated_quote = 'Космический звездолет приземлился на поверхность планеты Марс.'

        scenes = [
            self._make_scene('Мартин и Лука работают в кузнице ранним утром.'),
            self._make_scene(hallucinated_quote)
        ]
        data = self._base_analysis(scenes)

        with self.assertRaises(DomainError) as ctx:
            validate_analysis(data, self.story)

        err_msg = str(ctx.exception)
        self.assertIn('Цитата сцены 2 отсутствует в исходном тексте или не подтверждена источником', err_msg)
        self.assertIn('Космический звездолет', err_msg)
        self.assertIn('Ближайшие фрагменты', err_msg)

    def test_attach_exact_sources_with_string_and_missing_ids(self):
        """7. attach_exact_sources handles string IDs, missing IDs, raw quotes, and pacing expansion."""
        segments = story_segments(self.story)
        self.assertEqual(5, len(segments))

        raw_llm_data = {
            'characters': [{'name': 'Мартин', 'description': 'Blacksmith'}],
            'scenes': [
                {'source_id': '2', 'beat': 'Striking', 'characters': ['Мартин']},
                {'source_id': '[0]', 'beat': 'Morning', 'characters': ['Мартин']},
                {'source_quote': 'погружают готовое лезвие в большую бочку', 'beat': 'Oil quenching', 'characters': ['Мартин']}
            ]
        }

        processed = attach_exact_sources(raw_llm_data, segments)
        self.assertEqual(3, len(processed['scenes']))
        # Quotes are populated from segments in story order
        self.assertEqual(segments[0], processed['scenes'][0]['source_quote'])
        self.assertEqual(segments[2], processed['scenes'][1]['source_quote'])
        self.assertEqual(segments[4], processed['scenes'][2]['source_quote'])

    def test_multi_chunk_screenplay_merging(self):
        """8. Multi-chunk screenplay analyses merge without boundary errors."""
        # Create a story with 12 distinct segments
        sentences = [f"Событие номер {i}: персонаж выполняет важное действие {i}." for i in range(12)]
        long_story = " ".join(sentences)
        segments = story_segments(long_story)
        self.assertEqual(12, len(segments))

        chunks = chunk_segments(segments, maximum=120)  # split into multiple chunks
        self.assertTrue(len(chunks) >= 2)

        analyses = []
        for chunk in chunks:
            chunk_scenes = []
            for seg in chunk:
                chunk_scenes.append(self._make_scene(seg))
            chunk_data = self._base_analysis(chunk_scenes)
            chunk_story = " ".join(chunk)
            validated_chunk = validate_analysis(chunk_data, chunk_story)
            analyses.append(validated_chunk)

        merged = LlamaCppProvider._merge_analyses(analyses, long_story)
        self.assertEqual(12, len(merged['scenes']))
        for i in range(12):
            self.assertEqual(segments[i], merged['scenes'][i]['source_quote'])
            if i > 0:
                self.assertTrue(merged['scenes'][i-1]['source_start'] < merged['scenes'][i]['source_start'])

    def test_long_screenplay_scaling_up_to_2000_scenes(self):
        """9. Performance and correctness on large storyboard scaling up to 2000 scenes."""
        # Story with 100 sentences
        sentences = [f"Сцена {i}: мастер Мартин выполняет технологический шаг {i} в кузнице." for i in range(100)]
        story = " ".join(sentences)

        initial_scenes = [self._make_scene(sentences[i]) for i in range(100)]
        analysis_data = self._base_analysis(initial_scenes)
        validated = validate_analysis(analysis_data, story)

        # Expand to 2000 scenes
        expanded = expand_storyboard_scenes(validated, 2000, camera_mode='cinema')
        self.assertEqual(2000, len(expanded['scenes']))

        # Validate complete 2000-scene storyboard
        final_validated = validate_analysis(expanded, story)
        self.assertEqual(2000, len(final_validated['scenes']))
        # Ensure monotonically non-decreasing start positions
        for i in range(1, 2000):
            self.assertTrue(final_validated['scenes'][i-1]['source_start'] <= final_validated['scenes'][i]['source_start'])

    def test_groq_provider_analyse_with_fuzzy_llm_response(self):
        """10. GroqProvider end-to-end analysis succeeds when LLM outputs messy markdown/quotes."""
        provider = GroqProvider('gsk_valid')
        mock_response = {
            'choices': [{
                'finish_reason': 'stop',
                'message': {
                    'content': """```json
{
  "world": "Кузница",
  "characters": [
    {"name": "Мартин", "description": "Adult man, 40 years old, master blacksmith", "wardrobe": {}},
    {"name": "Лука", "description": "Adult man, 22 years old, apprentice", "wardrobe": {}}
  ],
  "locations": [
    {"name": "Кузница", "description": "Деревянно-каменная кузница."}
  ],
  "props": [],
  "scenes": [
    {
      "source_id": "1",
      "beat": "Martin pulls the hot blade",
      "characters": ["Мартин"],
      "wardrobe": {},
      "location": "Кузница",
      "location_ref": "Кузница",
      "time": "Morning",
      "shot": "Medium shot",
      "emotion": "focused",
      "continuity_before": "",
      "continuity_after": "",
      "visible_people": 1
    },
    {
      "source_id": "0",
      "beat": "Blacksmiths at work early morning",
      "characters": ["Мартин", "Лука"],
      "wardrobe": {},
      "location": "Кузница",
      "location_ref": "Кузница",
      "time": "Morning",
      "shot": "Wide establishing",
      "emotion": "calm",
      "continuity_before": "",
      "continuity_after": "",
      "visible_people": 2
    }
  ]
}
```"""
                }
            }]
        }

        project = {
            'mode': 'characters',
            'style': 'cinematic realism',
            'idea': 'Blacksmith masters',
            'story': self.story,
            'camera_mode': 'cinema',
            'camera_pacing': 'steady'
        }

        with patch.object(provider, '_chat_completion', return_value=mock_response):
            result = provider.analyse(project, target=2)

        self.assertEqual(2, len(result['scenes']))
        # With fewer frames than source events, the selection spans the whole
        # story while retaining chronological order and the exact ending.
        self.assertEqual('Мартин и Лука работают в кузнице ранним утром.', result['scenes'][0]['source_quote'])
        self.assertEqual('Мартин вытягивает длинный раскалённый клинок из горна.', result['scenes'][1]['source_quote'])
        self.assertEqual(0, result['scenes'][0]['source_start'])
        self.assertTrue(result['scenes'][0]['source_start'] < result['scenes'][1]['source_start'])


    def test_groq_retries_an_empty_scene_array_for_only_the_current_chunk(self):
        provider = GroqProvider('gsk_valid')
        project = {
            'mode': 'characters', 'style': 'cinematic realism', 'idea': '',
            'story': 'Дэниел выходит на маршрут.', 'camera_mode': 'cinema', 'camera_pacing': 'steady'
        }
        empty = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'world': 'Northwood', 'characters': [], 'locations': [], 'props': [], 'scenes': []
        })}}]}
        valid = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            'world': 'Northwood', 'characters': [], 'locations': [], 'props': [],
            'scenes': [{
                'source_id': 0, 'beat': 'Daniel starts the route.', 'characters': [], 'wardrobe': {},
                'location': 'Northwood', 'location_ref': '', 'time': 'Morning', 'shot': 'Wide shot',
                'emotion': 'calm', 'continuity_before': '', 'continuity_after': '',
                'visible_people': 0, 'props': []
            }]
        })}}]}
        with patch.object(provider, '_chat_completion', side_effect=[empty, valid]) as completion:
            result = provider._analyse_segments(project, story_segments(project['story']), 1)
        self.assertEqual(2, completion.call_count)
        self.assertEqual('Дэниел выходит на маршрут.', result['scenes'][0]['source_quote'])

    def test_groq_repairs_a_chunk_rejected_for_missing_scene_location(self):
        provider = GroqProvider('gsk_valid')
        project = {
            'mode': 'characters', 'style': 'cinematic realism', 'idea': '',
            'story': 'Дэниел прибыл на мокрый причал.',
            'camera_mode': 'cinema', 'camera_pacing': 'steady'
        }
        base = {
            'world': 'Coastal island',
            'characters': [{
                'name': 'Дэниел',
                'description': 'Entity type: human; adult man, 32 years old, lean build, dark hair.',
                'wardrobe': {},
            }],
            # Two catalog entries prevent the deterministic single-location
            # fallback from hiding the missing scene binding in the first reply.
            'locations': [
                {'name': 'Береговая зона', 'description': 'Wet wooden coastal pier.'},
                {'name': 'Маяк', 'description': 'Stone lighthouse on the cliff.'},
            ],
            'props': [],
        }
        invalid = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            **base,
            'scenes': [{
                'source_id': 0, 'beat': 'Daniel arrives at the wet pier.',
                'characters': ['Дэниел'], 'wardrobe': {},
                'location': '', 'location_ref': '', 'time': 'Morning',
                'shot': 'Wide establishing shot', 'emotion': 'focused',
                'continuity_before': '', 'continuity_after': '',
                'visible_people': 1, 'props': [],
            }],
        }, ensure_ascii=False)}}]}
        repaired = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
            **base,
            'scenes': [{
                'source_id': 0, 'beat': 'Daniel arrives at the wet pier.',
                'characters': ['Дэниел'], 'wardrobe': {},
                'location': 'Береговая зона', 'location_ref': 'Береговая зона', 'time': 'Morning',
                'shot': 'Wide establishing shot', 'emotion': 'focused',
                'continuity_before': '', 'continuity_after': '',
                'visible_people': 1, 'props': [],
            }],
        }, ensure_ascii=False)}}]}

        with patch.object(provider, '_chat_completion', side_effect=[invalid, repaired]) as completion:
            result = provider._analyse_segments(project, story_segments(project['story']), 1)

        self.assertEqual(2, completion.call_count)
        self.assertEqual('Береговая зона', result['scenes'][0]['location_ref'])
        repair_instruction = completion.call_args_list[1].args[0]['messages'][-1]['content']
        self.assertIn('отсутствует локация', repair_instruction)

    def test_long_story_keeps_more_than_six_distinct_locations(self):
        segments = [f'Событие {i} происходит в локации Место {i}.' for i in range(1, 13)]
        story = ' '.join(segments)
        data = {
            'world': 'A journey through many physical settings.',
            'characters': [],
            'locations': [
                {'name': f'Место {i}', 'description': f'Distinct physical location {i}.'}
                for i in range(1, 13)
            ],
            'props': [],
            'scenes': [{
                'source_quote': segment,
                'beat': f'Atmospheric view of location {i}.',
                'characters': [], 'wardrobe': {},
                'location': f'Место {i}', 'location_ref': f'Место {i}',
                'time': 'Day', 'shot': 'Wide establishing shot',
                'shot_type': 'atmosphere', 'emotion': 'calm',
                'continuity_before': '', 'continuity_after': '',
                'visible_people': 0, 'props': [],
            } for i, segment in enumerate(segments, 1)],
        }
        normalized = normalize_analysis(data)
        validated = validate_analysis(normalized, story)
        self.assertEqual(12, len(validated['locations']))
        self.assertEqual(
            [f'Место {i}' for i in range(1, 13)],
            [scene['location_ref'] for scene in validated['scenes']],
        )

    def test_scene_budget_samples_the_entire_story_instead_of_truncating_the_ending(self):
        segments = [f'Exact event {i}.' for i in range(40)]
        selected = select_story_segments(segments, 10)
        self.assertEqual(10, len(selected))
        self.assertEqual('Exact event 0.', selected[0])
        self.assertEqual('Exact event 39.', selected[-1])
        indices = [segments.index(value) for value in selected]
        self.assertEqual(indices, sorted(set(indices)))
        self.assertTrue(all(3 <= right - left <= 5 for left, right in zip(indices, indices[1:])))

    def test_document_surface_is_never_promoted_to_a_location(self):
        data = {
            'world': 'Island archive', 'characters': [], 'props': [],
            'locations': [
                {'name': 'Archive Room', 'description': 'A physical archive room.'},
                {'name': 'Magazine page', 'description': 'A page carrying a lighthouse diagram.'},
            ],
            'scenes': [
                self._make_scene(self.story.split('. ')[0] + '.', characters=[]),
            ],
        }
        data['scenes'][0].update({
            'location': 'Magazine page', 'location_ref': 'Magazine page',
            'visible_people': 0,
        })
        normalized = normalize_analysis(data)
        self.assertEqual(['Archive Room'], [item['name'] for item in normalized['locations']])
        self.assertEqual('', normalized['scenes'][0]['location_ref'])

    def test_object_only_followup_inherits_location_instead_of_inventing_a_room(self):
        first = self._make_scene('Мартин и Лука работают в кузнице ранним утром.')
        first.update({'location': 'Hotel Basement', 'location_ref': 'Hotel Basement'})
        second = self._make_scene('Мартин вытягивает длинный раскалённый клинок из горна.')
        second.update({'location': 'Remote speaker setup', 'location_ref': 'Remote speaker setup'})
        data = {
            'world': 'Old hotel', 'characters': self._base_analysis([])['characters'], 'props': [],
            'locations': [
                {'name': 'Hotel Basement', 'description': 'A physical cellar.'},
                {'name': 'Remote speaker setup', 'description': 'Audio equipment.'},
            ],
            'scenes': [first, second],
        }
        normalized = normalize_analysis(data)
        self.assertEqual(['Hotel Basement'], [item['name'] for item in normalized['locations']])
        self.assertEqual('Hotel Basement', normalized['scenes'][1]['location_ref'])


if __name__ == '__main__':
    unittest.main()
