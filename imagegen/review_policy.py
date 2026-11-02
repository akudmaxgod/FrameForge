"""The experimental visual evaluator was cancelled; source contracts stay active.

Persisted settings and cached verdicts cannot re-enable automatic visual review.
"""
AUTOMATIC_VISUAL_REVIEWS = False
MANDATORY_STILL_APPROVAL = False
CANCELLED_CATEGORIES = frozenset({'source_image', 'source_reference', 'source_prompt'})


def visual_reviews_enabled():
    return AUTOMATIC_VISUAL_REVIEWS


def category_enabled(category):
    return visual_reviews_enabled() or category not in CANCELLED_CATEGORIES


def manual_review():
    return {'status': 'manual_review_required', 'automatic': False,
            'reason': 'Автоматическая оценка отменена. Изображение нужно просмотреть вручную.'}
