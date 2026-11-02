"""CPU YOLO inference entry point for the bundled ComfyUI Python environment."""
import argparse
import contextlib
import io
import json
import os


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
        count = len(result.boxes)
    print('FF_DETECTOR_RESULT:' + json.dumps({'count': count}))


if __name__ == '__main__':
    main()
