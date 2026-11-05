import unittest
from types import SimpleNamespace

import numpy as np

from imagegen.detector_process import count_instances


class Tensor:
    def __init__(self, value):
        self.value = np.asarray(value)

    def cpu(self):
        return self

    def numpy(self):
        return self.value

    def __len__(self):
        return len(self.value)


class Boxes:
    def __init__(self, classes):
        self.cls = Tensor(classes)
        self.conf = Tensor(np.linspace(.95, .6, len(classes)))

    def __len__(self):
        return len(self.cls)


def detection(masks, classes=None):
    return SimpleNamespace(
        boxes=Boxes(classes if classes is not None else [0] * len(masks)),
        masks=SimpleNamespace(data=Tensor(masks)),
    )


class DetectorInstanceTests(unittest.TestCase):
    def test_one_person_detected_as_full_and_partial_body_counts_once(self):
        masks = np.zeros((3, 60, 100))
        masks[0, 5:55, 5:40] = 1
        masks[1, 5:55, 60:95] = 1
        masks[2, 5:55, 18:40] = 1
        self.assertEqual(count_instances(detection(masks)), 2)

    def test_three_different_people_remain_three(self):
        masks = np.zeros((3, 60, 100))
        masks[0, 5:55, 5:35] = 1
        masks[1, 5:55, 30:65] = 1
        masks[2, 5:55, 60:95] = 1
        self.assertEqual(count_instances(detection(masks)), 3)

    def test_small_person_inside_another_bounding_area_remains_separate(self):
        masks = np.zeros((2, 60, 100))
        masks[0, 5:55, 5:95] = 1
        masks[1, 20:40, 40:60] = 1
        self.assertEqual(count_instances(detection(masks)), 2)
        masks[0, 20:40, 40:60] = 0
        self.assertEqual(count_instances(detection(masks)), 2)

    def test_different_classes_or_missing_masks_do_not_merge(self):
        masks = np.ones((2, 60, 100))
        result = detection(masks, [0, 1])
        self.assertEqual(count_instances(result), 2)
        result.masks = None
        self.assertEqual(count_instances(result), 2)
