"""Separate Markdown link destinations from speech without changing offsets.

The visible label remains narration. A URL written as ordinary prose remains
speech too: only explicit Markdown destinations are treated as metadata.
"""
import re

LINK = re.compile(r'(?<!\\)\[(?:\\.|[^\]\\])*\]\(\s*<?https?://', re.I)
REVISION = 1


def link_destinations(story):
    spans = []
    for match in LINK.finditer(story):
        start = story.rfind('](', match.start(), match.end()) + 1
        depth = 1
        escaped = False
        quote = None
        for i in range(start + 1, len(story)):
            ch = story[i]
            if ch in '\r\n':
                break
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in '\"\'' and story[i-1].isspace():
                quote = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    spans.append({'start': start, 'end': i+1, 'kind': 'markdown_link_destination',
                                  'label_start':match.start()+1,'label_end':start-1})
                    break
    return spans


def speech_projection(story):
    chars = list(story)
    for span in link_destinations(story):
        chars[span['start']:span['end']] = ' ' * (span['end'] - span['start'])
    return ''.join(chars)


def narration_text(story):
    """Export the visible prose for TTS, retaining every linked label."""
    for span in reversed(link_destinations(story)):
        story = (story[:span['label_start']-1] + story[span['label_start']:span['label_end']]
                 + story[span['end']:])
    return story
