import unittest,io,json
import numpy as np
from unittest.mock import patch
from PIL import Image
from tests import test_advisory_image_review as fixture
from imagegen import frame_matte as detector
from imagegen.worker import Worker

class CompositionEvidenceTests(unittest.TestCase):
    setUp=fixture.AdvisoryImageReviewTests.setUp
    def test_thin_matte_is_accepted_unchanged_without_cropping(self):
        a=np.random.default_rng(3).integers(40,200,(576,1024,3),dtype=np.uint8);a[:7]=0;a[-7:]=0
        buf=io.BytesIO();Image.fromarray(a).save(buf,'PNG')
        def validate(snapshot,data):
            with Image.open(io.BytesIO(data)) as image:Worker._validate_matte(image)
        with patch.object(self.service,'scene_snapshot',return_value=dict(self.snapshot)):
            result=self.service.upload_scene_image(self.sid,buf.getvalue(),image_validator=validate)
        row=self.service.db.one('SELECT * FROM generations WHERE id=?',(result['generation_id'],))
        self.assertEqual(row['state'],'succeeded')
        self.assertNotIn('format_repair',json.loads(row['snapshot']))
        asset=self.service.asset(row['asset_id'])
        with Image.open(self.service.root/asset['path']) as saved:
            np.testing.assert_array_equal(np.asarray(saved),a)
    def test_rejected_files_are_bounded_deduplicated_and_never_count_as_ready(self):
        from imagegen.worker import ContentRejectedError
        error=ContentRejectedError('confirmed matte',422)
        error.details={'code':'COMPOSITION_MATTE','retryable':True}
        with patch.object(self.service,'scene_snapshot',return_value=dict(self.snapshot)):
            for n in [0,0,1,2,3,4,5,6,7]:
                buf=io.BytesIO();Image.new('RGB',(1024,576),(80+n,70,60)).save(buf,'PNG')
                with self.assertRaises(ContentRejectedError):
                    self.service.upload_scene_image(self.sid,buf.getvalue(),image_validator=lambda *a: (_ for _ in ()).throw(error))
        evidence=list((self.service.root/'composition-rejected').rglob('*.png'))
        self.assertEqual(len(evidence),6)
        self.assertEqual(len(list((self.service.root/'composition-rejected').rglob('*.json'))),6)
        self.assertFalse(self.service.scene(self.sid).get('active_generation_id'))
        self.assertEqual(self.service.db.one('SELECT state FROM generations WHERE id=?',(self.gid,))['state'],'queued')

