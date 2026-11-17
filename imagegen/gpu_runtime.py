"""Load the packaged CUDA DLLs without importing image/neural libraries."""
import os
from pathlib import Path

_dll_handles=[]

def prepare_dlls():
    if _dll_handles:return
    import site,sys
    folders=([str(Path(sys._MEIPASS))] if getattr(sys,'frozen',False) else [])+list(site.getsitepackages())+[str(Path(__file__).resolve().parents[1]/'.venv/Lib/site-packages'),str(Path(sys.prefix).parents[1]/'.venv/Lib/site-packages')]
    import json
    roots={Path(__file__).resolve().parents[1],Path(sys.prefix).parent,Path(sys.prefix).parents[1]}
    for root in roots:
        config=root/'backends/detector-runtime.json'
        if not config.is_file():continue
        runtime=json.loads(config.read_text(encoding='utf-8'))
        library=Path(runtime['python']).parents[1]/'Lib/site-packages/torch/lib'
        if library.is_dir() and os.name=='nt':
            _dll_handles.append(os.add_dll_directory(str(library)))
            os.environ['PATH']=str(library)+os.pathsep+os.environ.get('PATH','')
    for folder in folders:
        root=Path(folder)/'nvidia'
        headers=root/'cuda_runtime'
        if (headers/'include').is_dir():os.environ.setdefault('CUDA_PATH',str(headers))
        if root.is_dir() and os.name=='nt':
            for path in root.glob('*/bin'):
                _dll_handles.append(os.add_dll_directory(str(path)))

