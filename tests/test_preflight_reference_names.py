import unittest
from imagegen.analyzer_v3.models import SourceSpan
from imagegen.forgotten_cities.models import CityBibleIR, ForgottenCitiesDocumentIR, VisualAnchorIR
from imagegen.forgotten_cities.preflight import ForgottenCitiesPreflightValidator


class ReferencePreflightNamesTests(unittest.TestCase):
    def setUp(self):
        self.story = 'Древний город и печать.'
        anchors = [VisualAnchorIR('city', 'city_master', 'Город — главный вид', 'Houses',
                                  'historical_reconstruction', reference_required=True),
                   VisualAnchorIR('ruins', 'landscape', 'Город — современные руины', 'Ruins',
                                  'modern_ruins', reference_required=True),
                   VisualAnchorIR('seal', 'artifact', 'Печать', 'Clay seal', 'museum_display',
                                  evidence_spans=[SourceSpan(0, len(self.story), self.story)], reference_required=True)]
        doc = ForgottenCitiesDocumentIR(CityBibleIR('city', 'Город'), anchors)
        self.resources = [{'id': 'generic', 'name': 'Город', 'locked': True, 'references': {'environment': 'generic-image'}},
                          {'id': 'city', 'name': 'Город — главный вид', 'locked': False, 'references': {}},
                          {'id': 'ruins', 'name': 'Город — современные руины', 'locked': False, 'references': {}}]
        self.props = [{'id': 'seal', 'name': 'Печать', 'locked': False, 'references': {}}]
        self.project = {'analysis_metadata': {'forgotten_cities': doc.to_dict()},
                        'locations': self.resources, 'props': self.props, 'scenes': []}

    def test_generic_locked_city_does_not_authorize_unlocked_specific_anchors(self):
        report = ForgottenCitiesPreflightValidator().validate_project(self.project, self.story)
        missing = [e for e in report['errors'] if e['code'] == 'missing_required_reference']
        self.assertEqual({'Город — главный вид', 'Город — современные руины', 'Печать'},
                         {e['details']['anchor_name'] for e in missing})
        self.assertTrue(all(e['details']['anchor_name'] in e['message'] for e in missing))
        self.assertFalse(report['ok'])

    def test_exact_locked_anchors_allow_generation_and_unlock_blocks_only_that_anchor(self):
        for resource in self.resources[1:] + self.props:
            resource.update(locked=True, references={'environment': resource['id']+'-image'})
        validator = ForgottenCitiesPreflightValidator()
        self.assertTrue(validator.validate_project(self.project, self.story)['ok'])
        self.resources[2]['locked'] = False
        missing = validator.validate_project(self.project, self.story)['errors']
        self.assertEqual(1, len(missing))
        self.assertEqual('Город — современные руины', missing[0]['details']['anchor_name'])
