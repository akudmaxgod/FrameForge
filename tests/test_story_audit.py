import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imagegen.db import uid, dump, now
from imagegen.domain import DomainError
from imagegen.service import Service
from imagegen.story_audit import audit_chunk, audit_results, regenerate_audit, ensure_skull_reference, _parse_audit
from imagegen.forgotten_cities.prompts import build_prompt_packet
from imagegen.forgotten_cities.grounding import enforce_source_constraints
from tests import test_reference_casting_flow as casting
from tests.test_app import png


class StoryAuditTests(unittest.TestCase):
    setUp = casting.ReferenceCastingFlowTests.setUp
    post = casting.ReferenceCastingFlowTests.post
    cast = casting.ReferenceCastingFlowTests.cast
    finish = casting.ReferenceCastingFlowTests.finish

    def ready(self):
        scene = self.service.detail(self.pid)['scenes'][0]
        data = png()
        Path(self.tmp.name, 'ready.png').write_bytes(data)
        asset_id = uid()
        with self.service.db.transaction() as conn:
            gid = self.service.insert_generation(conn, self.pid, 'scene', {}, scene_id=scene['id'], scene_revision=1)
            conn.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)', (asset_id,self.pid,gid,'ready.png','digest',1024,576,len(data)))
            conn.execute("UPDATE generations SET state='succeeded',asset_id=? WHERE id=?", (asset_id,gid))
            conn.execute('UPDATE scenes SET active_generation_id=? WHERE id=?', (gid,scene['id']))
        return scene['id']

    def reviewer(self, sid, status='meaning'):
        return SimpleNamespace(request=Mock(return_value={'frames':[{'scene_id':sid,'status':status,
            'observed':'Пустой город','reason':'Нужен вид действующего города.',
            'corrected_prompt':'Eye-level view of a maintained ancient city, complete adobe houses.' if status in ('meaning','variety') else ''}]}))

    def test_audit_persists_and_reuses_without_enqueuing(self):
        sid=self.ready(); reviewer=self.reviewer(sid)
        before=self.service.db.all('SELECT id FROM generations')
        with patch('imagegen.story_audit.SourceFidelityReviewer',return_value=reviewer):
            first=self.app.dispatch('POST',f'/api/projects/{self.pid}/story-audit',{'scene_ids':[sid]})
        self.assertEqual(first,audit_chunk(self.service,self.pid,[sid],reviewer))
        reviewer.request.assert_called_once()
        self.assertEqual(before,self.service.db.all('SELECT id FROM generations'))
        self.assertEqual(self.service.db.all('SELECT id FROM batches'),[])
        restored=Service(self.tmp.name)
        self.assertEqual(audit_results(restored,self.pid)[0]['id'],first['items'][0]['id'])
        self.assertFalse(audit_results(restored,self.pid)[0]['stale'])
        self.assertEqual(self.app.dispatch('GET',f'/api/projects/{self.pid}/story-audit',None)['items'][0]['id'],first['items'][0]['id'])

    def test_explicit_regeneration_uses_correction_and_is_idempotent(self):
        sid=self.ready(); entry=audit_chunk(self.service,self.pid,[sid],self.reviewer(sid))['items'][0]
        assets=self.finish(self.cast('location',self.location))
        self.post(f'/locations/{self.location["id"]}/select',{'asset_id':assets[0],'revision':1})
        self.post(f'/locations/{self.location["id"]}/lock',{'references':{'environment':assets[0]},'revision':1})
        with patch.object(self.service,'project_preflight',return_value={'ok':True}), patch.object(self.service,'review_source_plans',return_value={}):
            result=regenerate_audit(self.service,self.pid,entry['id'])
            again=regenerate_audit(self.service,self.pid,entry['id'])
        self.assertEqual(result['batch_id'],again['batch_id'])
        jobs=self.service.db.all('SELECT * FROM generations WHERE batch_id=?',(result['batch_id'],))
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['scene_id'],sid)
        snapshot=json.loads(jobs[0]['snapshot'])
        self.assertIn(entry['corrected_prompt'],dump(snapshot))
        self.assertEqual(self.service.scene(sid)['current_revision'],2)
        self.assertTrue(audit_results(self.service,self.pid)[0]['stale'])

    def test_malformed_or_unavailable_review_never_rejects_or_generates(self):
        sid=self.ready()
        bad=SimpleNamespace(request=Mock(return_value={'frames':[]}))
        self.assertEqual(audit_chunk(self.service,self.pid,[sid],bad)['items'][0]['status'],'unverified')
        good=self.reviewer(sid,'keep')
        self.assertEqual(audit_chunk(self.service,self.pid,[sid],good)['items'][0]['status'],'keep')
        self.assertEqual(len(self.service.db.all('SELECT id FROM generations WHERE purpose=?',('scene',))),1)
        with self.assertRaises(DomainError):
            audit_chunk(self.service,self.pid,[sid,sid],good)

    def test_nullable_optional_fields_and_canonical_names_are_normalized(self):
        result={'frames':[{'scene_id':'one','status':' KEEP ','reason':'Соответствует источнику.',
            'observed':None,'corrected_prompt':None,'required_artifacts':[' глаз ']}]}
        parsed,errors=_parse_audit(result,['one'],{'Глаз'})
        self.assertEqual(errors,{})
        self.assertEqual(parsed['one']['required_artifacts'],['Глаз'])
        self.assertEqual(parsed['one']['corrected_prompt'],'')

    def test_one_bad_verdict_does_not_erase_valid_neighbour_and_mapping_is_not_guessed(self):
        base={'status':'keep','reason':'Соответствует.','observed':'Город','corrected_prompt':''}
        result={'frames':[dict(base,scene_id='one'),dict(base,scene_id='two',status='meaning'),dict(base,scene_id='wrong')]}
        parsed,errors=_parse_audit(result,['one','two','three'],set())
        self.assertEqual(set(parsed),{'one'})
        self.assertEqual(errors,{'two':'missing_correction','three':'missing_scene'})

    def test_format_failure_is_retried_once_and_has_safe_diagnostic(self):
        sid=self.ready();good=self.reviewer(sid,'keep').request.return_value
        reviewer=SimpleNamespace(request=Mock(side_effect=[{'frames':[]},good]))
        result=audit_chunk(self.service,self.pid,[sid],reviewer)
        self.assertEqual(result['items'][0]['status'],'keep')
        self.assertEqual(reviewer.request.call_count,2)
        self.assertFalse(result['stop_audit'])
        schema=reviewer.request.call_args.kwargs['response_schema']
        self.assertEqual(schema['properties']['frames']['items']['properties']['scene_id']['enum'],[sid])
        diagnostics=list(Path(self.tmp.name,'story_audit_diagnostics').glob('*.json'))
        self.assertEqual(len(diagnostics),1)
        self.assertEqual(json.loads(diagnostics[0].read_text())['errors'],{sid:'missing_scene'})

    def test_service_failure_is_not_retried_or_exposed(self):
        sid=self.ready();reviewer=SimpleNamespace(request=Mock(side_effect=ValueError('secret transport details')))
        result=audit_chunk(self.service,self.pid,[sid],reviewer)
        self.assertTrue(result['stop_audit']);reviewer.request.assert_called_once()
        self.assertEqual(result['items'][0]['audit_error_code'],'service_unavailable')
        self.assertNotIn('secret',dump(result))

    def test_stale_audit_and_cross_project_cannot_regenerate(self):
        sid=self.ready(); entry=audit_chunk(self.service,self.pid,[sid],self.reviewer(sid))['items'][0]
        other=self.service.create_project({'title':'Other','story':'Other story','style':'photo','mode':'objects'})
        with self.assertRaises(DomainError):regenerate_audit(self.service,other['id'],entry['id'])
        self.service.edit_scene(sid,{'manual_prompt':'Changed'},1)
        with self.assertRaises(DomainError):regenerate_audit(self.service,self.pid,entry['id'])
        self.assertEqual(self.service.db.all('SELECT id FROM batches'),[])

    def test_corrected_subject_binds_reference_instead_of_old_city_view(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        scene['data'].update(audit_visual_direction='Close study of the source artifact.',
            audit_source_quote=scene['data']['source_quote'],audit_required_artifacts=['Глаз'])
        grounded=self.service._ground_forgotten_scene(scene,self.service.project(self.pid))['data']
        self.assertEqual(grounded['props'][0]['name'],'Глаз')
        self.assertEqual(grounded['location_ref'],'')
        self.assertIn('Close study of the source artifact.',grounded['composition'])

    def test_audit_replaces_cached_eye_with_selected_skull_reference(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        project=self.service.project(self.pid)
        metadata=json.loads(project['analysis_metadata'])
        skull='Череп с треугольным отверстием'
        metadata['forgotten_cities']['anchors'].append({'anchor_id':'skull','anchor_type':'artifact','canonical_name':skull})
        project['analysis_metadata']=metadata
        scene['data'].update(audit_visual_direction='Lateral view of the skull with a triangular opening.',
            audit_source_quote=scene['data']['source_quote'],audit_required_artifacts=[skull])
        with patch('imagegen.forgotten_cities.fidelity.SourceFidelityReviewer.cached_plan',return_value={'status':'pass','required_artifacts':['Глаз']}) as cached:
            grounded=self.service._ground_forgotten_scene(scene,project)['data']
        cached.assert_not_called()
        self.assertEqual(grounded['forgotten_cities_prompt_contract']['required_artifacts'],[skull])
        self.assertEqual([p['name'] for p in grounded['props']],[skull])
        self.assertEqual(grounded['location_ref'],'')

    def test_audit_does_not_replace_inhabited_city_with_empty_ruins(self):
        quote='Для жителей здесь готовят, хранят вещи и работают.'
        project=self.service.project(self.pid);project['story']=quote
        scene=self.service.detail(self.pid)['scenes'][0]
        scene['data'].update(source_quote=quote,source_start=0,audit_source_quote=quote,
            audit_visual_direction='Empty eroded ruins, no people.',audit_required_artifacts=[])
        contract=self.service._ground_forgotten_scene(scene,project)['data']['forgotten_cities_prompt_contract']
        self.assertEqual(contract['city_population'],'inhabited')
        self.assertIn('inhabited ancient city',contract['visual_direction'])
        self.assertNotIn('Empty eroded ruins',contract['visual_direction'])

    def test_explicit_empty_artifacts_remove_old_eye_reference(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        scene['data'].update(audit_visual_direction='Ancient worker spinning thread.',
            audit_source_quote=scene['data']['source_quote'],audit_required_artifacts=[],
            audit_source_binding='artifact_action',audit_time_layer='historical_reconstruction',audit_people=1)
        grounded=self.service._ground_forgotten_scene(scene,self.service.project(self.pid))['data']
        self.assertEqual(grounded['props'],[])
        self.assertEqual(grounded['visible_people'],1)

    def test_two_anonymous_editorial_roles_are_distinct(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        scene['data'].update(audit_visual_direction='Two anonymous participants exchanging goods.',
            audit_source_quote=scene['data']['source_quote'],audit_required_artifacts=[],
            audit_source_binding='context',audit_time_layer='historical_reconstruction',audit_people=2)
        grounded=self.service._ground_forgotten_scene(scene,self.service.project(self.pid))['data']
        self.assertEqual(len(set(grounded['episodic_people'])),2)

    def test_scoped_bridge_batch_accepts_only_unique_scenes_of_open_project(self):
        import time
        sid=self.ready();other='second-scene'
        with self.service.db.transaction() as conn:
            conn.execute('INSERT INTO scenes(id,project_id,position) VALUES(?,?,?)',(other,self.pid,2))
        self.service.active_project_id=self.pid
        self.app.flow_bridge_v1.update(last_seen=time.time(),adapter_connected=True,
            adapter_instance_id='adapter',project_id=self.pid,reference_casting=True,scoped_scene_start=True)
        payload={'action':'start','project_id':self.pid,'scene_ids':[sid,other]}
        self.assertTrue(self.app.dispatch('POST','/api/bridge/v1/command',payload)['ok'])
        self.assertEqual(self.app.flow_bridge_v1['command']['scene_ids'],[sid,other])
        for selected in ([sid,sid],[sid,'foreign-scene']):
            with self.assertRaises(DomainError):
                self.app.dispatch('POST','/api/bridge/v1/command',{**payload,'scene_ids':selected})

    def test_reference_correction_preserves_burial_state(self):
        scene=self.service.detail(self.pid)['scenes'][0]
        quote='В погребении нашли женщину с искусственным глазом.'
        scene['data'].update(source_quote=quote,audit_source_quote=quote,
            audit_visual_direction='One burial with a small eye prosthesis.',audit_required_artifacts=['Глаз'])
        project=self.service.project(self.pid);project['story']=quote
        grounded=self.service._ground_forgotten_scene(scene,project)['data']
        self.assertEqual(grounded['subject_state'],'buried_remains')
        self.assertEqual(grounded['visible_people'],0)

    def test_failed_dispatch_can_retry_without_another_revision(self):
        sid=self.ready();entry=audit_chunk(self.service,self.pid,[sid],self.reviewer(sid))['items'][0]
        with patch.object(self.service,'project_preflight',return_value={'ok':True}), patch.object(self.service,'batch',side_effect=[DomainError('temporarily unavailable'),{'id':'retried'}]):
            with self.assertRaises(DomainError):regenerate_audit(self.service,self.pid,entry['id'])
            self.assertFalse(audit_results(self.service,self.pid)[0]['stale'])
            self.assertEqual(regenerate_audit(self.service,self.pid,entry['id'])['batch_id'],'retried')
        self.assertEqual(self.service.scene(sid)['current_revision'],2)

    def test_skull_upgrade_is_scoped_and_creates_no_jobs(self):
        story='Нашли женщину с искусственным глазом у поврежденного черепа. Позже нашли череп девочки. В правой теменной области было вырезано отверстие треугольной формы. Края зажили.'
        with self.service.db.transaction() as conn:conn.execute('UPDATE projects SET story=? WHERE id=?',(story,self.pid))
        before=self.service.db.all('SELECT id FROM generations')
        first=ensure_skull_reference(self.service,self.pid);second=ensure_skull_reference(self.service,self.pid)
        self.assertTrue(first['created']);self.assertFalse(second['created'])
        self.assertEqual(first['prop_id'],second['prop_id'])
        prop=self.service.prop(first['prop_id'])
        self.assertFalse(prop['locked'])
        self.assertIn('RIGHT parietal',prop['description'])
        metadata=json.loads(self.service.project(self.pid)['analysis_metadata'])['forgotten_cities']
        for quote in ('Позже нашли череп девочки.','Края зажили.'):
            packet=build_prompt_packet(metadata,'e',quote,'',story_text=story,source_quote=quote,source_start=story.index(quote))
            packet=enforce_source_constraints(packet,story)
            self.assertEqual(packet['required_artifacts'],[prop['name']])
            self.assertIn('triangular opening',packet['compiled_prompt'])
            self.assertIn('circular drilled hole',packet['compiled_prompt'])
        first_quote=story.split(' Позже')[0]
        packet=build_prompt_packet(metadata,'e',first_quote,'',story_text=story,source_quote=first_quote,source_start=0)
        self.assertNotIn(prop['name'],packet['required_artifacts'])
        self.assertEqual(before,self.service.db.all('SELECT id FROM generations'))
        from imagegen.forgotten_cities.bible import ForgottenCitiesBibleBuilder
        from imagegen.forgotten_cities.models import CityBibleIR
        from imagegen.analyzer_v3.models import DocumentIR, EventIR, SourceSpan
        span=SourceSpan(story.index('Позже'),len(story),story[story.index('Позже'):])
        doc=DocumentIR('hash',events=[EventIR('event','section',span)])
        anchors=ForgottenCitiesBibleBuilder()._build_anchors(CityBibleIR('city','Город'),[],doc,story)
        self.assertEqual(len([a for a in anchors if a.anchor_id=='anchor_artifact_trepanned_skull']),1)
