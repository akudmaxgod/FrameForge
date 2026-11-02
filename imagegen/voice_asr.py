"""Isolated CPU speech runtime. Audio is never sent to a cloud provider."""
import argparse
import copy
import json
import os
import hashlib
from pathlib import Path
from .voice_alignment import build_report, align_words


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


REPAIR_REVISION = 5


def repair_cache_matches(result, story):
    return (result.get('complete') and result.get('model') == 'faster-whisper-large-v3-turbo'
            and result.get('repair_revision', 0) >= REPAIR_REVISION
            and result.get('repair_story_sha256') == hashlib.sha256(story.encode()).hexdigest())


def weak_intervals(report, duration):
    """Revisit short omissions too; a single missing word can set a shot boundary."""
    intervals = []; words = report['words']; i = 0
    while i < len(words):
        if words[i]['method'] not in ('unmatched', 'interpolated'):
            i += 1; continue
        j = i
        while j < len(words) and words[j]['method'] in ('unmatched', 'interpolated'):
            j += 1
        left = next((w['end'] for w in reversed(words[:i]) if w['end'] is not None), 0)
        right = next((w['start'] for w in words[j:] if w['start'] is not None), duration)
        a, b = max(0, min(left, right) - 2.5), min(duration, max(left, right) + 2.5)
        if 0 < b - a <= 22:
            if intervals and a <= intervals[-1][1] and b - intervals[-1][0] <= 22:
                intervals[-1] = (intervals[-1][0], max(b, intervals[-1][1]))
            else: intervals.append((a, b))
        i = j
    # Do not silently abandon every omission after the twelfth. Each retry is
    # still bounded to 22 seconds and every candidate must improve alignment.
    return intervals


def improves_alignment(before, after):
    unsupported = lambda r: sum(r['metrics']['methods'][k] for k in ('unmatched', 'interpolated'))
    numeric = lambda r: {w['char_start'] for w in r['words'] if w.get('number_mismatch') or w.get('numeric_unresolved')}
    return (unsupported(after) < unsupported(before) and
            not after['metrics']['backwards_word_starts'] and numeric(after) <= numeric(before))


def splice_repair(asr, new, report, a, b):
    """Decode with context, replace only the hole between acoustic anchors."""
    words=report['words'];holes=[];recovered=[];i=0
    while i<len(words):
        if words[i]['method'] not in ('unmatched','interpolated'):
            i+=1;continue
        j=i
        while j<len(words) and words[j]['method'] in ('unmatched','interpolated'):
            j+=1
        left=next((w['end'] for w in reversed(words[:i]) if w['end'] is not None),0)
        right=next((w['start'] for w in words[j:] if w['start'] is not None),asr['audio_duration'])
        if a<=left<=right<=b:
            holes.append((left,right))
            # The old following anchor may begin too early. Locate recovered
            # words using the actual surrounding phrase, not a hard midpoint
            # cutoff that discards the last word of a successful retry.
            context=words[max(0,i-2):min(len(words),j+2)]
            phrase=' '.join(w['text'] for w in context)
            local,_=align_words(phrase,{'audio_duration':asr['audio_duration'],
                'segments':[{'words':new}]})
            first=i-max(0,i-2)
            recovered.extend((w['start'],w['end']) for w in local[first:first+j-i]
                if w['method'] not in ('unmatched','interpolated') and w['start'] is not None)
        i=j
    inside=lambda w:any(left <= (w['start']+w['end'])/2 < right for left,right in holes)
    old=[w for segment in asr['segments'] for w in segment['words'] if not inside(w)]
    candidate=copy.deepcopy(asr)
    selected=lambda w:any(left <= (w['start']+w['end'])/2 < right for left,right in recovered)
    candidate['segments']=[{'words':sorted(old+[w for w in new if selected(w)],key=lambda w:(w['start'],w['end']))}]
    return candidate


def recognize(audio_path, model_path, output, progress, story_path):
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    from faster_whisper.audio import decode_audio
    story = story_path.read_text(encoding='utf-8')
    audio = decode_audio(str(audio_path), sampling_rate=16000)
    duration = len(audio) / 16000
    model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                         cpu_threads=4, num_workers=1, local_files_only=True)
    pipe = BatchedInferencePipeline(model=model)
    sections = output.parent / (output.stem + '-sections')
    sections.mkdir(exist_ok=True)
    rows = []
    cached = json.loads(output.read_text(encoding='utf-8')) if output.exists() else None
    reuse = cached and cached.get('complete') and cached.get('model') == 'faster-whisper-large-v3-turbo'
    for start in ([] if reuse else range(0, len(audio), 180 * 16000)):
        end = min(len(audio), start + 180 * 16000)
        checkpoint = sections / (str(start) + '.json')
        if checkpoint.exists():
            section = json.loads(checkpoint.read_text(encoding='utf-8'))
        else:
            write(progress, {'processed_seconds': start / 16000, 'total_seconds': duration})
            segments, _ = pipe.transcribe(audio[start:end], batch_size=4, language='ru',
                word_timestamps=True, vad_filter=True, beam_size=5)
            section = []
            for segment in segments:
                offset = start / 16000
                section.append({'start': float(segment.start + offset), 'end': float(segment.end + offset),
                    'text': segment.text, 'words': [{'word': w.word, 'start': float(w.start + offset),
                        'end': float(w.end + offset), 'probability': float(w.probability)} for w in segment.words or []]})
                write(progress, {'processed_seconds': segment.end + offset, 'total_seconds': duration})
            write(checkpoint, section)
        rows.extend(section)
    asr = cached if reuse else {'model': 'faster-whisper-large-v3-turbo', 'audio_duration': duration,
           'complete': True, 'segments': rows}
    snapshot = {'project': {'id': '', 'story': story}, 'scenes': []}
    report = build_report(snapshot, asr)
    # Revisit bounded weak intervals automatically, accepting only improvements.
    intervals = weak_intervals(report, duration)
    diagnostics = []
    for index, (a, b) in enumerate(intervals):
        write(progress, {'processed_seconds': duration, 'total_seconds': duration,
                         'repair_current': index + 1, 'repair_total': len(intervals)})
        segments, _ = model.transcribe(audio[int(a * 16000):int(b * 16000)], language='ru',
            word_timestamps=True, vad_filter=False, beam_size=5, condition_on_previous_text=False)
        new = [{'word': w.word, 'start': float(w.start + a), 'end': float(w.end + a), 'probability': float(w.probability)}
               for segment in segments for w in segment.words or []]
        candidate = splice_repair(asr, new, report, a, b)
        proof = build_report(snapshot, candidate)
        accepted = improves_alignment(report, proof)
        diagnostics.append({'start':a,'end':b,'accepted':accepted,
            'recognized_text':' '.join(w['word'].strip() for w in new),
            'unsupported_before':sum(report['metrics']['methods'][k] for k in ('unmatched','interpolated')),
            'unsupported_after':sum(proof['metrics']['methods'][k] for k in ('unmatched','interpolated'))})
        if accepted:
            asr, report = candidate, proof
    asr['repair_revision'] = REPAIR_REVISION
    asr['repair_story_sha256'] = hashlib.sha256(story.encode()).hexdigest()
    asr['repair_intervals_checked'] = len(intervals)
    asr['repair_diagnostics'] = diagnostics
    write(output, asr)
    write(progress, {'processed_seconds': duration, 'total_seconds': duration, 'complete': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('audio', 'model', 'output', 'progress', 'story'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    recognize(args.audio, args.model, args.output, args.progress, args.story)
