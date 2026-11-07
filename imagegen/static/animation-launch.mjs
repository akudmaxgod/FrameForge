const pendingStatuses=new Set(['queued_omni','generating_omni','queued_veo','generating_veo']);
export function pendingCloudAnimation(scene){
 return !scene.video_asset_id&&pendingStatuses.has(scene.data?.animation?.status);
}
export function animationLaunchPlan(scenes,ids,isSourceReady){
 const selected=new Set(ids),fresh=[],resume=[],unavailable=[];
 for(const scene of scenes){
  if(!selected.has(scene.id))continue;
  if(scene.video_asset_id||scene.data?.animation?.status==='completed'){unavailable.push(scene);continue;}
  if(pendingCloudAnimation(scene)){resume.push(scene);continue;}
  (isSourceReady(scene)?fresh:unavailable).push(scene);
 }
 return {fresh,resume,unavailable};
}
