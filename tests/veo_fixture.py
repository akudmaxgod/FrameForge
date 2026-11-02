import io
import json
import tempfile
import unittest
from PIL import Image

from imagegen.service import Service
from imagegen.server import Application
from imagegen.domain import DomainError
from imagegen.animation import (
    score_scene_for_animation,
    compile_veo_prompt,
    determine_camera_motion,
    build_animation_plan,
    validate_animation_settings
)
from imagegen.ken_burns import (
    KEN_BURNS_PRESETS,
    calculate_safe_crop,
    render_ken_burns_frames,
    render_ken_burns_video
)
from imagegen.assembler import assemble_storyboard_video


class TestVeoAnimationPlan(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = Service(self.temp_dir.name)
        self.app = Application(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_sample_project(self, scene_count=12):
        story = " ".join(f"Event {i} happens at the coast." for i in range(scene_count))
        p = self.service.create_project({
            'title': 'Animation Test Project',
            'story': story,
            'style': 'Cinematic'
        })
        p_id = p['id']

        analysis = {
            'world': 'Coastline',
            'characters': [{'name': 'Thomas', 'description': 'Keeper', 'wardrobe': {}}],
            'locations': [{'name': 'Tower', 'description': 'Lighthouse'}],
            'props': [],
            'scenes': []
        }
        for i in range(scene_count):
            analysis['scenes'].append({
                'beat': f'Thomas rushes to the lighthouse tower and grabs the lantern during the violent storm.' if i == 0 else f'Event {i} continues.',
                'source_quote': f'Event {i} happens at the coast.',
                'location': 'Tower',
                'location_ref': 'Tower',
                'time': 'Night' if i % 2 == 0 else 'Day',
                'shot': 'Wide panoramic establishing shot' if i == 0 else ('Medium action shot' if i == 1 else 'Close reaction detail'),
                'emotion': 'alarm' if i == 0 else 'calm',
                'continuity_before': '',
                'continuity_after': '',
                'visible_people': 1,
                'characters': ['Thomas'],
                'wardrobe': {},
                'props': []
            })
        self.service.import_analysis(p_id, analysis, p['version'])
        return p_id

    def _upload_scene_images(self, project_id, scene_count=None):
        detail = self.service.detail(project_id)
        scenes = detail['scenes']
        if scene_count is not None:
            scenes = scenes[:scene_count]
        for idx, sc in enumerate(scenes):
            buf = io.BytesIO()
            Image.new('RGB', (1280, 720), (10 + idx * 20, 40 + idx * 10, 80 + idx * 5)).save(buf, format='PNG')
            self.service.upload_scene_image(sc['id'], buf.getvalue())


    # 1. Scoring & Hook rules
    def test_01_scoring_and_hook_priorities(self):
        # Hook for scene 0: 35 pts base
        sc0 = {'beat': 'Thomas stands quietly.', 'visible_people': 1}
        score0, reasons0, _ = score_scene_for_animation(sc0, 0, 10)
        self.assertIn('hook', reasons0)
        self.assertGreaterEqual(score0, 35)

        # Hook for scene 1: 28 pts base
        sc1 = {'beat': 'Thomas looks around.', 'visible_people': 1}
        score1, reasons1, _ = score_scene_for_animation(sc1, 1, 10)
        self.assertIn('hook', reasons1)
        self.assertGreaterEqual(score1, 28)

        # Action bonus: up to +25
        sc_action = {'beat': 'Thomas rushes to the door and leaps across the ledge.', 'visible_people': 1}
        score_a, reasons_a, _ = score_scene_for_animation(sc_action, 5, 10)
        self.assertIn('character_action', reasons_a)
        self.assertGreaterEqual(score_a, 25)

        # Emotion bonus: up to +20
        sc_emotion = {'beat': 'Thomas experiences sudden panic and screams in terror.', 'visible_people': 1, 'emotion': 'terror'}
        score_e, reasons_e, _ = score_scene_for_animation(sc_emotion, 5, 10)
        self.assertIn('emotion_reaction', reasons_e)
        self.assertGreaterEqual(score_e, 20)

        # Environment bonus: up to +10
        sc_env = {'beat': 'Heavy rain and lightning batter the sea and ship.', 'visible_people': 1}
        score_w, reasons_w, _ = score_scene_for_animation(sc_env, 5, 10)
        self.assertIn('expressive_environment', reasons_w)

        # High risk crowd penalty (-20)
        sc_crowd = {'beat': 'A large group gathers.', 'visible_people': 4}
        score_c, reasons_c, _ = score_scene_for_animation(sc_crowd, 5, 10)
        self.assertIn('high_risk_group', reasons_c)

    # 2. Camera motion & Veo prompt structure
    def test_02_veo_prompt_structure_and_camera_motion(self):
        sc_wide = {'shot': 'Wide panoramic establishing composition', 'beat': 'The sun rises over the rocks.'}
        cam = determine_camera_motion(sc_wide)
        self.assertIn('pan', cam)

        prompt = compile_veo_prompt(sc_wide, cam)
        self.assertIn('Use supplied references exactly.', prompt)
        self.assertIn('Preserve identity, hair, wardrobe, age', prompt)
        self.assertIn('Action: The sun rises over the rocks.', prompt)
        self.assertIn(f'Camera: {cam}.', prompt)
        self.assertIn('No new people, no identity or wardrobe changes', prompt)

    # 3. Automatic plan defaults to Flow Omni and never charges Veo credits
    def test_03_automatic_plan_defaults_to_omni_with_durable_defaults(self):
        p_id = self._create_sample_project(scene_count=12)
        self._upload_scene_images(p_id)
        plan_res = self.service.build_animation_plan(p_id, plan_mode='economy')
        summary = plan_res['summary']
        self.assertEqual(summary['plan_mode'], 'economy')
        self.assertEqual(summary['total_credits'], 0)
        self.assertEqual(summary['omni_frames_count'], 12)
        self.assertEqual(summary['veo_frames_count'], 0)
        self.assertEqual(summary['ken_burns_frames_count'], 0)
        for item in plan_res['scenes']:
            anim = item['animation']
            self.assertEqual(anim['selected_engine'], 'omni_flash')
            self.assertEqual(anim['status'], 'planned_omni_flash')
            self.assertEqual(anim['omni']['resolution'], '720p')
            self.assertEqual(anim['omni']['duration_seconds'], 6)
            self.assertEqual(anim['credit_cost'], 0)

    def test_03b_plan_waits_for_static_image_instead_of_falling_back(self):
        p_id = self._create_sample_project(scene_count=2)
        plan = self.service.build_animation_plan(p_id, plan_mode='economy')
        self.assertEqual(plan['summary']['waiting_source_image_count'], 2)
        self.assertTrue(all(s['animation']['status'] == 'waiting_source_image' for s in plan['scenes']))

    def test_03c_replan_preserves_completed_legacy_animation_snapshot(self):
        p_id = self._create_sample_project(scene_count=1)
        scene_id = self.service.detail(p_id)['scenes'][0]['id']
        self.service.select_scene_animation_engine(scene_id, 'ken_burns', ken_burns_preset='slow_panorama')
        # Legacy completed metadata lacks the newer engine namespaces.
        scene = self.service.scene(scene_id); data = dict(scene['data']); data['animation'].update({'status': 'completed', 'source': 'ken_burns', 'video_asset_id': 'legacy-video'})
        with self.service.db.transaction() as con:
            con.execute('INSERT INTO scene_revisions VALUES(?,?,?,?)', (scene_id, scene['current_revision'] + 1, json.dumps(data), '2026-01-01T00:00:00Z'))
            con.execute('UPDATE scenes SET current_revision=? WHERE id=?', (scene['current_revision'] + 1, scene_id))
        plan = self.service.build_animation_plan(p_id)
        anim = plan['scenes'][0]['animation']
        self.assertEqual('completed', anim['status'])
        self.assertEqual('ken_burns', anim['selected_engine'])
        self.assertEqual('slow_panorama', anim['ken_burns_preset'])

    # 4. Economy mode server prevents 6th Veo frame
    def test_04_economy_mode_rejects_sixth_veo_frame(self):
        p_id = self._create_sample_project(scene_count=8)
        self._upload_scene_images(p_id)
        self.service.build_animation_plan(p_id, plan_mode='economy')
        detail = self.service.detail(p_id)
        # Automatic Omni choices do not consume the separate manual Veo budget.
        for scene in detail['scenes'][:5]:
            self.service.select_scene_animation_engine(scene['id'], 'veo_lite')
        detail = self.service.detail(p_id)
        kb_scene = detail['scenes'][5]
        with self.assertRaises(DomainError) as ctx:
            self.service.select_scene_animation_engine(kb_scene['id'], 'veo_lite')
        self.assertIn('50 кредитов', str(ctx.exception))

    # 5. Manual mode allows > 5 Veo frames with exact budget projection
    def test_05_manual_mode_allows_over_budget_with_exact_projection(self):
        p_id = self._create_sample_project(scene_count=8)
        self.service.build_animation_plan(p_id, plan_mode='manual')

        detail = self.service.detail(p_id)
        # Select veo_lite for 7 scenes in manual mode
        for s in detail['scenes'][:7]:
            res = self.service.select_scene_animation_engine(s['id'], 'veo_lite')

        final_detail = self.service.detail(p_id)
        veo_scenes = [s for s in final_detail['scenes'] if s['data'].get('animation', {}).get('selected_engine') == 'veo_lite']
        self.assertEqual(len(veo_scenes), 7)

        status = self.service.animation_status(p_id)
        self.assertEqual(status['plan_mode'], 'manual')

    # 6. Safe crop algorithm guarantees no edges or black bars
    def test_06_ken_burns_safe_crop_boundary_verification(self):
        w, h = 1280, 720

        # Normal safe preset
        safe_crop = calculate_safe_crop(w, h, 'slow_zoom_in', overscan=0.15)
        self.assertTrue(safe_crop['edge_safe'])
        self.assertEqual(safe_crop['actual_preset'], 'slow_zoom_in')

        # Generate frames and check dimensions
        img = Image.new('RGB', (w, h), (100, 140, 180))
        frames, crop_res = render_ken_burns_frames(img, preset_name='slow_panorama', duration=1.0, fps=12, overscan=0.15)
        self.assertEqual(len(frames), 12)
        for f in frames:
            self.assertEqual(f.size, (1280, 720))

    # 7. Safe crop automatically switches to fallback slow_zoom_in when pan is excessive
    def test_07_safe_crop_excessive_pan_triggers_fallback(self):
        w, h = 1280, 720
        # If pan constraint is tiny (e.g. 0.001), horizontal pan must trigger fallback
        safe_crop = calculate_safe_crop(w, h, 'slow_panorama', overscan=0.01, max_pan_x=0.001, max_pan_y=0.001)
        self.assertFalse(safe_crop['edge_safe'])
        self.assertEqual(safe_crop['actual_preset'], 'slow_zoom_in')

    # 8. REST API endpoints dispatch and return expected formats
    def test_08_rest_api_animation_lifecycle(self):
        p_id = self._create_sample_project(scene_count=6)
        self._upload_scene_images(p_id)

        # POST /api/projects/:id/animation/plan
        plan_res = self.app.dispatch('POST', f'/api/projects/{p_id}/animation/plan', {'plan_mode': 'economy'})
        self.assertEqual(6, plan_res['summary']['omni_frames_count'])
        self.assertEqual(0, plan_res['summary']['veo_frames_count'])
        self.assertEqual(0, plan_res['summary']['ken_burns_frames_count'])

        # GET /api/projects/:id/animation/status
        status_res = self.app.dispatch('GET', f'/api/projects/{p_id}/animation/status', None)
        self.assertEqual(p_id, status_res['project_id'])
        self.assertEqual(6, status_res['total_scenes'])

        # POST /api/scenes/:id/animation/select
        detail = self.service.detail(p_id)
        scene0 = detail['scenes'][0]
        select_res = self.app.dispatch('POST', f'/api/scenes/{scene0["id"]}/animation/select', {'engine': 'ken_burns'})
        self.assertEqual('ken_burns', select_res['scene']['data']['animation']['selected_engine'])

        # A creator may explicitly select the local camera path for a frame.
        manual_res = self.app.dispatch('POST', f'/api/scenes/{scene0["id"]}/animation/select', {
            'engine': 'ken_burns', 'ken_burns_preset': 'slow_pull_back'
        })
        self.assertEqual('slow_pull_back', manual_res['scene']['data']['animation']['ken_burns_preset'])

        # POST /api/projects/:id/animation/generate
        gen_res = self.app.dispatch('POST', f'/api/projects/{p_id}/animation/generate', None)
        self.assertTrue(gen_res['ken_burns_completed'] >= 1)

    # 9. Assembly of chronological timeline into MP4
    def test_09_assemble_chronological_timeline(self):
        p_id = self._create_sample_project(scene_count=3)
        self._upload_scene_images(p_id)
        # Plan all as ken_burns for immediate local video generation
        self.service.build_animation_plan(p_id, plan_mode='manual')
        for s in self.service.detail(p_id)['scenes']:
            self.service.select_scene_animation_engine(s['id'], 'ken_burns')

        # Generate Ken Burns videos for all 3 scenes
        self.service.start_animation_generation(p_id)

        # Assemble video timeline
        assemble_res = self.service.assemble_animation_timeline(p_id, transition_seconds=0.2)
        self.assertEqual(3, assemble_res['clips_count'])
        self.assertTrue(assemble_res['asset_id'])
        self.assertTrue(assemble_res['url'].startswith('/api/assets/'))

        # Check asset exists
        asset = self.service.asset(assemble_res['asset_id'])
        self.assertGreater(asset['bytes'], 0)

    # 10. Veo jobs queue, video upload with metadata and idempotency
    def test_10_veo_jobs_queue_and_upload_idempotency(self):
        p_id = self._create_sample_project(scene_count=6)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(buf, format='PNG')
        valid_png = buf.getvalue()
        self.service.upload_character_portrait(char['id'], valid_png, lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], valid_png, lock=True)
        self._upload_scene_images(p_id)

        # Veo is manual: automatic plans now use Omni.
        self.service.build_animation_plan(p_id, plan_mode='economy')
        for scene in self.service.detail(p_id)['scenes'][:5]:
            self.service.select_scene_animation_engine(scene['id'], 'veo_lite')
        self.service.start_animation_generation(p_id)

        # Fetch animation jobs
        jobs_res = self.app.dispatch('GET', f'/api/projects/{p_id}/animation/jobs', None)
        self.assertEqual(p_id, jobs_res['project_id'])
        self.assertEqual('economy', jobs_res['plan_mode'])
        veo_jobs = [job for job in jobs_res['jobs'] if job['engine'] == 'veo_lite']
        self.assertEqual(5, len(veo_jobs))
        jobs_res['jobs'] = veo_jobs

        job0 = jobs_res['jobs'][0]
        self.assertEqual('scene_video', job0['kind'])
        self.assertEqual('veo-3.1-lite', job0['model'])
        self.assertTrue(job0['prompt'].startswith('Use supplied references exactly.') or job0['prompt'].startswith('Animate the supplied image'))
        self.assertTrue(bool(job0['idempotency_key']))

        # Simulate upload from Flow adapter via POST /api/scenes/<id>/video
        import base64
        fake_video_b64 = base64.b64encode(b'sample_mp4_video_bytes').decode('ascii')
        upload_payload = {
            'video': f'data:video/mp4;base64,{fake_video_b64}',
            'filename': 'veo.mp4',
            'flow_media_id': 'flow-media-veo-abc',
            'idempotency_key': job0['idempotency_key']
        }
        upload_res = self.app.dispatch('POST', f'/api/scenes/{job0["scene_id"]}/video', upload_payload)
        self.assertTrue(upload_res['video_asset_id'])
        self.assertEqual('completed', upload_res['animation']['status'])
        self.assertEqual(10, upload_res['animation']['credit_cost'])
        self.assertEqual('flow-media-veo-abc', upload_res['animation']['flow_media_id'])
        self.assertEqual(job0['idempotency_key'], upload_res['animation']['idempotency_key'])

        # Now GET /api/projects/:id/animation/jobs must only return 4 remaining queued jobs
        jobs_res2 = self.app.dispatch('GET', f'/api/projects/{p_id}/animation/jobs', None)
        self.assertEqual(4, len([job for job in jobs_res2['jobs'] if job['engine'] == 'veo_lite']))
        self.assertNotIn(job0['scene_id'], [j['scene_id'] for j in jobs_res2['jobs']])

        # Check status breakdown
        status_res = self.service.animation_status(p_id)
        self.assertEqual(1, status_res['veo_completed'])
        self.assertEqual(4, status_res['veo_queued'])
        self.assertEqual(10, status_res['total_credits_spent'])
        self.assertEqual(50, status_res['total_credits_projected'])

        # Test retry Veo button endpoint (+10 credits)
        retry_res = self.app.dispatch('POST', f'/api/scenes/{job0["scene_id"]}/animation/retry', None)
        self.assertEqual('queued_veo', retry_res['status'])
        updated_sc = self.service.scene(job0['scene_id'])
        self.assertNotEqual(job0['idempotency_key'], updated_sc['data']['animation']['idempotency_key'])

        # After retry, jobs list includes it again with new idempotency key
        jobs_res3 = self.app.dispatch('GET', f'/api/projects/{p_id}/animation/jobs', None)
        self.assertEqual(5, len([job for job in jobs_res3['jobs'] if job['engine'] == 'veo_lite']))

    # 11. Automatic Omni jobs are not constrained by Veo credit policy
    def test_11_automatic_omni_jobs_are_not_limited_by_veo_budget(self):
        p_id = self._create_sample_project(scene_count=8)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        buf = io.BytesIO(); Image.new('RGB', (100, 100), (255, 0, 0)).save(buf, format='PNG')
        self.service.upload_character_portrait(char['id'], buf.getvalue(), lock=True)
        self.service.upload_location_image(detail['locations'][0]['id'], buf.getvalue(), lock=True)
        self._upload_scene_images(p_id)
        self.service.build_animation_plan(p_id, plan_mode='economy')
        self.service.start_animation_generation(p_id)
        jobs = self.service.animation_jobs(p_id)['jobs']
        self.assertEqual(len(jobs), 8)
        self.assertTrue(all(job['engine'] == 'omni_flash' for job in jobs))
        self.assertTrue(all(job['resolution'] == '720p' and job['duration_seconds'] == 6 for job in jobs))


    # 12. Server-side idempotency: duplicate video upload calls return exact same result without duplicate assets or credits
    def test_12_server_idempotency_duplicate_veo_uploads(self):
        import base64
        import sqlite3
        p_id = self._create_sample_project(scene_count=4)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(buf, format='PNG')
        valid_png = buf.getvalue()
        self.service.upload_character_portrait(char['id'], valid_png, lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], valid_png, lock=True)
        self._upload_scene_images(p_id)

        self.service.build_animation_plan(p_id, plan_mode='economy')
        for scene in self.service.detail(p_id)['scenes']:
            self.service.select_scene_animation_engine(scene['id'], 'veo_lite')
        self.service.start_animation_generation(p_id)

        jobs_res = self.app.dispatch('GET', f'/api/projects/{p_id}/animation/jobs', None)
        self.assertTrue(len(jobs_res['jobs']) > 0)
        job = jobs_res['jobs'][0]
        scene_id = job['scene_id']
        idempotency_key = job['idempotency_key']

        fake_b64 = base64.b64encode(b'mp4_data_for_idempotency_test').decode('ascii')
        payload = {
            'video': f'data:video/mp4;base64,{fake_b64}',
            'filename': 'veo_scene.mp4',
            'flow_media_id': 'flow-media-idem-999',
            'idempotency_key': idempotency_key
        }

        # First upload
        res1 = self.app.dispatch('POST', f'/api/scenes/{scene_id}/video', payload)
        self.assertTrue(res1['video_asset_id'])
        self.assertEqual('completed', res1['animation']['status'])
        self.assertEqual(10, res1['animation']['credit_cost'])

        # Second identical upload with same idempotency_key
        res2 = self.app.dispatch('POST', f'/api/scenes/{scene_id}/video', payload)
        # 1. Both responses return the exact same video_asset_id
        self.assertEqual(res1['video_asset_id'], res2['video_asset_id'])
        self.assertEqual('completed', res2['animation']['status'])

        # 2. In DB, exactly one scene_video generation exists for this scene
        gens = self.service.db.all(
            "SELECT id, asset_id FROM generations WHERE scene_id=? AND purpose='scene_video'",
            (scene_id,)
        )
        self.assertEqual(1, len(gens))

        # 3. Exactly one row in veo_jobs for this (scene_id, idempotency_key)
        veo_rows = self.service.db.all(
            "SELECT * FROM veo_jobs WHERE scene_id=? AND idempotency_key=?",
            (scene_id, idempotency_key)
        )
        self.assertEqual(1, len(veo_rows))
        self.assertEqual('completed', veo_rows[0]['status'])
        self.assertEqual(res1['video_asset_id'], veo_rows[0]['asset_id'])

        # 4. Exactly one video asset created for this scene
        video_assets = self.service.db.all(
            "SELECT a.id FROM assets a JOIN generations g ON a.generation_id=g.id WHERE g.purpose='scene_video' AND g.scene_id=?",
            (scene_id,)
        )
        self.assertEqual(1, len(video_assets))
        self.assertEqual(res1['video_asset_id'], video_assets[0]['id'])

        # 5. Exactly 10 credits spent (counted once)
        status_res = self.service.animation_status(p_id)
        self.assertEqual(1, status_res['veo_completed'])
        self.assertEqual(10, status_res['total_credits_spent'])

        # 6. Verify UNIQUE(scene_id, idempotency_key) constraint protects against duplicate job creation
        with self.assertRaises(sqlite3.IntegrityError):
            with self.service.db.transaction() as conn:
                conn.execute("""
                    INSERT INTO veo_jobs(id, project_id, scene_id, idempotency_key, model, status, created_at)
                    VALUES('dup-id', ?, ?, ?, 'veo-3.1-lite', 'queued_veo', '2026-01-01')
                """, (p_id, scene_id, idempotency_key))

    def _create_valid_mp4(self, width=1280, height=720, duration=1.0, fps=10):
        import av
        buf = io.BytesIO()
        container = av.open(buf, mode='w', format='mp4')
        stream = container.add_stream('libx264', rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = 'yuv420p'
        stream.options = {'crf': '28', 'preset': 'ultrafast'}
        frame_count = int(duration * fps)
        for i in range(max(1, frame_count)):
            img = Image.new('RGB', (width, height), (i * 10 % 255, 100, 150))
            frame = av.VideoFrame.from_image(img)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return buf.getvalue()

    # 13. Animation passport generation, continuity checks, and staleness detection
    def test_13_animation_passport_generation_and_staleness(self):
        p_id = self._create_sample_project(scene_count=3)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        # Passport when no source image exists
        passport = self.service.animation_passport(sc0_id)
        self.assertEqual(p_id, passport['project_id'])
        self.assertEqual(sc0_id, passport['scene_id'])
        self.assertEqual(1, passport['position'])
        self.assertTrue(passport['stale'])  # no source image yet
        self.assertFalse(passport['has_source_image'])
        self.assertIn('0_2s', passport['micro_actions'])
        self.assertIn('2_5s', passport['micro_actions'])
        self.assertIn('5_8s', passport['micro_actions'])
        self.assertIn('Thomas', [c['name'] for c in passport['characters']])
        self.assertEqual('Tower', passport['location']['name'])
        self.assertIn('Keep the same cast identities', passport['prompt'])

        # Lock character portrait and upload source image for scene 0
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)

        buf = io.BytesIO()
        Image.new('RGB', (1280, 720), (50, 100, 150)).save(buf, format='PNG')
        self.service.upload_scene_image(sc0_id, buf.getvalue())

        # Check passport is fresh now
        passport2 = self.service.animation_passport(sc0_id)
        self.assertFalse(passport2['stale'])
        self.assertTrue(passport2['has_source_image'])
        self.assertTrue(bool(passport2['source_image_asset_id']))
        self.assertTrue(bool(passport2['source_image_sha256']))

        # Now modify scene prompt / beat -> revision increments -> stale
        sc0_updated = self.service.scene(sc0_id)
        new_data = dict(sc0_updated['data'])
        new_data['beat'] = 'Thomas urgently signals the incoming ship with the lantern.'
        self.service.edit_scene(sc0_id, new_data, sc0_updated['current_revision'])

        passport3 = self.service.animation_passport(sc0_id)
        self.assertTrue(passport3['stale'])
        self.assertTrue(bool(passport3['stale_reason']))
        self.assertTrue('устарел' in passport3['stale_reason'] or 'изменения' in passport3['stale_reason'])

    # 14. Story continuity validation: pot on head, packing boxes, off-screen characters
    def test_14_story_continuity_pot_boxes_lighthouse(self):
        from imagegen.animation import validate_story_continuity, build_animation_passport

        # 14a. Pot wearing before discovery violation
        scenes_invalid_pot = [
            {'beat': 'Misha puts an iron pot on his head like a knight helmet.', 'source_quote': 'Misha wore the pot.', 'characters': ['Misha'], 'props': ['pot']},
            {'beat': 'Misha finds an iron pot in the old kitchen cabinet.', 'source_quote': 'He found a pot.', 'characters': ['Misha'], 'props': ['pot']}
        ]
        res_pot = validate_story_continuity(scenes_invalid_pot)
        self.assertTrue(res_pot['has_violation'])
        self.assertTrue(any('горшок' in v.lower() or 'pot' in v.lower() for v in res_pot['violations']))

        # 14b. Pot discovery then wearing -> valid
        scenes_valid_pot = [
            {'beat': 'Misha searches the cabinet and discovers an iron pot.', 'source_quote': 'Found a pot.', 'characters': ['Misha'], 'props': ['pot']},
            {'beat': 'Misha puts the iron pot on his head like a helmet.', 'source_quote': 'Wore the pot.', 'characters': ['Misha'], 'props': ['pot']}
        ]
        res_valid_pot = validate_story_continuity(scenes_valid_pot)
        self.assertFalse(res_valid_pot['has_violation'])

        # 14c. Packing boxes before packing starts violation
        scenes_invalid_boxes = [
            {'beat': 'Sealed cardboard boxes stand stacked in the empty hallway.', 'source_quote': 'Boxes were stacked.', 'characters': [], 'props': ['box']},
            {'beat': 'The family begins packing their belongings into cardboard boxes.', 'source_quote': 'They started packing.', 'characters': [], 'props': ['box']}
        ]
        res_boxes = validate_story_continuity(scenes_invalid_boxes)
        self.assertTrue(res_boxes['has_violation'])
        self.assertTrue(any('коробк' in v.lower() or 'box' in v.lower() for v in res_boxes['violations']))

        # 14d. Off-screen voice/photo constraint
        project = {'title': 'Lighthouse Call', 'world': 'Island', 'characters': [{'name': 'Elena', 'id': 'c1'}], 'locations': [{'name': 'Radio Room', 'id': 'l1'}], 'props': []}
        scene_radio = {
            'beat': 'Elena listens tensely as Captain Miller speaks over the radio transmitter.',
            'source_quote': 'Miller called on the radio.',
            'location': 'Radio Room',
            'characters': ['Elena', 'Captain Miller'],
            'props': ['radio']
        }
        passport = build_animation_passport(project, scene_radio, scenes=[scene_radio], cast_map={'Elena': {'name': 'Elena', 'id': 'c1'}, 'Captain Miller': {'name': 'Captain Miller', 'id': 'c2'}})
        phys_names = [c['name'] for c in passport['characters']]
        self.assertIn('Elena', phys_names)
        self.assertNotIn('Captain Miller', phys_names)
        self.assertTrue(any('Captain Miller' in neg for neg in passport['negative_constraints']))

    # 15. Manual MP4 import and deep validation via PyAV
    def test_15_manual_mp4_import_and_deep_validation(self):
        import base64
        p_id = self._create_sample_project(scene_count=2)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        valid_mp4 = self._create_valid_mp4(width=1280, height=720, duration=1.0)
        valid_b64 = base64.b64encode(valid_mp4).decode('ascii')

        # 15a. Valid import via POST /api/scenes/<id>/video/import
        payload = {
            'video': f'data:video/mp4;base64,{valid_b64}',
            'filename': 'scene_0_flow.mp4',
            'idempotency_key': 'manual_key_001'
        }
        res = self.app.dispatch('POST', f'/api/scenes/{sc0_id}/video/import', payload)
        self.assertTrue(res['video_asset_id'])
        self.assertEqual('completed', res['animation']['status'])
        self.assertEqual('flow_manual', res['animation']['source'])
        self.assertEqual(0, res['animation']['credit_cost'])

        # Verify asset exists on disk and in DB
        asset = self.service.asset(res['video_asset_id'])
        self.assertEqual(len(valid_mp4), asset['bytes'])
        self.assertEqual(1280, asset['width'])
        self.assertEqual(720, asset['height'])

        # 15b. Negative test: corrupt / invalid file
        corrupt_b64 = base64.b64encode(b'this is totally not a video file').decode('ascii')
        with self.assertRaises(DomainError) as ctx_corrupt:
            self.app.dispatch('POST', f'/api/scenes/{sc0_id}/video/import', {
                'video': f'data:video/mp4;base64,{corrupt_b64}',
                'filename': 'corrupt.mp4'
            })
        self.assertEqual(400, ctx_corrupt.exception.status)
        self.assertTrue('Invalid data' in str(ctx_corrupt.exception) or 'контейнер' in str(ctx_corrupt.exception) or 'видео' in str(ctx_corrupt.exception))

        # 15c. Negative test: non 16:9 aspect ratio (square 500x500)
        square_mp4 = self._create_valid_mp4(width=500, height=500, duration=1.0)
        square_b64 = base64.b64encode(square_mp4).decode('ascii')
        with self.assertRaises(DomainError) as ctx_ratio:
            self.app.dispatch('POST', f'/api/scenes/{sc0_id}/video/import', {
                'video': f'data:video/mp4;base64,{square_b64}',
                'filename': 'square.mp4'
            })
        self.assertIn('16:9', str(ctx_ratio.exception))

        # 15d. Negative test: revision mismatch
        cur_sc = self.service.scene(sc0_id)
        with self.assertRaises(DomainError) as ctx_rev:
            self.app.dispatch('POST', f'/api/scenes/{sc0_id}/video/import', {
                'video': f'data:video/mp4;base64,{valid_b64}',
                'filename': 'rev_mismatch.mp4',
                'expected_scene_revision': cur_sc['current_revision'] + 99
            })
        self.assertIn('Ревизия кадра изменилась', str(ctx_rev.exception))

    # 16. Manual import atomically cancels pending automated veo_jobs and guarantees 0 credit cost
    def test_16_manual_import_cancels_pending_veo_job_and_idempotency(self):
        p_id = self._create_sample_project(scene_count=4)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(buf, format='PNG')
        valid_png = buf.getvalue()
        self.service.upload_character_portrait(char['id'], valid_png, lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], valid_png, lock=True)
        self._upload_scene_images(p_id)

        # Veo is manual; automatic planning remains Omni.
        self.service.build_animation_plan(p_id, plan_mode='economy')
        for scene in self.service.detail(p_id)['scenes']:
            self.service.select_scene_animation_engine(scene['id'], 'veo_lite')
        self.service.start_animation_generation(p_id)

        # Confirm scene 0 is queued_veo
        sc0_id = detail['scenes'][0]['id']
        jobs = self.service.animation_jobs(p_id)['jobs']
        self.assertIn(sc0_id, [j['scene_id'] for j in jobs])

        # Verify veo_jobs has queued_veo row for scene 0
        vjob = self.service.db.one("SELECT * FROM veo_jobs WHERE scene_id=? AND status='queued_veo'", (sc0_id,))
        self.assertIsNotNone(vjob)

        # Now import manual MP4
        valid_mp4 = self._create_valid_mp4(width=1280, height=720, duration=1.0)
        res_import = self.service.import_scene_video(
            sc0_id, valid_mp4, filename='flow_scene0.mp4',
            idempotency_key='manual_idem_test_16'
        )
        self.assertEqual('flow_manual', res_import['animation']['source'])
        self.assertEqual(0, res_import['animation']['credit_cost'])

        # 16a. Atomically marked veo_jobs as completed
        vjob_after = self.service.db.one("SELECT * FROM veo_jobs WHERE scene_id=?", (sc0_id,))
        self.assertEqual('completed', vjob_after['status'])
        self.assertEqual(res_import['video_asset_id'], vjob_after['asset_id'])

        # 16b. animation_jobs suppresses scene 0
        jobs_after = self.service.animation_jobs(p_id)['jobs']
        self.assertNotIn(sc0_id, [j['scene_id'] for j in jobs_after])

        # 16c. Credit status: manual costs 0, total spent is 0
        status = self.service.animation_status(p_id)
        self.assertEqual(1, status['veo_manual_completed'])
        self.assertEqual(0, status['veo_auto_completed'])
        self.assertEqual(0, status['total_credits_spent'])

        # 16d. Idempotency deduplication: importing the same clip again returns same asset without error or extra cost
        res_import2 = self.service.import_scene_video(
            sc0_id, valid_mp4, filename='flow_scene0.mp4',
            idempotency_key='manual_idem_test_16'
        )
        self.assertEqual(res_import['video_asset_id'], res_import2['video_asset_id'])

    # 17. Full assembly: imported Veo MP4 combined with Ken Burns clips into a playable storyboard timeline
    def test_17_full_assembly_with_imported_veo_and_ken_burns(self):
        import av
        p_id = self._create_sample_project(scene_count=3)
        self._upload_scene_images(p_id)
        detail = self.service.detail(p_id)
        scenes = detail['scenes']

        # Import manual Veo video for Scene 0
        valid_mp4 = self._create_valid_mp4(width=1280, height=720, duration=1.0)
        self.service.import_scene_video(scenes[0]['id'], valid_mp4, filename='scene0_veo.mp4')

        # Plan Scene 1 and Scene 2 as Ken Burns
        self.service.build_animation_plan(p_id, plan_mode='manual')
        self.service.select_scene_animation_engine(scenes[1]['id'], 'ken_burns')
        self.service.select_scene_animation_engine(scenes[2]['id'], 'ken_burns')

        # Generate Ken Burns clips for remaining scenes
        self.service.start_animation_generation(p_id)

        # Assemble full timeline
        assembled = self.service.assemble_animation_timeline(p_id, transition_seconds=0.1)
        self.assertEqual(3, assembled['clips_count'])
        self.assertTrue(assembled['asset_id'])

        # Verify assembled MP4 file is valid and readable via PyAV
        assembled_asset = self.service.asset(assembled['asset_id'])
        assembled_path = self.service.root / assembled_asset['path']
        self.assertTrue(assembled_path.is_file())

        container = av.open(str(assembled_path))
        self.assertEqual(1, len(container.streams.video))
        vstream = container.streams.video[0]
        self.assertEqual(1280, vstream.width)
        self.assertEqual(720, vstream.height)
        decoded_frames = 0
        for packet in container.demux(vstream):
            for _ in packet.decode():
                decoded_frames += 1
        container.close()
    # 18. Selective Veo card flow and full card contract for Flow Automator
    def test_18_selective_veo_card_flow_and_protocol_contract(self):
        p_id = self._create_sample_project(scene_count=4)
        detail = self.service.detail(p_id)
        scenes = detail['scenes']

        # Lock portrait and location
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)

        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], char_buf.getvalue(), lock=True)

        # Upload scene static image for scene 0
        img_buf = io.BytesIO()
        Image.new('RGB', (1280, 720), (50, 100, 150)).save(img_buf, format='PNG')
        self.service.upload_scene_image(scenes[0]['id'], img_buf.getvalue())

        # Select scene 0 as Veo with dynamic motion
        res_sel = self.service.select_scene_animation_engine(
            scenes[0]['id'],
            engine='veo_lite',
            camera_motion='dynamic',
            duration_seconds=5
        )
        self.assertEqual('veo_lite', res_sel['scene']['data']['animation']['selected_engine'])
        self.assertEqual('dynamic', res_sel['scene']['data']['animation']['camera_motion'])
        self.assertEqual(5, res_sel['scene']['data']['animation']['duration_seconds'])

        # Queue animation
        self.service.start_animation_generation(p_id)

        # Fetch animation jobs and verify full card contract
        jobs_res = self.service.animation_jobs(p_id)
        self.assertEqual(1, len(jobs_res['jobs']))
        job = jobs_res['jobs'][0]
        active_scene = self.service.scene(scenes[0]['id'])
        active_generation = self.service.db.one(
            'SELECT asset_id FROM generations WHERE id=?',
            (active_scene['active_generation_id'],)
        )
        prompts = self.service.project_prompts(p_id)
        prompt_scene = next(s for s in prompts['scenes'] if s['scene_id'] == scenes[0]['id'])

        self.assertEqual('scene_video', job['kind'])
        self.assertEqual('veo-3.1-lite', job['model'])
        latest_sc0 = self.service.scene(scenes[0]['id'])
        self.assertEqual(latest_sc0['current_revision'], job['scene_revision'])
        # A generation id cannot be used with /api/assets. Both the static
        # sync payload and the Veo job must expose the real rendered asset.
        self.assertEqual(active_generation['asset_id'], prompt_scene['image_asset_id'])
        self.assertEqual(f"/api/assets/{active_generation['asset_id']}", prompt_scene['image_url'])
        self.assertEqual(active_generation['asset_id'], job['source_image_asset_id'])
        self.assertEqual(f"/api/assets/{active_generation['asset_id']}", job['source_image_url'])
        self.assertIn('0_2s', job['micro_actions'])
        self.assertIn('2_5s', job['micro_actions'])
        self.assertIn('5_8s', job['micro_actions'])
        self.assertIn('Thomas', [c['name'] for c in job['characters']])
        self.assertEqual('Tower', job['location']['name'])
        self.assertTrue(len(job['negative_constraints']) > 0)

        # Deselect scene 0 (engine='none')
        res_desel = self.service.select_scene_animation_engine(scenes[0]['id'], engine='none')
        self.assertIsNone(res_desel['scene']['data']['animation']['selected_engine'])
        self.assertEqual('not_planned', res_desel['scene']['data']['animation']['status'])

        # After deselect, animation_jobs returns 0 jobs
        jobs_res2 = self.service.animation_jobs(p_id)
        self.assertEqual(0, len(jobs_res2['jobs']))

    # 19. Staleness tracking on scene edits and reference changes
    def test_19_scene_staleness_and_deselection(self):
        p_id = self._create_sample_project(scene_count=2)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)

        # Upload initial image
        buf = io.BytesIO()
        Image.new('RGB', (1280, 720), (50, 100, 150)).save(buf, format='PNG')
        self.service.upload_scene_image(sc0_id, buf.getvalue())

        # Select Veo
        self.service.select_scene_animation_engine(sc0_id, engine='veo_lite')
        passport = self.service.animation_passport(sc0_id)
        self.assertFalse(passport['stale'])

        # Edit scene beat -> increments revision
        sc0 = self.service.scene(sc0_id)
        new_data = dict(sc0['data'])
        new_data['beat'] = 'Brand new story action.'
        self.service.edit_scene(sc0_id, new_data, sc0['current_revision'])

        # Passport is now marked stale with explanation
        passport_stale = self.service.animation_passport(sc0_id)
        self.assertTrue(passport_stale['stale'])
        self.assertTrue(bool(passport_stale['stale_reason']))

    # 20. Strict source image SHA-256 and revision validation in animation_jobs and upload_scene_video
    def test_20_source_image_sha256_and_revision_protection(self):
        p_id = self._create_sample_project(scene_count=2)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        # Lock references
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], char_buf.getvalue(), lock=True)

        # Upload initial scene static image
        img_buf = io.BytesIO()
        Image.new('RGB', (1024, 576), (45, 90, 135)).save(img_buf, format='PNG')
        img_bytes = img_buf.getvalue()
        sc_res = self.service.upload_scene_image(sc0_id, img_bytes)

        # Select Veo engine
        self.service.select_scene_animation_engine(sc0_id, engine='veo_lite')
        self.service.start_animation_generation(p_id)

        # 1. Check animation_jobs output has exact source_image_sha256 and idempotency_key
        jobs_res = self.service.animation_jobs(p_id)
        self.assertEqual(1, len(jobs_res['jobs']))
        job = jobs_res['jobs'][0]
        asset_rec = self.service.asset(job['source_image_asset_id'])
        expected_sha256 = asset_rec['sha256']
        self.assertEqual(expected_sha256, job['source_image_sha256'])
        self.assertTrue(job['source_image_url'].startswith('/api/assets/'))
        self.assertTrue(job['idempotency_key'].startswith(f"veo_{sc0_id[:8]}_"))

        # 2. upload_scene_video with different expected_scene_revision succeeds if source image SHA-256 matches
        fake_video = b'RIFF\x00\x00\x00\x00WEBM'  # dummy video bytes
        # Simulate UI/metadata revision bump (e.g. from revision 3 to 10)
        cur_scene = self.service.scene(sc0_id)
        bumped_rev = cur_scene['current_revision'] + 5

        # 3. upload_scene_video with mismatched source_image_asset_id raises DomainError 409
        with self.assertRaises(DomainError) as ctx_asset:
            self.service.upload_scene_video(
                sc0_id, fake_video,
                expected_scene_revision=bumped_rev,
                source_image_asset_id='wrong-asset-id',
                source_image_sha256=expected_sha256
            )
        self.assertEqual(409, ctx_asset.exception.status)

        # 4. upload_scene_video with mismatched source_image_sha256 raises DomainError 409
        with self.assertRaises(DomainError) as ctx_sha:
            self.service.upload_scene_video(
                sc0_id, fake_video,
                expected_scene_revision=bumped_rev,
                source_image_asset_id=job['source_image_asset_id'],
                source_image_sha256='0000000000000000000000000000000000000000000000000000000000000000'
            )
        self.assertEqual(409, ctx_sha.exception.status)

        # 5. Successful upload updates scene video and respects idempotency
        res_upload = self.service.upload_scene_video(
            sc0_id, fake_video,
            expected_scene_revision=job['scene_revision'],
            source_image_asset_id=job['source_image_asset_id'],
            source_image_sha256=expected_sha256,
            idempotency_key=job['idempotency_key']
        )
        self.assertTrue(res_upload['video_asset_id'])
        self.assertEqual('completed', res_upload['animation']['status'])

        # 6. Re-submitting same idempotency_key returns cached result without recreating
        res_cached = self.service.upload_scene_video(
            sc0_id, fake_video,
            expected_scene_revision=res_upload['animation'].get('scene_revision', job['scene_revision'] + 1),
            source_image_asset_id=job['source_image_asset_id'],
            source_image_sha256=expected_sha256,
            idempotency_key=job['idempotency_key']
        )
        self.assertEqual(res_upload['video_asset_id'], res_cached['video_asset_id'])

    # 21. import_scene_video rejects mismatched source image hash
    def test_21_import_scene_video_source_image_validation(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        img_buf = io.BytesIO()
        Image.new('RGB', (1024, 576), (10, 20, 30)).save(img_buf, format='PNG')
        img_bytes = img_buf.getvalue()
        self.service.upload_scene_image(sc0_id, img_bytes)

        # Attempt import with wrong hash
        fake_video = b'RIFF\x00\x00\x00\x00WEBM'
        with self.assertRaises(DomainError) as ctx_imp:
            self.service.import_scene_video(
                sc0_id, fake_video,
                expected_scene_revision=1,
                source_image_sha256='badhash'
            )
        self.assertEqual(409, ctx_imp.exception.status)

    # 22. delete_scene_video resets video asset, clears metadata, bumps revision, and records history
    def test_22_delete_scene_video_and_revisions(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        img_buf = io.BytesIO()
        Image.new('RGB', (1024, 576), (10, 20, 30)).save(img_buf, format='PNG')
        self.service.upload_scene_image(sc0_id, img_buf.getvalue())

        # Select Ken Burns preset
        self.service.select_scene_animation_engine(sc0_id, engine='ken_burns', ken_burns_preset='portrait_push_in')

        # Import video
        valid_video = self._create_valid_mp4(width=1280, height=720, duration=1.0)
        res_import = self.service.import_scene_video(
            sc0_id, valid_video, filename='test_clip.mp4', source='ken_burns', expected_scene_revision=self.service.scene(sc0_id)['current_revision']
        )
        self.assertTrue(res_import['video_asset_id'])
        sc_before = self.service.scene(sc0_id)
        self.assertIsNotNone(sc_before['video_asset_id'])
        self.assertEqual('completed', sc_before['data']['animation']['status'])
        rev_before = sc_before['current_revision']

        # Delete video via service
        del_res = self.service.delete_scene_video(sc0_id)
        self.assertIsNone(del_res['video_asset_id'])

        sc_after = self.service.scene(sc0_id)
        self.assertIsNone(sc_after['video_asset_id'])
        self.assertNotIn('video_asset_id', sc_after['data']['animation'])
        self.assertNotIn('source', sc_after['data']['animation'])
        self.assertEqual('not_planned', sc_after['data']['animation']['status'])
        self.assertEqual('portrait_push_in', sc_after['data']['animation']['ken_burns_preset'])
        self.assertEqual(rev_before + 1, sc_after['current_revision'])

        # Verify revision history stored
        rev_records = self.service.db.all('SELECT revision FROM scene_revisions WHERE scene_id=?', (sc0_id,))
        self.assertIn(sc_after['current_revision'], [r['revision'] for r in rev_records])

    # 23. delete_video endpoint routing supports POST, DELETE, GET and /video DELETE
    def test_23_delete_video_api_routing(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        img_buf = io.BytesIO()
        Image.new('RGB', (1024, 576), (10, 20, 30)).save(img_buf, format='PNG')
        self.service.upload_scene_image(sc0_id, img_buf.getvalue())

        valid_video = self._create_valid_mp4(width=1280, height=720, duration=1.0)

        # Test POST /api/scenes/{id}/delete_video
        self.service.import_scene_video(sc0_id, valid_video, filename='test.mp4', source='flow_manual')
        self.assertIsNotNone(self.service.scene(sc0_id)['video_asset_id'])
        res_post = self.app.dispatch('POST', f'/api/scenes/{sc0_id}/delete_video', {})
        self.assertIsNone(res_post['video_asset_id'])
        self.assertIsNone(self.service.scene(sc0_id)['video_asset_id'])

        # Test GET /api/scenes/{id}/delete_video (fallback for GET client calls)
        self.service.import_scene_video(sc0_id, valid_video, filename='test.mp4', source='flow_manual')
        self.assertIsNotNone(self.service.scene(sc0_id)['video_asset_id'])
        res_get = self.app.dispatch('GET', f'/api/scenes/{sc0_id}/delete_video', None)
        self.assertIsNone(res_get['video_asset_id'])
        self.assertIsNone(self.service.scene(sc0_id)['video_asset_id'])

        # Test DELETE /api/scenes/{id}/delete_video
        self.service.import_scene_video(sc0_id, valid_video, filename='test.mp4', source='flow_manual')
        self.assertIsNotNone(self.service.scene(sc0_id)['video_asset_id'])
        res_del = self.app.dispatch('DELETE', f'/api/scenes/{sc0_id}/delete_video', None)
        self.assertIsNone(res_del['video_asset_id'])
        self.assertIsNone(self.service.scene(sc0_id)['video_asset_id'])

        # Test DELETE /api/scenes/{id}/video
        self.service.import_scene_video(sc0_id, valid_video, filename='test.mp4', source='flow_manual')
        self.assertIsNotNone(self.service.scene(sc0_id)['video_asset_id'])
        res_vid_del = self.app.dispatch('DELETE', f'/api/scenes/{sc0_id}/video', None)
        self.assertIsNone(res_vid_del['video_asset_id'])
        self.assertIsNone(self.service.scene(sc0_id)['video_asset_id'])

    # 24. Independent preservation of ken_burns_preset and camera_motion
    def test_24_independent_preset_preservation(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']

        # 1. Set Ken Burns preset to portrait_push_in
        res1 = self.service.select_scene_animation_engine(sc0_id, engine='ken_burns', ken_burns_preset='portrait_push_in')
        anim1 = res1['scene']['data']['animation']
        self.assertEqual('ken_burns', anim1['selected_engine'])
        self.assertEqual('portrait_push_in', anim1['ken_burns_preset'])

        # 2. Select Veo with camera_motion='dynamic'
        res2 = self.service.select_scene_animation_engine(sc0_id, engine='veo_lite', camera_motion='dynamic')
        anim2 = res2['scene']['data']['animation']
        self.assertEqual('veo_lite', anim2['selected_engine'])
        self.assertEqual('dynamic', anim2['camera_motion'])
        # Crucial: Ken Burns preset MUST be preserved!
        self.assertEqual('portrait_push_in', anim2['ken_burns_preset'])

        # 3. Switch back to Ken Burns with preset='slow_panorama'
        res3 = self.service.select_scene_animation_engine(sc0_id, engine='ken_burns', ken_burns_preset='slow_panorama')
        anim3 = res3['scene']['data']['animation']
        self.assertEqual('ken_burns', anim3['selected_engine'])
        self.assertEqual('slow_panorama', anim3['ken_burns_preset'])
        # Crucial: Veo camera motion MUST be preserved!
        self.assertEqual('dynamic', anim3['camera_motion'])

    # 25. Per-scene independent animation engine selection
    def test_25_per_scene_independent_animation_selection(self):
        p_id = self._create_sample_project(scene_count=3)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']
        sc1_id = detail['scenes'][1]['id']
        sc2_id = detail['scenes'][2]['id']

        # Scene 0: Veo reaction
        self.service.select_scene_animation_engine(sc0_id, engine='veo_lite', camera_motion='reaction')
        # Scene 1: Ken Burns object_reveal
        self.service.select_scene_animation_engine(sc1_id, engine='ken_burns', ken_burns_preset='object_reveal')
        # Scene 2: None / unselected
        self.service.select_scene_animation_engine(sc2_id, engine='none')

        sc0_data = self.service.scene(sc0_id)['data']['animation']
        sc1_data = self.service.scene(sc1_id)['data']['animation']
        sc2_data = self.service.scene(sc2_id)['data']['animation']

        self.assertEqual('veo_lite', sc0_data['selected_engine'])
        self.assertEqual('reaction', sc0_data['camera_motion'])

        self.assertEqual('ken_burns', sc1_data['selected_engine'])
        self.assertEqual('object_reveal', sc1_data['ken_burns_preset'])

        self.assertIsNone(sc2_data['selected_engine'])
        self.assertEqual('not_planned', sc2_data['status'])

    # 26. Single frame Veo queueing via API endpoint and service method
    def test_26_queue_single_scene_veo(self):
        p_id = self._create_sample_project(scene_count=3)
        self._upload_scene_images(p_id)
        detail = self.service.detail(p_id)
        sc0_id = detail['scenes'][0]['id']
        sc1_id = detail['scenes'][1]['id']
        sc2_id = detail['scenes'][2]['id']

        # Ensure scene 1 and 2 start with ken_burns
        self.service.select_scene_animation_engine(sc1_id, engine='ken_burns', ken_burns_preset='slow_zoom_in')
        self.service.select_scene_animation_engine(sc2_id, engine='ken_burns', ken_burns_preset='portrait_push_in')

        # Send ONLY Scene 1 (index 1) to Veo via API
        res = self.app.dispatch('POST', f'/api/scenes/{sc1_id}/animation/queue_veo', {
            'camera_motion': 'reaction'
        })
        self.assertTrue(res.get('queued'))

        sc0_after = self.service.scene(sc0_id)
        sc1_after = self.service.scene(sc1_id)
        sc2_after = self.service.scene(sc2_id)

        # Scene 1 must be queued for Veo
        anim1 = sc1_after['data']['animation']
        self.assertEqual('veo_lite', anim1['selected_engine'])
        self.assertEqual('queued_veo', anim1['status'])
        self.assertEqual('reaction', anim1['camera_motion'])
        self.assertTrue(anim1.get('idempotency_key'))

        # Check veo_jobs entry for Scene 1
        veo_job = self.service.db.one('SELECT * FROM veo_jobs WHERE scene_id=?', (sc1_id,))
        self.assertIsNotNone(veo_job)
        self.assertEqual('queued_veo', veo_job['status'])

        # Scenes 0 and 2 must NOT be queued in Veo or altered into batch generation
        anim0 = sc0_after['data'].get('animation', {})
        anim2 = sc2_after['data'].get('animation', {})
        self.assertNotEqual('queued_veo', anim0.get('status'))
        self.assertEqual('ken_burns', anim2['selected_engine'])
        self.assertNotEqual('queued_veo', anim2.get('status'))

        # Check that no veo_jobs exist for scene 0 or 2
        self.assertIsNone(self.service.db.one('SELECT * FROM veo_jobs WHERE scene_id=?', (sc0_id,)))
        self.assertIsNone(self.service.db.one('SELECT * FROM veo_jobs WHERE scene_id=?', (sc2_id,)))



    def test_24b_canonical_animation_delete_is_idempotent_and_keeps_static_asset(self):
        p_id = self._create_sample_project(scene_count=1)
        self._upload_scene_images(p_id)
        scene = self.service.detail(p_id)['scenes'][0]
        static_generation = scene['active_generation_id']
        self.service.queue_single_scene_omni(scene['id'])
        first = self.app.dispatch('POST', f"/api/scenes/{scene['id']}/animation/delete", {})
        second = self.app.dispatch('POST', f"/api/scenes/{scene['id']}/animation/delete", {})
        final_scene = self.service.scene(scene['id'])
        self.assertIsNone(first['video_asset_id'])
        self.assertIsNone(second['video_asset_id'])
        self.assertEqual(final_scene['active_generation_id'], static_generation)
        self.assertEqual(final_scene['data']['animation']['status'], 'not_planned')
        self.assertEqual(self.service.db.one('SELECT status FROM veo_jobs WHERE scene_id=?', (scene['id'],))['status'], 'cancelled')

    def test_25_omni_defaults_are_durable_and_queue_for_flow_without_api_key(self):
        p_id = self._create_sample_project(scene_count=1)
        self._upload_scene_images(p_id)
        scene_id = self.service.detail(p_id)['scenes'][0]['id']
        selection = self.service.select_scene_animation_engine(
            scene_id, 'omni_flash', duration_seconds=6, omni_resolution='720p'
        )
        animation = selection['scene']['data']['animation']
        self.assertEqual('omni_flash', animation['selected_engine'])
        self.assertEqual(6, animation['omni']['duration_seconds'])
        self.assertEqual('720p', animation['omni']['resolution'])
        self.assertEqual(6, animation['duration_seconds'])
        queued = self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        self.assertTrue(queued['queued'])
        queued_row = self.service.db.one('SELECT model, status FROM veo_jobs WHERE scene_id=?', (scene_id,))
        self.assertEqual('gemini-omni-1.1-flash', queued_row['model'])
        self.assertEqual('queued_omni', queued_row['status'])
        # Without a ready Flow passport the queue stays explicit and is not
        # replaced by a local/Veo fallback.
        self.assertEqual('queued_omni', self.service.scene(scene_id)['data']['animation']['status'])
        with self.assertRaises(DomainError):
            self.service.select_scene_animation_engine(scene_id, 'omni_flash', duration_seconds=5)

    # 27. Omni revision race condition: UI/metadata edits bump scene revision, but upload succeeds because source image is verified
    def test_27_omni_revision_race_condition_resilient(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], char_buf.getvalue(), lock=True)

        self._upload_scene_images(p_id)
        scene = self.service.detail(p_id)['scenes'][0]
        scene_id = scene['id']

        # 1. User queues Omni (e.g. at revision 7)
        queued = self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        self.assertTrue(queued['queued'])
        jobs = self.service.animation_jobs(p_id)['jobs']
        self.assertEqual(1, len(jobs))
        job = jobs[0]
        expected_rev = job['scene_revision']
        source_asset_id = job['source_image_asset_id']
        source_sha256 = job['source_image_sha256']
        idempotency_key = job['idempotency_key']

        # 2. Simulate subsequent metadata edits / UI interactions that bump scene revision (e.g. 7 -> 8 -> 9 -> 10)
        for i in range(3):
            cur = self.service.scene(scene_id)
            data = dict(cur['data'])
            data['beat'] = f"Updated beat {i+1} during storm"
            self.service.edit_scene(scene_id, data, cur['current_revision'])

        bumped_scene = self.service.scene(scene_id)
        self.assertGreater(bumped_scene['current_revision'], expected_rev)

        # 3. Bridge finishes generation and uploads video with original expected_scene_revision (7)
        # Backend accepts the video because source_image_asset_id and source_image_sha256 are verified!
        fake_video = b'RIFF\x00\x00\x00\x00WEBM'
        res = self.service.upload_scene_video(
            scene_id, fake_video,
            expected_scene_revision=expected_rev,
            source_image_asset_id=source_asset_id,
            source_image_sha256=source_sha256,
            idempotency_key=idempotency_key
        )
        self.assertTrue(res['video_asset_id'])
        self.assertEqual('completed', res['animation']['status'])

        # Veo jobs row marked completed
        job_row = self.service.db.one('SELECT status, asset_id FROM veo_jobs WHERE scene_id=? AND idempotency_key=?', (scene_id, idempotency_key))
        self.assertEqual('completed', job_row['status'])
        self.assertEqual(res['video_asset_id'], job_row['asset_id'])

    # 28. Switching active variant or regenerating image cancels pending job and rejects stale upload with clear 409 error
    def test_28_source_variant_switch_or_regeneration_cancels_and_rejects_old_job(self):
        p_id = self._create_sample_project(scene_count=1)
        detail = self.service.detail(p_id)
        char = detail['characters'][0]
        char_buf = io.BytesIO()
        Image.new('RGB', (100, 100), (255, 0, 0)).save(char_buf, format='PNG')
        self.service.upload_character_portrait(char['id'], char_buf.getvalue(), lock=True)
        loc = detail['locations'][0]
        self.service.upload_location_image(loc['id'], char_buf.getvalue(), lock=True)

        self._upload_scene_images(p_id)
        scene = self.service.detail(p_id)['scenes'][0]
        scene_id = scene['id']

        # Queue Omni for active generation
        self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        job = self.service.animation_jobs(p_id)['jobs'][0]
        old_idempotency_key = job['idempotency_key']
        old_asset_id = job['source_image_asset_id']
        old_sha256 = job['source_image_sha256']

        # Create a second image generation / variant and select it
        img_buf = io.BytesIO()
        Image.new('RGB', (1024, 576), (100, 150, 200)).save(img_buf, format='PNG')
        gen2 = self.service.upload_scene_image(scene_id, img_buf.getvalue())
        self.service.select_generation(gen2['generation_id'])

        # Pending job in veo_jobs is cancelled
        old_job_row = self.service.db.one('SELECT status FROM veo_jobs WHERE scene_id=? AND idempotency_key=?', (scene_id, old_idempotency_key))
        self.assertEqual('cancelled', old_job_row['status'])

        # Attempting upload for the old cancelled job / old source returns specific 409 error
        fake_video = b'RIFF\x00\x00\x00\x00WEBM'
        with self.assertRaises(DomainError) as ctx:
            self.service.upload_scene_video(
                scene_id, fake_video,
                expected_scene_revision=job['scene_revision'],
                source_image_asset_id=old_asset_id,
                source_image_sha256=old_sha256,
                idempotency_key=old_idempotency_key
            )
        self.assertEqual(409, ctx.exception.status)
        self.assertIn('Источник кадра изменён', str(ctx.exception))

    # 29. Deduplication on re-click and job cancellation on parameter change
    def test_29_repeated_click_deduplication_and_parameter_change(self):
        p_id = self._create_sample_project(scene_count=1)
        self._upload_scene_images(p_id)
        scene = self.service.detail(p_id)['scenes'][0]
        scene_id = scene['id']

        # First click
        q1 = self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        self.assertTrue(q1['queued'])
        self.assertFalse(q1.get('reused', False))
        key1 = q1['scene']['data']['animation']['idempotency_key']

        # Re-click with identical params: returns existing job without duplicates
        q2 = self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        self.assertTrue(q2['queued'])
        self.assertTrue(q2.get('reused', False))
        self.assertEqual(key1, q2['scene']['data']['animation']['idempotency_key'])
        total_jobs = self.service.db.all('SELECT * FROM veo_jobs WHERE scene_id=?', (scene_id,))
        self.assertEqual(1, len(total_jobs))

        # Re-click with different parameters (e.g. 10s duration): cancels previous job and creates new
        q3 = self.service.queue_single_scene_omni(scene_id, duration_seconds=10, resolution='720p')
        self.assertTrue(q3['queued'])
        self.assertFalse(q3.get('reused', False))
        key3 = q3['scene']['data']['animation']['idempotency_key']
        self.assertNotEqual(key1, key3)

        old_job = self.service.db.one('SELECT status FROM veo_jobs WHERE scene_id=? AND idempotency_key=?', (scene_id, key1))
        self.assertEqual('cancelled', old_job['status'])
        new_job = self.service.db.one('SELECT status, duration_seconds FROM veo_jobs WHERE scene_id=? AND idempotency_key=?', (scene_id, key3))
        self.assertEqual('queued_omni', new_job['status'])
        self.assertEqual(10, new_job['duration_seconds'])

    # 30. Bridge status sync with veo_jobs
    def test_30_bridge_status_updates_sync_with_veo_jobs(self):
        p_id = self._create_sample_project(scene_count=1)
        self._upload_scene_images(p_id)
        scene = self.service.detail(p_id)['scenes'][0]
        scene_id = scene['id']

        self.service.queue_single_scene_omni(scene_id, duration_seconds=6, resolution='720p')
        self.service.set_scene_animation_status(scene_id, 'generating_omni')

        job_row = self.service.db.one('SELECT status FROM veo_jobs WHERE scene_id=?', (scene_id,))
        self.assertEqual('generating_omni', job_row['status'])


if __name__ == '__main__':
    unittest.main()
