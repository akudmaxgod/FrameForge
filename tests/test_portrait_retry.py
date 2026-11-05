import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
from imagegen.service import Service
from imagegen.domain import DomainError
from imagegen.worker import Worker
from imagegen.db import dump
from imagegen.portrait_contract import identity_description,name_gender_hint,marker_head_count,marker_scene_review
from imagegen.visual_style import MARKER_PREFIX

def drawing(heads=1):
    image=Image.new('RGB',(768,432),'white');draw=ImageDraw.Draw(image)
    for x in ([275] if heads==1 else [110,460]):
        draw.ellipse((x,50,x+170,220),fill='white',outline='black',width=5)
        draw.ellipse((x+50,115,x+58,125),fill='black');draw.ellipse((x+110,115,x+118,125),fill='black')
        draw.arc((x+60,145,x+115,185),0,180,fill='black',width=4)
    return image

def png(image):
    value=io.BytesIO();image.save(value,format='PNG');return value.getvalue()

class PortraitRetryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp());self.service=Service(self.root)
        p=self.service.create_project(dict(title='Portrait retry',story='Врач осматривает пациента.',mode='dynamic',style=MARKER_PREFIX+', 2D hand-drawn illustration'))
        self.pid=p['id'];self.cid='doctor-test'
        with self.service.db.transaction() as c:
            c.execute('INSERT INTO characters(id,project_id,name,description,wardrobe) VALUES(?,?,?,?,?)',(self.cid,self.pid,'Отто','Врач в очках, принимающий Марту. Назначает обследование.','{}'))
    def tearDown(self):
        import shutil
        shutil.rmtree(self.root)
    def test_identity_prompt_has_no_patient_or_medical_action(self):
        snap=self.service.casting_snapshot(self.service.project(self.pid),self.service.character(self.cid))
        self.assertNotIn('Марту',snap['prompt']);self.assertNotIn('принимающий',snap['prompt']);self.assertNotIn('Назначает',snap['prompt'])
        self.assertIn('Врач в очках',snap['prompt']);self.assertEqual(snap['gender_constraint']['value'],'male')
        self.assertEqual(name_gender_hint('Анри'),None)
    def test_explicit_description_overrides_name_hint(self):
        ch=self.service.character(self.cid);ch['description']='Женщина с короткими волосами.'
        self.assertEqual(self.service.casting_snapshot(self.service.project(self.pid),ch)['gender_constraint']['value'],'female')
    def test_single_default_and_repeated_click_is_idempotent(self):
        a=self.service.cast(self.cid,count=2);b=self.service.cast(self.cid)
        self.assertEqual(len(a['generation_ids']),1);self.assertEqual(a['generation_ids'],b['generation_ids'])
        self.assertEqual(len(self.service.db.all("SELECT id FROM generations WHERE purpose='casting'")),1)
    def test_repeated_queued_requests_are_consolidated_but_active_preserved(self):
        project=self.service.project(self.pid);snap=self.service.casting_snapshot(project,self.service.character(self.cid))
        with self.service.db.transaction() as c:
            ids=[self.service.insert_generation(c,self.pid,'casting',snap,character_id=self.cid,character_revision=1) for _ in range(3)]
        result=self.service.cast(self.cid)
        self.assertEqual(len(result['generation_ids']),1)
        self.assertEqual(len(self.service.db.all("SELECT id FROM generations WHERE purpose='casting' AND state='queued' AND cancel_requested=0")),1)
    def test_claim_refreshes_stale_portrait_and_freezes_exact_sent_prompt(self):
        gid=self.service.cast(self.cid)['generation_ids'][0]
        with self.service.db.transaction() as c:
            old=json.loads(c.execute('SELECT snapshot FROM generations WHERE id=?',(gid,)).fetchone()[0])
            old['prompt']='Old classroom scene with many people'
            c.execute('UPDATE generations SET snapshot=? WHERE id=?',(dump(old),gid))
        claimed=self.service.claim_character_cast(self.cid,gid,'request')
        self.assertTrue(claimed['prompt'].startswith('SOLO IDENTITY REFERENCE ONLY. Exactly one person'))
        self.assertNotIn('Old classroom scene',claimed['prompt'])
        snapshot=json.loads(self.service.db.one('SELECT snapshot FROM generations WHERE id=?',(gid,))['snapshot'])
        self.assertEqual(snapshot['prompt'],claimed['prompt'])
        self.assertEqual(snapshot['portrait_contract_version'],'solo-portrait/2')
        self.assertEqual(self.service.claim_character_cast(self.cid,gid,'request')['prompt'],claimed['prompt'])

    def test_claim_does_not_allow_another_external_request(self):
        gid=self.service.cast(self.cid)['generation_ids'][0]
        self.service.claim_character_cast(self.cid,gid,'request-a');self.service.claim_character_cast(self.cid,gid,'request-a')
        with self.assertRaises(DomainError):self.service.claim_character_cast(self.cid,gid,'request-b')
        self.assertEqual(self.service.cast(self.cid)['generation_ids'],[gid])
    def test_upload_completes_exact_request_and_repeated_upload_is_idempotent(self):
        gid=self.service.cast(self.cid)['generation_ids'][0];self.service.claim_character_cast(self.cid,gid,'request')
        r=self.service.upload_character_portrait(self.cid,png(drawing()),generation_id=gid,request_id='request')
        self.assertFalse(r['locked']);self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?',(gid,))['state'],'succeeded')
        again=self.service.upload_character_portrait(self.cid,png(drawing()),generation_id=gid,request_id='request')
        self.assertEqual(r['asset_id'],again['asset_id']);self.assertEqual(len(self.service.db.all("SELECT id FROM generations WHERE purpose='casting'")),1)
    def test_locked_portrait_is_retained_until_new_candidate_is_chosen(self):
        old=self.service.upload_character_portrait(self.cid,png(drawing()),lock=True)
        gid=self.service.cast(self.cid)['generation_ids'][0];self.service.claim_character_cast(self.cid,gid,'next')
        new=self.service.upload_character_portrait(self.cid,png(drawing()),generation_id=gid,request_id='next')
        self.assertEqual(json.loads(new['references_json'])['portrait'],old['asset_id']);self.assertTrue(new['locked'])
        self.service.lock(self.cid,{'portrait':new['asset_id']},1,{'one_person_confirmed':True})
        self.assertEqual(json.loads(self.service.character(self.cid)['references_json'])['portrait'],new['asset_id'])
    def test_uncertain_multiple_contours_preserve_candidate_and_require_manual_lock(self):
        self.assertEqual(marker_head_count(drawing(2)),2)
        gid=self.service.cast(self.cid)['generation_ids'][0];self.service.claim_character_cast(self.cid,gid,'request')
        result=self.service.upload_character_portrait(self.cid,png(drawing(2)),generation_id=gid,request_id='request')
        row=self.service.db.one('SELECT state,snapshot,asset_id FROM generations WHERE id=?',(gid,))
        self.assertEqual(row['state'],'succeeded');self.assertTrue(row['asset_id'])
        self.assertEqual(json.loads(row['snapshot'])['portrait_review']['status'],'unverified')
        self.assertFalse(self.service.character(self.cid)['locked'])
        with self.assertRaises(DomainError):
            self.service.lock(self.cid,{'portrait':result['asset_id']},1)
        self.service.lock(self.cid,{'portrait':result['asset_id']},1,{'one_person_confirmed':True})

    def test_terminal_failure_allows_one_fresh_request(self):
        gid=self.service.cast(self.cid)['generation_ids'][0];self.service.claim_character_cast(self.cid,gid,'request')
        self.service.fail_character_cast(self.cid,gid,'request','ERR_GEN_FAILED')
        self.assertNotEqual(self.service.cast(self.cid)['generation_ids'],[gid])
    def test_marker_scene_does_not_use_photographic_identity(self):
        worker=Worker.__new__(Worker)
        snap={'style':MARKER_PREFIX,'profile':{'width':768,'height':432},'expected_people':1,'references':[{'role':'portrait','asset_id':'x'}]}
        with patch.object(worker,'_has_panel_grid',return_value=False),patch.object(worker,'_validate_matte'),patch.object(worker,'_has_embedded_text',return_value=False),patch.object(worker,'_identity_similarity',side_effect=AssertionError('ArcFace must not run for markers')):
            worker._validate_external_scene_image_strict(snap,png(drawing()))
        self.assertEqual(snap['identity_review']['status'],'unverified')
        self.assertTrue(marker_scene_review(snap,drawing(2)))
        self.assertEqual(snap['composition_review']['status'],'needs_review')
    def test_photo_style_does_not_bypass_identity(self):
        snap={'style':'Cinematic photorealism'}
        self.assertFalse(marker_scene_review(snap,drawing()))

    def test_gender_of_patient_does_not_override_doctor(self):
        from imagegen.portrait_contract import subject_gender
        self.assertEqual(subject_gender('Врач, обследующий Леона.','Анри')['value'],'male')
        self.assertEqual(subject_gender('Врач, обследующая Леона.','Алекс')['value'],'female')
        self.assertEqual(subject_gender('Женщина, обследующая мужчину.','Павел')['value'],'female')

    def test_unknown_drawing_requires_explicit_visual_confirmation(self):
        image=Image.new('RGB',(768,432),'white')
        result=self.service.upload_character_portrait(self.cid,png(image),lock=True)
        self.assertFalse(result['locked'])
        with self.assertRaises(DomainError):self.service.lock(self.cid,{'portrait':result['asset_id']},1)
        self.service.lock(self.cid,{'portrait':result['asset_id']},1,{'one_person_confirmed':True})
        self.assertTrue(self.service.character(self.cid)['locked'])

    def test_api_scopes_one_character_and_rejects_old_bridge(self):
        import threading,time
        from imagegen.server import Application
        app=Application.__new__(Application);app.service=self.service;app.flow_bridge_v1_lock=threading.Lock()
        self.service.active_project_id=self.pid
        app.flow_bridge_v1=dict(last_seen=time.time(),adapter_connected=True,adapter_instance_id='test-adapter',project_id=self.pid,running=False,reference_casting=True,character_casting=False,scoped_scene_start=True)
        gid=self.service.cast(self.cid)['generation_ids'][0]
        args=dict(action='start',project_id=self.pid,generation_ids=[gid])
        with self.assertRaises(DomainError):app.dispatch('POST','/api/bridge/v1/command',args)
        app.flow_bridge_v1['character_casting']=True
        result=app.dispatch('POST','/api/bridge/v1/command',args)
        self.assertTrue(result['ok']);self.assertEqual(app.flow_bridge_v1['command']['generation_ids'],[gid])

    def test_concurrent_clicks_reuse_one_waiting_request(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids=list(pool.map(lambda _:self.service.cast(self.cid)['generation_ids'][0],range(8)))
        self.assertEqual(len(set(ids)),1)
