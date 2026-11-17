import json
import tempfile
import unittest
from unittest.mock import patch

from imagegen.analyzer_v3.checkpoint import serialize_analysis, restore_analysis
from imagegen.analyzer_v3.models import DocumentIR, DocumentMode, EventIR, SourceSpan
from imagegen.analyzer_v3.planner_models import VisualEventPlan, SelectedEventIR
from imagegen.analyzer_v3.service import StoryAnalyzerV3Service
from imagegen.analyzer_v3.persistence import V3AnalysisRepository
from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.worker import Worker


STORY = 'An old vessel stands inside a workshop.'


def analysis_fixture():
    span = SourceSpan(0, len(STORY), STORY)
    doc = DocumentIR('storyhash', mode=DocumentMode.NARRATIVE, events=[EventIR('ev', 'section', span)],
                     metadata={'provenance': {'source': STORY}})
    plan = VisualEventPlan('plan', 'storyhash', DocumentMode.NARRATIVE, 1,
                          selected_events=[SelectedEventIR('selected', 'ev', 0, 'section', span)],
                          provenance={'source_span': span.to_dict()})
    analysis = StoryAnalyzerV3Service.export_to_app_analysis(doc, plan, STORY, mode='objects')
    analysis['locations'] = [{'name': analysis['scenes'][0]['location'], 'description': 'An ancient workshop.'}]
    return {**analysis, '_doc_ir': doc, '_plan': plan}


class AnalysisCheckpointTests(unittest.TestCase):
    def test_real_ir_roundtrip_preserves_types_spans_enums_and_metadata(self):
        analysis = analysis_fixture()
        serialized = serialize_analysis(analysis)
        loaded = restore_analysis(json.loads(json.dumps(serialized)))
        self.assertIsInstance(analysis['_doc_ir'], DocumentIR)
        self.assertIsInstance(analysis['_plan'], VisualEventPlan)
        self.assertIsInstance(loaded['_doc_ir'], DocumentIR)
        self.assertIsInstance(loaded['_plan'], VisualEventPlan)
        self.assertEqual(loaded['_doc_ir'].to_dict(), analysis['_doc_ir'].to_dict())
        self.assertEqual(loaded['_plan'].to_dict(), analysis['_plan'].to_dict())
        self.assertEqual(serialize_analysis(loaded), serialized)

    def test_real_database_checkpoint_failure_then_resume_saves_full_v3_record(self):
        with tempfile.TemporaryDirectory() as root:
            service = Service(root)
            project = service.create_project({'title': 'Checkpoint', 'story': STORY, 'style': 'documentary', 'mode': 'objects'})
            profile = {**service.settings(), 'width': 1024, 'height': 576, 'unet': 'test', 'clip': 'test', 'vae': 'test'}
            with service.db.transaction() as conn:
                gid = service.insert_generation(conn, project['id'], 'analysis', {'project': project, 'profile': profile})
            generation = service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
            worker = Worker(service)
            analysis = analysis_fixture()
            with patch.object(service, 'profile', side_effect=DomainError('Temporary downstream failure')):
                with self.assertRaisesRegex(DomainError, 'downstream'):
                    worker._save_analysis(generation, analysis, project['version'])
            saved = service.db.one('SELECT * FROM generations WHERE id=?', (gid,))
            checkpoint = json.loads(saved['snapshot'])['pending_analysis']
            self.assertIsInstance(checkpoint['_doc_ir'], dict)
            self.assertEqual(checkpoint['_plan']['selected_events'][0]['span']['text'], STORY)
            with patch.object(service, 'profile', return_value=profile):
                worker.prepare(saved)
            self.assertEqual(service.db.one('SELECT state FROM generations WHERE id=?', (gid,))['state'], 'succeeded')
            record = V3AnalysisRepository(db=service.db).get_by_story_id(project['id'])
            self.assertEqual(record.get_document_ir().to_dict(), analysis['_doc_ir'].to_dict())
            self.assertEqual(record.get_visual_event_plan().to_dict(), analysis['_plan'].to_dict())
            self.assertEqual(len(service.db.all('SELECT id FROM scenes WHERE project_id=?', (project['id'],))), 1)
            self.assertEqual(len(service.db.all('SELECT id FROM generations WHERE project_id=?', (project['id'],))), 1)


if __name__ == '__main__':
    unittest.main()
