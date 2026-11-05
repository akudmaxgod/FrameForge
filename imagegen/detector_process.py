"""CPU YOLO inference entry point for the bundled ComfyUI Python environment."""
import argparse
import contextlib
import io
import json
import os


def count_instances(result):
    """Merge duplicate segmentation detections of the same visible instance."""
    import numpy as np

    boxes = result.boxes
    if result.masks is None or len(result.masks.data) != len(boxes):
        return len(boxes)
    masks = result.masks.data.cpu().numpy() > .5
    classes = boxes.cls.cpu().numpy()
    confidence = boxes.conf.cpu().numpy()
    areas = masks.sum(axis=(1, 2))
    retained = []
    for index in np.argsort(-confidence):
        duplicate = False
        for previous in retained:
            if classes[index] != classes[previous]:
                continue
            smaller = min(areas[index], areas[previous])
            if not smaller:
                continue
            overlap = np.logical_and(masks[index], masks[previous]).sum()
            union = areas[index] + areas[previous] - overlap
            # Strong containment plus similar visible area distinguishes a
            # repeated torso from a smaller person standing within another bbox.
            if overlap / smaller >= .98 and overlap / union >= .5:
                duplicate = True
                break
        if not duplicate:
            retained.append(index)
    return len(retained)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--confidence', type=float, required=True)
    parser.add_argument('--config-dir', required=True)
    args = parser.parse_args()
    os.environ['YOLO_CONFIG_DIR'] = args.config_dir
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    os.environ['OMP_NUM_THREADS'] = '2'
    os.environ['MKL_NUM_THREADS'] = '2'
    # Imports/model messages are not part of the JSON protocol.
    with contextlib.redirect_stdout(io.StringIO()):
        import torch
        from ultralytics import YOLO
        torch.set_num_threads(2)
        result = YOLO(args.model)(args.image, device='cpu', imgsz=640,
                                  conf=args.confidence, verbose=False, save=False)[0]
        count = count_instances(result)
    print('FF_DETECTOR_RESULT:' + json.dumps({'count': count, 'raw_count': len(result.boxes)}))


if __name__ == '__main__':
    main()
