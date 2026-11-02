"""Verify the pinned local inference bundle before release or startup."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(root, full=True):
    root = Path(root).resolve()
    settings_path = root / 'data' / 'settings.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    if settings.get('local_image_generation_enabled') is False:
        # Flow does not use ComfyUI, FLUX or its Python environment. Keep the
        # selected local text analyzer available for hybrid story analysis.
        required = []
        if settings.get('text_backend', 'deepseek') not in ('deepseek', 'groq', 'nvidia'):
            model_name = settings.get('text_model', 'Qwen3-4B-Q4_K_M.gguf')
            if Path(model_name).name != model_name:
                raise RuntimeError('Invalid local text model filename.')
            required = [root / 'backends' / 'llama-cuda' / 'llama-server.exe',
                        root / 'backends' / 'models' / 'text_encoders' / model_name]
        missing = [str(path.relative_to(root)) for path in required
                   if not path.is_file() or path.stat().st_size == 0]
        if missing:
            raise RuntimeError('Flow runtime verification failed: ' + ', '.join(missing))
        return 1 if required else 0
    manifest = json.loads((root / 'MODEL_MANIFEST.json').read_text(encoding='utf-8'))
    errors = []
    for model in manifest['models']:
        path = root / model['filename']
        if not path.is_file():
            errors.append(f"missing: {model['filename']}")
            continue
        if path.stat().st_size != model['bytes']:
            errors.append(f"wrong size: {model['filename']}")
            continue
        if full:
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while block := source.read(8 * 1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != model['sha256']:
                errors.append(f"wrong SHA-256: {model['filename']}")
    required = [root / 'backends' / 'comfy-venv' / 'Scripts' / 'python.exe']
    if not list((root / 'backends' / 'ComfyUI').glob('Comfy-Org-ComfyUI-*')):
        errors.append('missing: bundled ComfyUI source')
    errors.extend(f'missing: {path.relative_to(root)}' for path in required if not path.is_file())

    try:
        import onnxruntime
        import insightface
        from insightface.app import FaceAnalysis
        insight_root = root / 'data' / 'models' / 'insightface'
        if not (insight_root / 'models' / 'buffalo_l').is_dir():
            errors.append('missing: data/models/insightface/models/buffalo_l directory')
        else:
            app = FaceAnalysis(name='buffalo_l', root=str(insight_root), providers=['CPUExecutionProvider'])
            app.prepare(ctx_id=-1, det_size=(320, 320))
            if not app.models:
                errors.append('failed: insightface models failed to load')
    except Exception as exc:
        errors.append(f'insightface/onnxruntime runtime verification failed: {exc}')

    if errors:
        raise RuntimeError('Bundle verification failed:\n' + '\n'.join(errors))
    return len(manifest['models'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quick', action='store_true', help='check file presence and exact sizes without rehashing')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    count = verify(root, full=not args.quick)
    print(f'Bundle verified: {count} models ({"quick" if args.quick else "SHA-256"}).')


if __name__ == '__main__':
    main()
