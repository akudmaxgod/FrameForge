export function applyAutomaticTransitions(doc,{kind='dissolve',kinds,every=3,duration_ms=300}={}){
 const chosen=kinds===undefined?[kind]:kinds;
 if(!Array.isArray(chosen)||!chosen.length||chosen.some(k=>!['dissolve','black','white'].includes(k))||
    new Set(chosen).size!==chosen.length||!Number.isInteger(every)||every<1||every>20||
    !Number.isFinite(duration_ms)||duration_ms<50||duration_ms>5000)throw Error('Отметьте хотя бы один вид перехода и проверьте частоту и длительность.');
 const clips=doc.clips.filter(c=>!c.excluded);
 const transitions=(doc.transitions||[]).filter(t=>!t.automatic);
 const manual=new Set(transitions.map(t=>`${t.left}|${t.right}`));
 let count=0;
 for(let i=0;i<clips.length-1;i++)if((i+1)%every===0&&!manual.has(`${clips[i].edit_clip_id}|${clips[i+1].edit_clip_id}`))
  transitions.push({left:clips[i].edit_clip_id,right:clips[i+1].edit_clip_id,kind:chosen[count++%chosen.length],duration_ms,automatic:true});
 doc.transitions=transitions;doc.auto_transitions={kind:chosen[0],kinds:[...chosen],every,duration_ms};return doc;
}
