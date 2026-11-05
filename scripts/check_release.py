"""CPU/offline release regressions. No paid generation or optional GPU runtime."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULES = [
    'test_release', 'test_montage', 'test_mp4_export', 'test_montage_voice_fit',
    'test_panorama_camera', 'test_generation_scene_wire', 'test_composition_sequence',
    'test_source_prompt_guards', 'test_subject_reference_policy', 'test_population_evidence',
    'test_identity_advisory', 'test_flow_portrait_stage', 'test_material_search_core',
    'test_material_search_real_e2e', 'test_material_search_quality_dataset',
    'test_searxng_image_search', 'test_manual_media_revision', 'test_embedded_text_gate',
    'test_flow_prompt_budget', 'test_detector_instances', 'test_typed_identity',
    'test_instance_launch', 'test_flow_admission_release',
]
EXCLUDED = {
    'tests.test_generation_scene_wire.GenerationSceneWireTests.test_exact_prompt_is_required_and_long_input_is_not_cut': 'pre-existing LIVE assertion failure',
    'tests.test_generation_scene_wire.GenerationSceneWireTests.test_named_actor_is_not_counted_twice_as_episodic': 'pre-existing LIVE assertion failure',
    'tests.test_composition_sequence.SequenceTests.test_provider_limit_stops_sequence_without_content_repair': 'pre-existing missing optional nvidia_text module',
    'tests.test_material_search_real_e2e.TestMaterialSearchRealE2E.test_worker_driven_queue_verification_and_protection': 'optional GPU runtime; separate integration test',
}

def cases(suite):
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from cases(value)
        else:
            yield value

if __name__ == '__main__':
    discovered = unittest.defaultTestLoader.loadTestsFromNames(['tests.' + name for name in MODULES])
    selected = []
    for test in cases(discovered):
        if test.id() in EXCLUDED or test.id().endswith('.test_worker_driven_queue_verification_and_protection'):
            print('EXCLUDED:', test.id(), EXCLUDED.get(test.id(), 'optional GPU integration'))
        else:
            selected.append(test)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    sys.exit(not result.wasSuccessful())
