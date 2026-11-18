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
        if mode=='panorama_explain':
            from .science_script import prepare_plan
            plan['headline']='Shared scene camera';plan['support_quote']='Signal enters the system. Processing starts.'
            plan['reveal_steps']=[{'kind':'focus','cue_quote':'Signal enters the system','title':'Input','region_key':'input'}, {'kind':'focus','cue_quote':'Processing starts','title':'Processing','region_key':'process'}]
            plan=prepare_plan(plan,plan['support_quote'])
            plan['scene_regions']=[{'key':'input','asset_id':'smoke','verified':True,'box':[.1,.3,.2,.3]}, {'key':'process','asset_id':'smoke','verified':True,'box':[.6,.3,.2,.3]}]
            plan['scene_layers']=[{'kind':'rotate','source_asset_id':'smoke','verified':True,'box':[.1,.3,.2,.2],'step_index':0}]
            plan['scene_paths']=[{'source_asset_id':'smoke','verified':True,'points':[[.1,.5],[.8,.5]],'step_index':0}]
            from .science_atlas_render import compile_commands
            plan=compile_commands(plan);assert plan.get('script_scene')
        render_video(plan,lambda _:image,output,duration=2,fps=24,target_size=(640,360))
        with av.open(str(output)) as video:
            stream=video.streams.video[0];frames=sum(1 for _ in video.decode(video=0))
            assert frames==48,(mode,frames)
            results.append({'mode':mode,'frames':frames,'width':stream.width,'height':stream.height,'codec':stream.codec_context.name})
    (args.output/'verification.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps({'cuda_nvenc_smoke':'passed','variants':results}),flush=True)
    return 0
