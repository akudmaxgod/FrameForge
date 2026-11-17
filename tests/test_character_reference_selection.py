import copy
import unittest
from imagegen.character_selection import project_selection, resolve_links
from imagegen.domain import resolve_scene_presence_contract

def char(n, name):
    return dict(id=f'uuid-{n}',cast_id=f'ch_{n}',name=name,canonical_name=name,aliases=[],
                role='minor',description='Человек',wardrobe={},revision=1,locked=False,references={})

class CharacterReferenceSelectionTests(unittest.TestCase):
    def project(self):
        return dict(mode='dynamic',story='Врач осматривает пациента.',characters=[char(1,'Доктор'),char(2,'Пациент'),char(3,'Автор')],locations=[],props=[],
          scenes=[dict(id='s1',position=1,current_revision=1,data=dict(analysis_authority='deepseek-v2',beat='Врач осматривает пациента.',source_quote='Врач осматривает пациента.',characters=['Доктор','Пациент'],character_ids=['uuid-1','uuid-2'],visible_people=2))])

    def test_registry_preserved_and_minor_physical_selected(self):
        p=self.project();before=copy.deepcopy(p);r=project_selection(p)
        self.assertEqual(r['character_reference_ids'],['uuid-1','uuid-2']);self.assertEqual(p,before)
        self.assertEqual(len(r['reference_requirements']),2)

    def test_uuid_and_cast_id_only_survive_role_description(self):
        for ids in (['uuid-1','uuid-2'],['ch_1','ch_2']):
            p=self.project();p['scenes'][0]['data'].pop('characters');p['scenes'][0]['data']['character_ids']=ids
            self.assertEqual(project_selection(p)['character_reference_ids'],['uuid-1','uuid-2'])

    def test_last_presence_removed_preserves_portrait(self):
        p=self.project();p['characters'][0].update(locked=True,references={'portrait':'saved'})
        p['scenes']=[];r=project_selection(p)
        self.assertEqual(r['character_reference_ids'],[]);self.assertEqual(p['characters'][0]['references']['portrait'],'saved')

    def test_revision_add_remove_and_names_override_stale_ids(self):
        p=self.project();scene=p['scenes'][0]['data'];scene['characters']=['Автор']
        linked,issues=resolve_links(scene,p['characters'],names_only=True)
        self.assertEqual(linked['character_ids'],['ch_3']);self.assertFalse(issues)
        p['scenes'][0]['data']=linked
        self.assertEqual(project_selection(p)['character_reference_ids'],['uuid-3'])

    def test_ambiguous_alias_is_reported(self):
        chars=[char(1,'Первый врач'),char(2,'Второй врач')]
        for c in chars:c['aliases']=['Врач']
        linked,issues=resolve_links({'characters':['Врач']},chars)
        self.assertEqual(linked['characters'],[]);self.assertEqual(issues[0]['code'],'ambiguous_character_link')

    def test_distinct_unnamed_patients_not_merged(self):
        chars=[char(1,'Пациент первой истории'),char(2,'Пациент второй истории')]
        linked,issues=resolve_links({'character_ids':['uuid-2']},chars)
        self.assertEqual(linked['character_ids'],['ch_2']);self.assertFalse(issues)

    def test_mode_matrix(self):
        for mode,expected in [('dynamic',2),('illustration',0),('characters',2),('objects',2)]:
            p=self.project();p['mode']=mode
            self.assertEqual(len(project_selection(p)['character_reference_ids']),expected)

    def test_unlocked_physical_is_included_without_reference(self):
        r=project_selection(self.project())
        self.assertFalse(any(x['ready'] for x in r['reference_requirements']))

    def test_invalid_signed_source_remains_inspectable(self):
        from unittest.mock import patch
        from imagegen.domain import DomainError
        with patch('imagegen.generation_scene.normalize',side_effect=DomainError('Source needs review')):
            result=project_selection(self.project())
        self.assertEqual(result['character_reference_ids'],[])
        self.assertEqual(result['character_selection_issues'][0]['code'],'invalid_character_source')
