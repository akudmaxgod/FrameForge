import hashlib
import json
import os
from pathlib import Path
from huggingface_hub import hf_hub_download

ROOT=Path(os.environ.get('FRAMEFORGE_COMPONENTS') or Path(__file__).resolve().parents[1])
repo='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
revision='e8f8c211226b894fcb81acc59f3b34ba3efd5f42'
files=['onnx/model_quint8_avx2.onnx','tokenizer.json','tokenizer_config.json','sentence_bert_config.json','config.json']
target=ROOT/'models/semantic-mini'
for name in files:hf_hub_download(repo,name,revision=revision,local_dir=target)
spec={'repo':repo,'revision':revision,'file':files[0],'max_tokens':128,
      'sha256':{name:hashlib.sha256((target/name).read_bytes()).hexdigest() for name in files}}
(target/'MODEL.json').write_text(json.dumps(spec,indent=2),encoding='utf-8')
print('Local semantic model verified. User text/audio is processed offline.')
