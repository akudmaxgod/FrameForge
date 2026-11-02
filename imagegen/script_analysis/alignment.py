"""Monotonic paragraph alignment. Coverage is structural, never a semantic certificate."""
import math
import re
from collections import Counter


def tokens(text):
    return {w[:6] for w in re.findall(r'[а-яёa-z]{4,}', text.casefold())}


def align_scenes(scenes, story, headings):
    n = len(scenes)
    if not n:
        raise ValueError('Пустой список сцен не может покрывать сценарий.')
    if n > len(story):
        raise ValueError('Сцен больше, чем символов сценария.')
    if len(headings) == n and all(p >= 0 for p in headings) and all(a < b for a,b in zip(headings,headings[1:])):
        starts = [0, *headings[1:]]
        return starts, starts[1:] + [len(story)], []
    boundaries = [0] + [m.end() for m in re.finditer(r'\n\s*\n', story) if m.end() < len(story)] + [len(story)]
    if len(boundaries)-1 < n:
        boundaries = sorted(set([0,len(story)] + [m.end() for m in re.finditer(r'[.!?]\s+',story)]))
    m = len(boundaries)-1
    if m < n:
        raise ValueError('Недостаточно текстовых границ для привязки сцен; требуется проверка разметки.')
    words=[tokens(story[a:b]) for a,b in zip(boundaries,boundaries[1:])]
    freq=Counter(w for group in words for w in group)
    weight={w:math.log(1+m/count) for w,count in freq.items()}
    scores=[]
    for scene in scenes:
        query=tokens(scene.summary+' '+scene.heading)
        scores.append([sum(weight[w] for w in query & group)/max(1,sum(weight.get(w,1) for w in query)) for group in words])
    # Each scene receives a nonempty contiguous run; dynamic programming preserves chronology.
    dp={(0,0):0.0};parent={}
    for i in range(1,n+1):
        prefix=[0.0]
        for value in scores[i-1]:prefix.append(prefix[-1]+value)
        for end in range(i,m-(n-i)+1):
            best=None
            for begin in range(i-1,end):
                if (i-1,begin) not in dp:continue
                value=dp[i-1,begin]+prefix[end]-prefix[begin]
                if best is None or value>best[0]:best=(value,begin)
            if best:
                dp[i,end]=best[0];parent[i,end]=best[1]
    cuts=[m];end=m
    for i in range(n,0,-1):end=parent[i,end];cuts.append(end)
    cuts.reverse();starts=[boundaries[k] for k in cuts[:-1]];ends=[boundaries[k] for k in cuts[1:]]
    warnings=['Границы сцен оценены по абзацам и порядку; полное покрытие не подтверждает смысловую точность.']
    for i,(a,b) in enumerate(zip(cuts,cuts[1:])):
        if max(scores[i][a:b],default=0)<.15:warnings.append(f'Сцена {i+1}: слабая текстовая привязка; требуется проверка.')
    assert starts[0]==0 and ends[-1]==len(story) and ends[:-1]==starts[1:]
    return starts,ends,warnings
