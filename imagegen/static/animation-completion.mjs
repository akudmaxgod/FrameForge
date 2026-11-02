export function beginAnimationRun(previous,scenes,{replaceAssets=false}={}){
  const targets=new Map(previous&&!previous.finished?previous.targets.map(item=>[item.scene_id,item]):[]);
  const failures=previous&&!previous.finished?{...previous.failures}:{};
  for(const scene of scenes){
    targets.set(scene.id,{scene_id:scene.id,position:scene.position,previous_asset_id:replaceAssets?scene.video_asset_id:null});
    delete failures[scene.id];
  }
  return {id:Date.now()+':'+Math.random().toString(36).slice(2),targets:[...targets.values()],failures,finished:false,notified:false,dismissed:false};
}

export function animationRunProgress(run,scenes){
  const byId=new Map(scenes.map(scene=>[scene.id,scene]));
  const completed=[],failed=[],pending=[];
  for(const target of run.targets){
    const scene=byId.get(target.scene_id),anim=scene?.data?.animation||{};
    if(scene?.video_asset_id&&scene.video_asset_id!==target.previous_asset_id&&!scene.stale&&!scene.video_stale){completed.push(target);continue;}
    if(run.failures?.[target.scene_id]||!scene||scene.stale||scene.video_stale||['failed','waiting_source_image','not_planned'].includes(anim.status)||!anim.selected_engine){failed.push({...target,error:run.failures?.[target.scene_id]||anim.error||'Кадр требует внимания'});continue;}
    pending.push(target);
  }
  return {total:run.targets.length,completed,failed,pending,finished:run.targets.length>0&&!pending.length};
}
