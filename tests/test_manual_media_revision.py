from pathlib import Path
import copy, unittest
from unittest.mock import patch
from tests import test_veo_animation
from imagegen.editing import normalize
from imagegen.domain import DomainError
from imagegen.export_worker import render
from PIL import Image
import av

class ManualMediaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_veo_animation.TestVeoAnimationPlan();self.fixture.setUp();self.s=self.fixture.service
        self.pid=self.fixture._create_sample_project(1)
    def tearDown(self):self.fixture.tearDown()
    def test_unanalyzed_image_overlay_render_revert_and_isolation(self):
        path=Path(self.fixture.temp_dir.name)/'manual.png';Image.new('RGB',(160,90),'red').save(path)
        item=self.s.media_library.import_file(self.pid,path)
        self.assertTrue(all(x['analysis_status']=='pending' for x in item['segments']))
        before=self.s.db.all('SELECT * FROM scene_revisions')
        with patch.object(self.s.media_analysis,'start_job',side_effect=AssertionError('No analyzer for manual insert')):
            result=self.s.editing.library_clip(self.pid,item['id'])
        foreign=self.s.create_project({'title':'Other','story':'Other story','style':'Cinematic'})['id']
        with self.assertRaises(DomainError):self.s.editing.library_clip(foreign,item['id'])
        base=copy.deepcopy(result['clip']);base.update(source_kind='scene',scene_id=self.s.detail(self.pid)['scenes'][0]['id'],source_scene_revision=1,trim_out_ms=1000)
        # Use a real generated video as base instead of inventing an image generation.
        scene=self.s.detail(self.pid)['scenes'][0]
        self.s.import_scene_video(scene['id'],self.fixture._create_valid_mp4(width=160,height=90,duration=1),filename='base.mp4')
        before=self.s.db.all('SELECT * FROM scene_revisions')
        doc=self.s.editing.sync(self.pid,{'expected_revision':0,'add_only':True})['document']
        original=copy.deepcopy(doc)
        layer=result['clip'];layer.update(edit_clip_id=doc['clips'][0]['edit_clip_id'],origin_scene_id=scene['id'],enabled=True,trim_out_ms=1000)
        doc['clips'][0]['footage_overlay']=layer
        saved=self.s.editing.save(self.pid,{'expected_revision':0,'document':doc})
        out=Path(self.fixture.temp_dir.name)/'manual.mp4'
        render(self.s,saved['document'],out,lambda *_:None,lambda:False,size=(160,90))
        with av.open(str(out)) as media:
            frame=next(media.decode(video=0)).to_ndarray(format='rgb24');self.assertGreater(float(frame[:,:,0].mean()),230)
        restored=copy.deepcopy(saved['document']);del restored['clips'][0]['footage_overlay']
        self.s.editing.save(self.pid,{'expected_revision':saved['revision'],'document':restored})
        self.assertEqual(restored['clips'],original['clips'])
        self.assertEqual(before,self.s.db.all('SELECT * FROM scene_revisions'))
    def test_manual_video_pending_and_mute_lane(self):
        path=Path(self.fixture.temp_dir.name)/'clip.mp4';path.write_bytes(self.fixture._create_valid_mp4(width=160,height=90,duration=1))
        item=self.s.media_library.import_file(self.pid,path)
        result=self.s.editing.library_clip(self.pid,item['id'])
        c=result['clip'];c['muted']=False
        doc={'clips':[c],'transitions':[],'audio_tracks':[{'role':'voice','enabled':True}],'source_audio':True,'footage_muted':True}
        self.assertTrue(normalize(doc)['clips'][0]['muted'])
        self.assertFalse(doc['clips'][0]['muted'])
        self.assertEqual(normalize(doc)['audio_tracks'],doc['audio_tracks'])
        doc['footage_muted']=False
        self.assertFalse(normalize(doc)['clips'][0]['muted'])


if __name__=='__main__':unittest.main()
