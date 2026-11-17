"""Isolated CUDA/NVENC release smoke; never opens user projects or calls models."""
import argparse,json
from pathlib import Path

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--verify-gpu',action='store_true')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    import os
    os.environ['CUPY_CACHE_DIR']=str(args.output/'cuda-cache')
    from .release_paths import configure
    configure()
    from PIL import Image
    from .science_atlas_render import render_video
    from .science_sequence import MODES
    import av
    image=args.output/'smoke-source.png';Image.new('RGB',(400,400),'#487477').save(image)
    results=[]
    for mode in MODES:
        plan={'version':'science-atlas/1','presentation':'visual_first','presentation_motion':mode,
            'frame_density':'filled','layout':'hero_caption','asset_keys':['smoke'],
            'illustrations':[{'asset_id':'smoke','width':400,'height':400}],
            'illustration_motion':{'duration_seconds':2},
            'explanatory_labels':['Scientific overview','Signal enters the system','The system processes the signal'],
            'support_quote':'Signal enters the system, then processing starts.',
            'explanatory_scheme':{'kind':'signal','labels':['Input','System','Output'],'cue_quote':'Signal enters the system'}}
        output=args.output/(mode+'.mp4')
        render_video(plan,lambda _:image,output,duration=2,fps=24,target_size=(640,360))
        with av.open(str(output)) as video:
            stream=video.streams.video[0];frames=sum(1 for _ in video.decode(video=0))
            assert frames==48,(mode,frames)
            results.append({'mode':mode,'frames':frames,'width':stream.width,'height':stream.height,'codec':stream.codec_context.name})
    (args.output/'verification.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps({'cuda_nvenc_smoke':'passed','variants':results}),flush=True)
    return 0
