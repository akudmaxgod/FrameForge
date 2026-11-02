"""Describe actual narration timing without claiming measured listening accuracy."""
from collections import Counter
from .domain import DomainError
from .narration_expansion import validate_timeline
from .source_contract import require_complete_coverage
from .voice_boundary_audit import build


def summarize(story, alignment, frames, duration, audio_hash, target_seconds, semantic_beats):
    data=[frame['data'] for frame in frames]
    require_complete_coverage(story,data)
    validate_timeline(story,[{'timing':d['voice_timing'],'narration_source':{
        'start':d['source_start'],'end':d['source_end'],'quote':d['source_quote']}}
        for d in data],duration)
    audit=build(story,alignment['words'],frames,duration,audio_hash)
    if any(b['support_status']=='unmatched' for b in audit['boundaries']):
        raise DomainError('Есть границы кадров без опоры на речь. Текущий план сохранён.',409)
    durations=sorted(d['voice_timing']['duration'] for d in data)
    weak=[{'position':i,'reasons':d['voice_timing'].get('timing_reasons',[])}
        for i,d in enumerate(data,1) if d['voice_timing'].get('timing_needs_review')]
    return {'version':'narrative-timing/1','basis':'recorded_audio_word_alignment',
        'audio_sha256':audio_hash,'audio_duration':duration,'semantic_beats':semantic_beats,
        'frame_count':len(data),'target_seconds':target_seconds,
        'average_seconds':duration/len(data),'minimum_seconds':durations[0],
        'maximum_seconds':durations[-1],'median_seconds':durations[len(durations)//2],
        'above_target_frames':sum(v>target_seconds+.001 for v in durations),
        'target_policy':'preferred_average_not_forced_count_or_hard_maximum',
        'boundary_support':dict(Counter(b['support_status'] for b in audit['boundaries'])),
        'review_frames':weak,'timing_accuracy_measured':False,
        'complete_audio_coverage':True,'complete_source_coverage':True}
