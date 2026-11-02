"""Bounded excerpts must never create a token by cutting another word."""

def excerpt(story, start, end, before=1200, after=0):
    left, right = max(0, start-before), min(len(story), end+after)
    while left < start and left and story[left-1].isalnum() and story[left].isalnum():
        left += 1
    while right > end and right < len(story) and story[right-1].isalnum() and story[right].isalnum():
        right -= 1
    return story[left:right]
