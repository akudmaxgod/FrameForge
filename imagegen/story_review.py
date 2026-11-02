"""Source-text content review is the default, independent of audio workflows.

The explicit legacy opt-out is retained for compatibility with older callers.
"""
def review_analysis(analysis,story,client,folder,progress=None,*,use_visual_contract=True):
    if use_visual_contract:
        from .story_visual_plan import review_analysis as review
    else:
        from .legacy_story_review import review_analysis as review
    return review(analysis,story,client,folder,progress)
