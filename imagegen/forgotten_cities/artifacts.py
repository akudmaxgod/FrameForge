"""Distinct source evidence for recurring archaeological finds."""
import re


def skull_evidence(story):
    for triangle in re.finditer(r'треугольн\w*', story, re.I):
        start = max(0, triangle.start() - 650)
        preceding = list(re.finditer(r'\bчереп\w*', story[start:triangle.start()], re.I))
        if not preceding:
            continue
        skull_start = start + preceding[-1].start()
        text = story[skull_start:triangle.end() + 350]
        if not re.search(r'отверст|вырез|трепанац', text, re.I):
            continue
        return skull_start, min(len(story), triangle.end() + 350), text
    return None


def skull_name(evidence):
    return 'Череп девочки с треугольным отверстием' if re.search(r'девоч', evidence, re.I) else 'Череп с треугольным отверстием'


def skull_constraints(story):
    evidence = skull_evidence(story)
    if not evidence:
        return []
    text = evidence[2]
    site = ' in the RIGHT parietal region' if re.search(r'прав\w*\s+теменн', text, re.I) else ' at the source-described anatomical location'
    return ['One archaeological skull with the same triangular opening' + site +
            ' in every view. Preserve the three-sided contour, proportions and position; rounded remodeled margins do not turn it into a circular drilled hole. '
            'This is a distinct find: no artificial eye in this skull. Show bone only, no living surgery or invented cause.']
