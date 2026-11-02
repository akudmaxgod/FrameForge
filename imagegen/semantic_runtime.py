"""Offline ONNX sentence embeddings in the isolated voice environment."""
import argparse
import hashlib
import json
from pathlib import Path

def write(path,data):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');temporary.replace(path)

def encode(texts,model_path,progress=None):
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    spec=json.loads((model_path/'MODEL.json').read_text(encoding='utf-8'))
    tokenizer=Tokenizer.from_file(str(model_path/'tokenizer.json'))
    tokenizer.enable_truncation(max_length=128,stride=16)
    pad=tokenizer.token_to_id('<pad>')
    if pad is None:pad=tokenizer.token_to_id('[PAD]')
    if pad is None:raise ValueError('Tokenizer has no pad token.')
    options=ort.SessionOptions();options.intra_op_num_threads=4;options.inter_op_num_threads=1
    session=ort.InferenceSession(str(model_path/spec['file']),sess_options=options,providers=['CPUExecutionProvider'])
    chunks=[]
    for i,text in enumerate(texts):
        if not isinstance(text,str) or not text.strip() or len(text)>20000:raise ValueError('Invalid semantic input text.')
        first=tokenizer.encode(text)
        chunks.extend((i,x) for x in [first]+list(first.overflowing))
    vectors=np.zeros((len(texts),384),dtype=np.float64);weights=np.zeros(len(texts))
    inputs={x.name for x in session.get_inputs()}
    for start in range(0,len(chunks),16):
        batch=chunks[start:start+16];width=max(len(x.ids) for i,x in batch)
        ids=np.full((len(batch),width),pad,dtype=np.int64);mask=np.zeros_like(ids);types=np.zeros_like(ids)
        for row,(i,encoded) in enumerate(batch):
            size=len(encoded.ids);ids[row,:size]=encoded.ids;mask[row,:size]=encoded.attention_mask;types[row,:size]=encoded.type_ids
        feed={k:v for k,v in {'input_ids':ids,'attention_mask':mask,'token_type_ids':types}.items() if k in inputs}
        result=session.run(None,feed)[0]
        if result.ndim==3:pooled=(result*mask[:,:,None]).sum(axis=1)/np.maximum(mask.sum(axis=1,keepdims=True),1)
        elif result.ndim==2:pooled=result
        else:raise ValueError('Unexpected ONNX sentence output.')
        for row,(i,encoded) in enumerate(batch):
            weight=max(1,sum(encoded.attention_mask)-2);vectors[i]+=pooled[row]*weight;weights[i]+=weight
        if progress:write(progress,{'processed':min(start+16,len(chunks)),'total':len(chunks)})
    vectors/=np.maximum(weights[:,None],1)
    lengths=np.linalg.norm(vectors,axis=1,keepdims=True)
    if np.any(lengths<1e-8) or not np.isfinite(vectors).all():raise ValueError('Invalid semantic vectors.')
    return (vectors/lengths).astype(np.float32).tolist()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('input','model','output','progress'):parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args();data=json.loads(args.input.read_text(encoding='utf-8'))
    vectors=encode(data['texts'],args.model,args.progress)
    write(args.output,{'input_sha256':hashlib.sha256(args.input.read_bytes()).hexdigest(),'vectors':vectors})
