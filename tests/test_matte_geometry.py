import unittest
import numpy as np
from PIL import Image
from imagegen import frame_matte as detector

class NaturalEdges(unittest.TestCase):
    def test_smooth_sky_and_ground_with_center_building_are_not_matting(self):
        a=np.zeros((576,1024,3),dtype=np.uint8)
        for y in range(576):a[y]=[100+y//5,130+y//6,150+y//7]
        a[175:400,400:620]=[55,40,30]
        image=Image.fromarray(a)
        self.assertFalse(detector.detect_matte(image)['detected'])
    def test_horizontally_flat_colored_edges_with_gradient_are_not_bars(self):
        a=np.zeros((576,1024,3),dtype=np.uint8)
        for x in range(1024):a[:,x]=[60+x//7,85+x//8,100+x//9]
        a[140:450,350:650]=[20,70,20]
        image=Image.fromarray(a)
        self.assertFalse(detector.detect_matte(image)['detected'])
    def test_single_white_pillarbox_is_confirmed(self):
        a=np.random.default_rng(14).integers(20,200,(576,1024,3),dtype=np.uint8);a[:,:65]=255
        self.assertTrue(detector.detect_matte(Image.fromarray(a))['detected'])
    def test_colored_paired_matte_is_confirmed(self):
        a=np.random.default_rng(14).integers(20,200,(576,1024,3),dtype=np.uint8);a[:30]=[75,80,95];a[-30:]=[75,80,95]
        self.assertTrue(detector.detect_matte(Image.fromarray(a))['detected'])
    def test_soft_thin_letterbox_is_allowed(self):
        a=np.random.default_rng(2).integers(40,200,(576,1024,3),dtype=np.uint8)
        a[:7]=0;a[-7:]=0;a[7]=5;a[-8]=5
        verdict=detector.detect_matte(Image.fromarray(a))
        self.assertFalse(verdict['detected'])
        self.assertEqual(verdict['reason'],'thin_matte_allowed')
    def test_wide_frame_is_rejected(self):
        a=np.random.default_rng(2).integers(40,200,(576,1024,3),dtype=np.uint8);a[:50]=0;a[-50:]=0
        self.assertTrue(detector.detect_matte(Image.fromarray(a))['detected'])
