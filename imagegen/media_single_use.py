"""A library file may cover only one segment of a montage."""
import copy

def tokens(value):
    result=set()
    for key,prefix in [('item_id','item:'),('media_library_item_id','item:'),('sha256','sha:'),('media_sha256','sha:'),('media_content_sha256','sha:'),('file_path','path:')]:
        if value.get(key):result.add(prefix+str(value[key]).replace('\\','/').casefold())
    return result

def used_in_document(document):
    result=set()
    for clip in document.get('clips',[]):
        layer=clip.get('footage_overlay') or (clip if clip.get('source_kind')=='footage' else None)
        if layer:result.update(tokens(layer))
    return result

def used_in_project(service,pid,document):
    result=used_in_document(document);assets=set()
    for clip in document.get('clips',[]):
        layer=clip.get('footage_overlay') or (clip if clip.get('source_kind')=='footage' else None)
        if layer:assets.update(layer.get(k) for k in ('source_image_asset_id','source_video_asset_id') if layer.get(k))
    if assets:
        for row in service.db.all('SELECT id,sha256,path FROM assets WHERE project_id=?',(pid,)):
            if row['id'] in assets:result.update(tokens({'sha256':row['sha256'],'file_path':row['path']}))
    # Resolve legacy item-only proposals and duplicate library imports to file content.
    for item in service.db.all('SELECT id,sha256,file_path FROM media_library_items WHERE project_id=?',(pid,)):
        identity=tokens({'item_id':item['id'],'sha256':item['sha256'],'file_path':item['file_path']})
        if identity & result:result.update(identity)
    return result

def unique_proposals(proposals,used=()):
    reserved=set(used);kept=[]
    for proposal in sorted(proposals,key=lambda p:({'matched':0,'needs_review':1,'rejected':2}.get(p.get('match_status'),3),-float(p.get('retrieval_score') or 0))):
        identity=tokens(proposal.get('candidate_segment') or {})
        if identity & reserved:continue
        reserved.update(identity);kept.append(proposal)
    keys={p['proposal_id'] for p in kept}
    return [p for p in proposals if p['proposal_id'] in keys]

def resolve_proposals(service,pid,proposals):
    items={r['id']:r for r in service.db.all('SELECT id,sha256,file_path FROM media_library_items WHERE project_id=?',(pid,))}
    result=copy.deepcopy(proposals)
    for proposal in result:
        candidate=proposal.get('candidate_segment') or {};item=items.get(candidate.get('item_id'))
        if item:candidate.update(media_sha256=item['sha256'],file_path=item['file_path'])
    return result
