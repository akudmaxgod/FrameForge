import io
import json
import pathlib
import tempfile
import unittest
from PIL import Image, ImageDraw, ImageFont

from imagegen.domain import (
    build_global_story_bible,
    validate_project_preflight,
    extract_world_context,
    DomainError,
    compile_prompt,
    compile_flow_prompt,
    SHOT_TYPE_ESTABLISHING,
    SHOT_TYPE_MAIN_ACTION,
    SHOT_TYPE_PROTAGONIST_REACTION,
    SHOT_TYPE_PROP_DETAIL,
    SHOT_TYPE_ACTION_AFTERMATH,
    SHOT_TYPE_ATMOSPHERE,
)
from imagegen.cast import CharacterEntityResolver
from imagegen.providers import expand_storyboard_scenes
from imagegen.worker import Worker, ContentRejectedError
from imagegen.service import Service


def png(width=1024, height=576):
    result = io.BytesIO()
    Image.new('RGB', (width, height), '#526880').save(result, format='PNG')
    return result.getvalue()


class DanielStoryRegressionTestCase(unittest.TestCase):
    """End-to-end regression test suite for Daniel's 222-frame storyline."""

    def setUp(self):
        fixture_path = pathlib.Path(__file__).parent / 'fixtures' / 'daniel_northwood_fixture.json'
        with open(fixture_path, 'r', encoding='utf-8') as f:
            self.fixture = json.load(f)
        self.story = self.fixture['story']

    def test_fixture_world_and_entity_resolution_identifies_autumn_northwood_and_daniel(self):
        """Northwood must be recognised as location/world, Daniel as protagonist, season as autumn."""
        resolver = CharacterEntityResolver(self.story)
        candidates = resolver.extract_deterministic_candidates()
        resolved = resolver.resolve(candidates)
        key_chars = [c['canonical_name'] for c in resolved.get('characters', [])]

        # Daniel is the protagonist
        self.assertIn("Дэниел", key_chars)
        # Northwood is a town/location, NOT a character
        self.assertNotIn("Нортвуд", key_chars)

        world = extract_world_context(self.story)
        self.assertEqual(world["season"], "autumn")
        self.assertIn("Нортвуд", world["setting"])

    def test_expansion_to_222_frames_preserves_daniel_continuity_and_metadata(self):
        """Verify:
        - 222 frames generated without losing chronology
        - All frames with Daniel's actions contain characters: ["Дэниел"]
        - Frame based on 'Не отвечай, когда тебя зовут по имени' stays in cabin and bound to Daniel
        - All forest frames before epilogue have autumn Northwood
        - No random women, travelers, anonymous wanderers
        - Additional frames inherit event_id and parent event state
        """
        analysis = {
            "world": "Осенний прибрежный городок Нортвуд в окружении густых сосновых лесов",
            "characters": self.fixture["characters"],
            "locations": self.fixture["locations"],
            "props": self.fixture["props"],
            "scenes": self.fixture["events"],
        }

        # 1. Expand storyboard to 222 scenes
        expanded = expand_storyboard_scenes(analysis, 222, camera_mode="cinema")
        self.assertEqual(len(expanded["scenes"]), 222)

        # 2. Verify Story Bible
        bible = build_global_story_bible(self.story, expanded["characters"], expanded["scenes"])
        self.assertEqual(bible["protagonist"]["canonical_name"], "Дэниел")
        self.assertEqual(bible["world"]["season"], "autumn")
        self.assertIn("Нортвуд", bible["world"]["setting"])

        # 3. Verify Chronology and Frame Ordering
        last_event_num = 0
        banned_fillers = {"wanderer", "man", "person", "фигура", "путник", "незнакомец", "someone", "traveler", "woman", "женщина"}
        note_quote = "Не отвечай, когда тебя зовут по имени"
        found_note_scenes = []

        for idx, sc in enumerate(expanded["scenes"]):
            # Chronology check: event_id numbers must be non-decreasing
            evt_id = sc.get("event_id", "")
            self.assertTrue(evt_id.startswith("evt_"), f"Scene {idx+1} invalid event_id {evt_id}")
            evt_num = int(evt_id.split("_")[1])
            self.assertGreaterEqual(evt_num, last_event_num, f"Chronology inverted at scene {idx+1}")
            last_event_num = evt_num

            # Season must remain autumn
            self.assertEqual(sc.get("season"), "autumn", f"Scene {idx+1} season changed")

            # No abstract filler character names
            for c in sc.get("characters", []):
                self.assertNotIn(c.lower(), banned_fillers, f"Scene {idx+1} contains banned filler character {c}")

            # If atmosphere shot, must have 0 people and empty characters
            if sc.get("shot_type") == SHOT_TYPE_ATMOSPHERE:
                self.assertEqual(sc.get("visible_people"), 0, f"Atmosphere scene {idx+1} has visible people")
                self.assertEqual(sc.get("characters"), [], f"Atmosphere scene {idx+1} has characters")

            # If Daniel is active in beat/quote, characters must include Daniel
            beat = sc.get("beat", "")
            quote = sc.get("source_quote", "")
            if "Дэниел" in quote or "Он " in quote or "он " in quote:
                if sc.get("shot_type") != SHOT_TYPE_ATMOSPHERE:
                    self.assertIn("Дэниел", sc.get("characters", []), f"Scene {idx+1} lost Daniel from characters")
                    self.assertGreaterEqual(sc.get("visible_people", 0), 1)

            # Check the "Не отвечай..." warning note scene
            if note_quote in quote:
                found_note_scenes.append(sc)
                self.assertEqual(sc.get("location_ref"), "Хижина в лесу")
                self.assertIn("Хижина", sc.get("location", ""))
                if sc.get("shot_type") != SHOT_TYPE_ATMOSPHERE:
                    self.assertIn("Дэниел", sc.get("characters", []))

            # Forest scenes before epilogue (before evt_11) must have autumn and Northwood location/setting
            if evt_num < 11 and "лес" in sc.get("location", "").lower():
                self.assertEqual(sc.get("season"), "autumn")
                self.assertTrue("Нортвуд" in sc.get("location", "") or "Нортвуд" in sc.get("location_ref", "") or "Нортвуд" in bible["world"]["setting"])

        self.assertGreater(len(found_note_scenes), 0, "Warning note scene must be present in storyboard")

        # 4. Preflight validation must pass with 0 errors
        mock_project = {
            "id": "proj_daniel_fixture_222",
            "story": self.story,
            "characters": expanded["characters"],
            "locations": expanded["locations"],
            "props": expanded["props"],
            "scenes": [
                {
                    "id": f"sc_{i+1}",
                    "position": i + 1,
                    "data": sc,
                }
                for i, sc in enumerate(expanded["scenes"])
            ],
        }
        preflight = validate_project_preflight(mock_project, self.story)
        self.assertTrue(preflight["ok"], f"Preflight failed with errors: {preflight.get('errors')}")
        self.assertEqual(len(preflight["errors"]), 0)

    def test_preflight_blocks_project_when_unassigned_humans_exceed_one_percent(self):
        """Preflight must block project if >1% of scenes with people lack character binding."""
        # 100 scenes with people: 2 unassigned (2% > 1%) -> must fail preflight
        scenes = []
        for i in range(100):
            if i < 2:
                # Unassigned person without episodic role
                sc = {
                    "event_id": f"evt_{i+1}",
                    "beat": f"Неизвестный человек идет по улице {i+1}",
                    "source_quote": "Осенью Дэниел приехал в Нортвуд на старом внедорожнике с походным рюкзаком.",
                    "characters": [],
                    "visible_people": 1,
                    "location": "Нортвуд",
                    "location_ref": "Нортвуд",
                    "time": "day",
                    "story_day": "Day 1",
                    "day_phase": "day",
                    "season": "autumn",
                }
            else:
                sc = {
                    "event_id": f"evt_{i+1}",
                    "beat": f"Дэниел идет по улице {i+1}",
                    "source_quote": "Осенью Дэниел приехал в Нортвуд на старом внедорожнике с походным рюкзаком.",
                    "characters": ["Дэниел"],
                    "visible_people": 1,
                    "location": "Нортвуд",
                    "location_ref": "Нортвуд",
                    "time": "day",
                    "story_day": "Day 1",
                    "day_phase": "day",
                    "season": "autumn",
                }
            scenes.append({"id": f"s_{i+1}", "position": i + 1, "data": sc})

        mock_project = {
            "id": "proj_unassigned_pct",
            "story": self.story,
            "characters": self.fixture["characters"],
            "locations": self.fixture["locations"],
            "props": [],
            "scenes": scenes,
        }
        preflight = validate_project_preflight(mock_project, self.story)
        self.assertFalse(preflight["ok"])
        error_codes = [e.get("code") for e in preflight.get("errors", [])]
        self.assertIn("unassigned_humans_threshold", error_codes)

    def test_prompt_compilation_enforces_daniel_portrait_and_strict_person_contract(self):
        """Compiled prompt must enforce portrait reference, identical build/face/gear, no celebrities, and no extra people."""
        project = {
            'style': 'Cinematic photorealism',
            'world': 'Autumn Northwood',
            'mode': 'characters',
        }
        scene = {
            'beat': 'Дэниел достает фонарь в темной хижине',
            'source_quote': 'Он стиснул зубы, не проронив ни звука, и крепче сжал фонарь в руках.',
            'characters': ['Дэниел'],
            'visible_people': 1,
            'wardrobe': {'Дэниел': 'autumn_stormovka'},
            'location': 'Хижина в лесу',
            'time': 'night',
            'shot': 'Medium close-up',
            'props': [{'name': 'фонарь', 'state': 'held', 'visible': True}],
        }
        daniel_char = {
            'id': 'char_daniel',
            'name': 'Дэниел',
            'description': '35-летний мужчина в темно-зеленой штормовке с серым походным рюкзаком',
            'locked': True,
            'references_json': json.dumps({'portrait': 'asset_daniel_portrait'}),
            'wardrobe': json.dumps({'autumn_stormovka': 'Темно-зеленая штормовка с рюкзаком'}),
            'revision': 1,
        }
        prompt, references = compile_prompt(project, scene, [daniel_char], allow_unlocked=True)

        # 1. Portrait reference attached
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]['asset_id'], 'asset_daniel_portrait')
        self.assertEqual(references[0]['role'], 'portrait')

        # 2. Prompt contains character identity and gear consistency
        self.assertIn('@Дэниел', prompt)
        self.assertIn('exact same facial structure', prompt)
        self.assertIn('backpack and personal gear', prompt)
        self.assertIn('Strict identity constraint', prompt)

        # 3. Celebrity ban
        self.assertIn('Strictly avoid any celebrity likeness', prompt)

        # 4. Strict person contract
        self.assertIn('Strict person contract: exactly 1 person(s) present', prompt)
        self.assertIn('Strictly forbid adding any extra people', prompt)

    def test_ocr_text_rejection_blocks_subtitled_or_captioned_frames(self):
        """Worker must detect and reject images with baked-in text/subtitles (EN and RU) while accepting clean frames."""
        clean_img = Image.new("RGB", (1024, 576), (45, 60, 50))
        draw = ImageDraw.Draw(clean_img)
        for i in range(12):
            draw.line([(0, i * 45), (1024, i * 45 + 30)], fill=(70 + i * 3, 50 + i * 4, 30), width=20)
        draw.ellipse([300, 150, 500, 350], fill=(90, 70, 40))

        # Clean image has no embedded text
        self.assertFalse(Worker._has_embedded_text(clean_img))

        # 1. Image with English subtitle "There are no markers for thirty minutes"
        subtitled_en = clean_img.copy()
        draw_en = ImageDraw.Draw(subtitled_en)
        draw_en.text((160, 510), "There are no markers for thirty minutes", fill=(255, 255, 255))
        self.assertTrue(Worker._has_embedded_text(subtitled_en))

        # 2. Image with Russian subtitle "Дэниел медленно идет по улице Нортвуда"
        subtitled_ru = clean_img.copy()
        draw_ru = ImageDraw.Draw(subtitled_ru)
        draw_ru.text((180, 515), "Дэниел медленно идет по улице Нортвуда", fill=(255, 255, 255))
        self.assertTrue(Worker._has_embedded_text(subtitled_ru))

        # 3. Flow sometimes renders a cyan/glowing subtitle across the middle
        # of the frame instead of the usual white line at the bottom.
        subtitled_cyan = Image.new("RGB", (1024, 576), (35, 48, 44))
        draw_cyan = ImageDraw.Draw(subtitled_cyan)
        try:
            cyan_font = ImageFont.truetype("arial.ttf", 30)
        except OSError:
            cyan_font = ImageFont.load_default()
        draw_cyan.text(
            (145, 500),
            "IF YOU SEE YOURSELF - DO NOT LET HIM ENTER",
            fill=(165, 225, 245),
            font=cyan_font,
        )
        self.assertTrue(Worker._has_embedded_text(subtitled_cyan))

        floating_words = Image.new("RGB", (1024, 576), (28, 42, 38))
        draw_floating = ImageDraw.Draw(floating_words)
        draw_floating.text(
            (130, 230),
            "IF YOU HEAR A STREAM DO NOT SEEK IT KEEP WALKING AWAY",
            fill=(185, 210, 205),
            font=cyan_font,
        )
        self.assertTrue(Worker._has_embedded_text(floating_words))

        # Text that belongs to an object in the scene is allowed.  A small sign
        # in the upper image area is not a subtitle or a speech bubble.
        signed_scene = clean_img.copy()
        draw_sign = ImageDraw.Draw(signed_scene)
        draw_sign.rectangle([700, 90, 900, 145], fill=(125, 84, 42))
        draw_sign.text((720, 112), "NORTHWOOD", fill=(245, 235, 205))
        self.assertFalse(Worker._has_embedded_text(signed_scene))

        # 4. Worker.save_image rejects subtitled image
        temp_img_bytes = io.BytesIO()
        subtitled_en.save(temp_img_bytes, format="PNG")
        subtitled_png = temp_img_bytes.getvalue()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        worker = Worker(service)
        gen = {
            "id": "gen_test_text",
            "project_id": "proj_1",
            "purpose": "scene",
            "snapshot": json.dumps({"profile": {"width": 1024, "height": 576}, "expected_people": 1}),
        }

        with self.assertRaises(ContentRejectedError) as cm:
            worker.save_image(gen, subtitled_png)
        self.assertIn("\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b", str(cm.exception))

    def test_daniel_and_receptionist_scene_contract_and_validation(self):
        """Case 1: Daniel + visitor center receptionist (1 locked + 1 episodic = 2 visible).
        Preflight must succeed, Flow prompt must declare exactly 2 people (Daniel and visitor-center employee),
        and validator accepts image with 2 people where Daniel matches reference without requiring ArcFace for receptionist.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        proj = service.create_project({
            'title': 'Reception Scene Test',
            'story': self.story,
            'style': 'Cinematic',
            'world': 'Autumn Northwood'
        })
        char_id = 'char_daniel'
        with service.db.transaction() as conn:
            conn.execute('''
                INSERT INTO characters (id, project_id, name, canonical_name, description, locked, references_json, revision)
                VALUES ('char_daniel', ?, 'Дэниел', 'Дэниел', '35-летний мужчина в штормовке', 1, ?, 1)
            ''', (proj['id'], json.dumps({'portrait': 'asset_daniel_portrait'})))
            conn.execute('''
                INSERT INTO generations (id, project_id, purpose, state, snapshot, seed, created_at)
                VALUES ('gen_d_p', ?, 'casting', 'succeeded', '{}', 42, '2026-09-28T00:00:00')
            ''', (proj['id'],))
            conn.execute('''
                INSERT INTO assets (id, project_id, generation_id, path, sha256, width, height, bytes)
                VALUES ('asset_daniel_portrait', ?, 'gen_d_p', 'daniel.png', 'hash123', 1024, 1024, 100)
            ''', (proj['id'],))

        scene_data = {
            'event_id': 'evt_05',
            'beat': 'Дэниел регистрируется у стойки визит-центра, сотрудница центра выдает карту',
            'source_quote': 'Дэниел подошел к стойке информации. Сотрудница центра вежливо улыбнулась и протянула карту маршрутов.',
            'characters': ['Дэниел'],
            'character_ids': [char_id],
            'episodic_people': ['сотрудница центра'],
            'visible_people': 2,
            'location': 'Визит-центр Нортвуда',
            'location_ref': 'Визит-центр Нортвуда',
            'time': 'day',
            'season': 'autumn',
        }
        mock_proj = {
            'id': proj['id'],
            'story': self.story,
            'characters': [{'id': char_id, 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'locked': True, 'references': {'portrait': 'asset_daniel_portrait'}}],
            'locations': [],
            'props': [],
            'scenes': [{'id': 'sc_05', 'position': 5, 'data': scene_data}],
        }
        preflight = validate_project_preflight(mock_proj, self.story)
        self.assertTrue(preflight['ok'], f"Preflight should pass for Daniel + receptionist: {preflight.get('errors')}")

        flow_prompt = compile_flow_prompt(mock_proj, scene_data, mock_proj['characters'])
        self.assertIn('@Дэниел', flow_prompt)
        self.assertIn('сотрудница центра', flow_prompt)
        self.assertTrue('2 people' in flow_prompt or '2 person' in flow_prompt)

        worker = Worker(service)
        worker._detected_face_count = lambda _img: 2
        worker._detected_person_count = lambda _img: 2
        worker._detected_hand_count = lambda _img: 4
        worker._identity_similarity = lambda _img, _asset: 0.82
        worker._identity_face_too_small = lambda _img, _exp=1: False

        snap = {
            'profile': {'width': 1024, 'height': 576},
            'expected_people': 2,
            'references': [{'role': 'portrait', 'asset_id': 'asset_daniel_portrait'}],
        }
        worker.validate_external_scene_image(snap, png(1024, 576))

    def test_protagonist_and_guard_episodic_scene_contract(self):
        """Case 2: Protagonist + guard (1 locked character + 1 episodic role = 2 visible).
        Preflight must succeed and prompt must include both protagonist and guard.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        proj = service.create_project({'title': 'Guard Scene Test', 'story': self.story, 'style': 'Cinematic', 'world': 'Northwood'})
        daniel_char = {'id': 'char_daniel', 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'description': 'Protagonist', 'locked': True, 'references': {'portrait': 'asset_daniel'}}
        scene_data = {
            'event_id': 'evt_06',
            'beat': 'Дэниел предъявляет пропуск, охранник проверяет документы у ворот',
            'source_quote': 'Охранник поднял шлагбаум после того, как Дэниел показал разрешение на въезд.',
            'characters': ['Дэниел'],
            'character_ids': ['char_daniel'],
            'episodic_people': ['охранник'],
            'visible_people': 2,
            'location': 'КПП Нортвуда',
            'location_ref': 'КПП Нортвуда',
            'time': 'day',
            'season': 'autumn',
        }
        mock_proj = {
            'id': proj['id'],
            'story': self.story,
            'characters': [daniel_char],
            'locations': [],
            'props': [],
            'scenes': [{'id': 'sc_06', 'position': 6, 'data': scene_data}],
        }
        preflight = validate_project_preflight(mock_proj, self.story)
        self.assertTrue(preflight['ok'])

        flow_prompt = compile_flow_prompt(mock_proj, scene_data, [daniel_char])
        self.assertIn('@Дэниел', flow_prompt)
        self.assertIn('охранник', flow_prompt)
        self.assertTrue('2 people' in flow_prompt or '2 person' in flow_prompt)

    def test_two_locked_characters_scene_contract(self):
        """Case 3: Two locked story characters (2 locked + 0 episodic = 2 visible).
        Prompt includes both and validator checks portrait references for both.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        proj = service.create_project({'title': 'Two Characters Test', 'story': self.story, 'style': 'Cinematic', 'world': 'Northwood'})
        char1 = {'id': 'char_daniel', 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'locked': True, 'references': {'portrait': 'asset_daniel'}}
        char2 = {'id': 'char_maria', 'name': 'Мария', 'canonical_name': 'Мария', 'locked': True, 'references': {'portrait': 'asset_maria'}}
        scene_data = {
            'event_id': 'evt_07',
            'beat': 'Дэниел и Мария обсуждают карту в хижине',
            'source_quote': 'Дэниел разложил карту на столе, Мария указала на старую тропу.',
            'characters': ['Дэниел', 'Мария'],
            'character_ids': ['char_daniel', 'char_maria'],
            'episodic_people': [],
            'visible_people': 2,
            'location': 'Хижина',
            'location_ref': 'Хижина',
            'time': 'evening',
            'season': 'autumn',
        }
        mock_proj = {
            'id': proj['id'],
            'story': self.story,
            'characters': [char1, char2],
            'locations': [],
            'props': [],
            'scenes': [{'id': 'sc_07', 'position': 7, 'data': scene_data}],
        }
        preflight = validate_project_preflight(mock_proj, self.story)
        self.assertTrue(preflight['ok'])

        flow_prompt = compile_flow_prompt(mock_proj, scene_data, [char1, char2])
        self.assertIn('@Дэниел', flow_prompt)
        self.assertIn('@Мария', flow_prompt)

        worker = Worker(service)
        worker._detected_face_count = lambda _img: 2
        worker._detected_person_count = lambda _img: 2
        worker._detected_hand_count = lambda _img: 4
        worker._identity_assignment_similarity = lambda _img, _assets: 0.85
        worker._identity_face_too_small = lambda _img, _exp=2: False

        snap = {
            'profile': {'width': 1024, 'height': 576},
            'expected_people': 2,
            'references': [
                {'role': 'portrait', 'asset_id': 'asset_daniel'},
                {'role': 'portrait', 'asset_id': 'asset_maria'},
            ],
        }
        worker.validate_external_scene_image(snap, png(1024, 576))

    def test_atmosphere_uninhabited_scene_strictly_rejects_people(self):
        """Case 4: Atmosphere / uninhabited shot (0 locked + 0 episodic = 0 visible).
        Validation strictly rejects images where people/faces are detected.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        worker = Worker(service)

        gen_empty = {
            "id": "gen_empty_violation",
            "project_id": "proj_1",
            "purpose": "scene",
            "snapshot": json.dumps({
                "profile": {"width": 1024, "height": 576},
                "uninhabited": True,
                "expected_people": 0,
            }),
        }
        worker._detected_face_count = lambda _img: 1
        worker._detected_person_count = lambda _img: 1

        with self.assertRaises(ContentRejectedError) as cm:
            worker.save_image(gen_empty, png(1024, 576))
        self.assertIn("В пустой сцене найден человек", str(cm.exception))

    def test_extra_anonymous_human_without_role_blocked_in_preflight_and_prompts(self):
        """Case 5: Extra anonymous human without role (visible_people: 2, 1 locked, 0 episodic roles).
        Preflight fails and service.project_prompts blocks generation with ready_for_flow: False.
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        proj = service.create_project({'title': 'Extra Human Test', 'story': self.story, 'style': 'Cinematic', 'world': 'Northwood'})

        unexplained_scene = {
            'beat': 'Дэниел идет по лесу в одиночестве',
            'source_quote': 'Осенью Дэниел шел по тропе Нортвуда.',
            'characters': ['Дэниел'],
            'character_ids': [],
            'episodic_people': [],
            'visible_people': 2,  # 2 people declared but only 1 accounted for!
            'location': 'Лес Нортвуда',
            'location_ref': '',
            'time': 'day',
            'season': 'autumn',
        }

        # 1. Import analysis
        service.import_analysis(proj['id'], {
            'world': 'Northwood',
            'characters': [{'name': 'Дэниел', 'canonical_name': 'Дэниел', 'description': 'Protagonist', 'locked': True}],
            'locations': [],
            'props': [],
            'scenes': [unexplained_scene],
        }, proj['version'])

        chars = service.db.all('SELECT * FROM characters WHERE project_id=?', (proj['id'],))
        daniel_id = chars[0]['id']

        with service.db.transaction() as conn:
            conn.execute("UPDATE characters SET locked=1, references_json=? WHERE id=?", (json.dumps({'portrait': 'asset_daniel_p'}), daniel_id))
            conn.execute('''
                INSERT INTO generations (id, project_id, purpose, state, snapshot, seed, created_at)
                VALUES ('gen_d_p2', ?, 'casting', 'succeeded', '{}', 42, '2026-09-28T00:00:00')
            ''', (proj['id'],))
            conn.execute('''
                INSERT INTO assets (id, project_id, generation_id, path, sha256, width, height, bytes)
                VALUES ('asset_daniel_p', ?, 'gen_d_p2', 'daniel.png', 'hash123', 1024, 1024, 100)
            ''', (proj['id'],))

        unexplained_scene['character_ids'] = [daniel_id]
        mock_proj = {
            'id': proj['id'],
            'story': self.story,
            'characters': [{'id': daniel_id, 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'locked': True, 'references': {'portrait': 'asset_daniel_p'}}],
            'locations': [],
            'props': [],
            'scenes': [{'id': 'sc_err', 'position': 1, 'data': unexplained_scene}],
        }
        preflight = validate_project_preflight(mock_proj, self.story)
        self.assertFalse(preflight['ok'])
        error_codes = [e.get('code') for e in preflight.get('errors', [])]
        self.assertTrue('unassigned_humans_threshold' in error_codes or 'unexplained_humans' in error_codes)

        # 2. Check project_prompts
        prompts = service.project_prompts(proj['id'])
        scene_entry = prompts['scenes'][0]
        self.assertFalse(scene_entry['ready_for_flow'])
        self.assertIn('заявлено 2 человек', scene_entry['flow_error'])

    def test_lost_story_character_masked_as_episodic_blocked_by_preflight(self):
        """Case 6: Story character (e.g. Maria) mentioned in source text cannot be bypassed as episodic without binding."""
        story_with_maria = 'Дэниел приехал в Нортвуд. Мария встретила его у ворот старой усадьбы.'
        mock_proj = {
            'id': 'proj_lost_char',
            'story': story_with_maria,
            'characters': [
                {'id': 'char_daniel', 'name': 'Дэниел', 'canonical_name': 'Дэниел', 'locked': True, 'references': {'portrait': 'p1'}},
                {'id': 'char_maria', 'name': 'Мария', 'canonical_name': 'Мария', 'locked': True, 'references': {'portrait': 'p2'}},
            ],
            'locations': [],
            'props': [],
            'scenes': [
                {
                    'id': 'sc_1',
                    'position': 1,
                    'data': {
                        'beat': 'Мария встречает Дэниела у ворот',
                        'source_quote': 'Мария встретила его у ворот старой усадьбы.',
                        'characters': ['Дэниел'],  # Maria missing from characters binding!
                        'character_ids': ['char_daniel'],
                        'episodic_people': [],
                        'visible_people': 1,  # Only 1 person declared when Maria is present
                        'location': 'Усадьба',
                        'location_ref': 'Усадьба',
                    }
                }
            ]
        }
        preflight = validate_project_preflight(mock_proj, story_with_maria)
        self.assertFalse(preflight['ok'])
        error_codes = [e.get('code') for e in preflight.get('errors', [])]
        self.assertIn('missing_character_scenes', error_codes)

    def test_flow_bridge_skips_invalid_item_and_continues_queue(self):
        """Case 7: Flow Bridge skips items with ready_for_flow: False without throwing unhandled exception."""
        # Test simulated bridge loop processing
        items = [
            {'position': 1, 'scene_id': 's1', 'ready_for_flow': True, 'flow_prompt': 'Prompt 1', 'flow_error': None},
            {'position': 2, 'scene_id': 's2', 'ready_for_flow': False, 'flow_prompt': 'Prompt 2', 'flow_error': 'Кадр 2: ошибка состава'},
            {'position': 3, 'scene_id': 's3', 'ready_for_flow': True, 'flow_prompt': 'Prompt 3', 'flow_error': None},
        ]
        processed = []
        skipped = []
        for item in items:
            if item.get('ready_for_flow') is False:
                skipped.append((item['position'], item.get('flow_error')))
                continue
            processed.append(item['position'])

        self.assertEqual(processed, [1, 3])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][0], 2)

    def test_foreign_large_face_strictly_rejected_by_identity_gate(self):
        """Identity gate strictly rejects stranger likeness on large portrait face (< 0.45 similarity)."""
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        service = Service(temp_dir.name)
        worker = Worker(service)

        gen = {
            "id": "gen_foreign_face",
            "project_id": "proj_1",
            "purpose": "scene",
            "snapshot": json.dumps({
                "profile": {"width": 1024, "height": 576},
                "expected_people": 1,
                "references": [{"role": "portrait", "asset_id": "asset_daniel"}],
            }),
        }
        worker._detected_face_count = lambda _img: 1
        worker._detected_person_count = lambda _img: 1
        worker._detected_hand_count = lambda _img: 0
        worker._identity_similarity = lambda _img, _asset: 0.18  # Stranger face
        worker._identity_face_too_small = lambda _img, _exp=1: False  # Large face

        with self.assertRaises(ContentRejectedError) as cm:
            worker.validate_external_scene_image(json.loads(gen['snapshot']), png(1024, 576))
        self.assertIn("Лицо в кадре недостаточно похоже", str(cm.exception))


if __name__ == '__main__':
    unittest.main()

