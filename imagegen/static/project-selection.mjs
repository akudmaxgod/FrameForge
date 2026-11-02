// Serialize explicit selections so rapid clicks preserve the user's last choice.
// Background project refreshes must never call this function.
export function makeProjectActivation(api){
  let pending=Promise.resolve();
  return function activateProject(projectId){
    if(typeof projectId!=='string'||!projectId.trim())return Promise.reject(Error('Выберите проект.'));
    const activate=()=>api('/projects/current',{project_id:projectId});
    pending=pending.catch(()=>{}).then(activate);
    return pending;
  };
}
