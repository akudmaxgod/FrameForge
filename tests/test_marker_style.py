import unittest
from unittest.mock import patch
from imagegen.domain import compile_flow_prompt,compile_prompt
from imagegen.service import Service
from imagegen.generation_scene import compile_wire
from imagegen.visual_style import MARKER_PREFIX,is_marker_style

class MarkerStyleTests(unittest.TestCase):
    def project(self,mode='dynamic'):
        return dict(id='p',mode=mode,style=MARKER_PREFIX+', 2D hand-drawn educational illustration',story='Врач осматривает пациента.',world='Историческая клиника')
    def scene(self):
        return dict(beat='Врач сидит у стола.',source_quote='Врач сидит у стола.',characters=['Врач'],character_ids=['c'],visible_people=1,location='Кабинет',shot='medium shot')
    def character(self):
        return dict(id='c',name='Врач',description='Мужчина в очках',wardrobe={},locked=False,revision=1,references={})
    def test_flow_keeps_marker_language_without_photo_or_painterly_defaults(self):
        prompt=compile_flow_prompt(self.project(),self.scene(),[self.character()])
        self.assertIn('thick black outlines',prompt)
        self.assertIn('oversized white round heads',prompt)
        self.assertNotIn('cinematic photography',prompt)
        self.assertNotIn('painterly rendering',prompt)
        self.assertNotIn('Photorealistic, clean crisp focus',prompt)
    def test_casting_uses_cartoon_identity_and_white_background(self):
        service=Service.__new__(Service)
        prompt=service.casting_snapshot(self.project(),self.character(),profile={'width':768})['prompt']
        self.assertIn('pure white background',prompt)
        self.assertIn('Exactly one',prompt)
        self.assertNotIn('natural skin detail',prompt)
        self.assertNotIn('expressive brushwork',prompt)
    def test_empty_frame_has_no_character_style_instruction(self):
        scene=dict(beat='Пустой кабинет.',source_quote='Пустой кабинет.',characters=[],visible_people=0,location='Кабинет')
        prompt,_=compile_prompt(self.project(),scene,[],allow_unlocked=True)
        self.assertIn('no people or body parts',prompt)
        self.assertNotIn('oversized white round heads',prompt)
    def test_wire_preserves_marker_style_as_required_rendering(self):
        data=dict(participants=[],cast_bindings=[],required_artifacts=[],direction='A bowl on a table',action='Bowl rests',state='intact',place='Room',period='1800s',time_layer='historical',source_status='documented',source={'quote':'A bowl.'},setting_facts=[],setting_context={'quote':'A room.'},issues=[])
        with patch('imagegen.generation_scene.packet',return_value=data):
            wire=compile_wire(self.project(),{})
        self.assertIsNone(wire['error']);self.assertIn('2D marker illustration',wire['prompt'])
    def test_other_styles_keep_existing_photo_output(self):
        p=self.project();p['style']='Cinematic photorealism, natural skin texture'
        self.assertFalse(is_marker_style(p['style']))
        prompt=compile_flow_prompt(p,self.scene(),[self.character()])
        self.assertIn('cinematic photography',prompt)
    def test_illustration_mode_has_no_portrait_requirement(self):
        from imagegen.domain import DomainError
        service=Service.__new__(Service)
        with self.assertRaises(DomainError):
            service.casting_snapshot(self.project('illustration'),self.character(),profile={'width':768})

    def test_portrait_does_not_inherit_long_scene_style_or_lose_identity_at_bridge_limit(self):
        p=self.project();p['style']=MARKER_PREFIX+', '+('educational classroom scene with multiple people. '*80)
        c=self.character();c['description']='Мужчина в очках'
        prompt=Service.__new__(Service).casting_snapshot(p,c,profile={'width':768})['prompt']
        self.assertLessEqual(len(prompt),2100)
        self.assertTrue(prompt.startswith('SOLO IDENTITY REFERENCE ONLY. Exactly one person'))
        self.assertIn('Identity data: Мужчина в очках',prompt[:2200])
        self.assertNotIn('SHOT-SCALE RULE',prompt)
        self.assertNotIn('Wide/long/establishing',prompt)
        self.assertNotIn('educational classroom scene with multiple people',prompt)

    def test_oversized_portrait_identity_is_not_silently_truncated(self):
        from imagegen.domain import DomainError
        c=self.character();c['description']='Мужчина в очках '+('длинные волосы '*200)
        with self.assertRaises(DomainError):
            Service.__new__(Service).casting_snapshot(self.project(),c,profile={'width':768})

    def test_medical_story_and_closeup_hand_detail_survive_marker_simplification(self):
        prompt=compile_flow_prompt(self.project(),self.scene(),[self.character()])
        self.assertIn('Close-up arms and hands may have outlined volume',prompt)
        self.assertIn('Medical story visibility:',prompt)
        self.assertIn('never infer new signs from a diagnosis alone',prompt)
        self.assertNotIn('arms and legs drawn as single black strokes',prompt)
        self.assertNotIn('single-stroke black limbs',prompt)

    def test_explicit_visible_illness_is_preserved_in_identity_reference(self):
        from imagegen.portrait_contract import identity_description
        c=self.character();c['description']='Мужчина. Опухшая левая щека. Бледная кожа.'
        identity=identity_description(c['description'])
        self.assertIn('Опухшая левая щека',identity)
        prompt=Service.__new__(Service).casting_snapshot(self.project(),c,profile={'width':768})['prompt']
        self.assertIn('Опухшая левая щека',prompt)
        self.assertIn('do not invent visible signs from a diagnosis alone',prompt)
